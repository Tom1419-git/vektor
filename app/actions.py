"""Actions d'écriture avec graduation de risque (v1.7.0).

Deux classes, un seul canal d'exécution (SSH à commande forcée vers
/usr/local/bin/vektor-actions sur le PVE, qui re-valide un format fermé) :

1. CRITIQUE — double confirmation obligatoire (historique v1) :
   1. L'utilisateur demande une action (« redémarre jellyfin »).
   2. `detect_action()` la reconnaît (whitelist stricte) et renvoie une PROPOSITION.
   3. Rien n'est exécuté : Vektor demande de confirmer avec « OUI ».
   4. `confirm_pending()` n'exécute que si l'utilisateur renvoie exactement OUI
      dans la même conversation, et uniquement l'action proposée (jamais déduite
      du texte de confirmation).

2. AUTONOME — exécution immédiate sans confirmation (v1.7.0) : réservée aux
   actions RÉVERSIBLES ou strictement informationnelles (`AUTONOMOUS_ACTIONS`) :
   snapshots LXC à expiration automatique 7 jours, inventaire des snapshots,
   scan/inventaire des mises à jour Docker (aucun conteneur redémarré), fstrim.
   Le pire cas est un LV snapshot qui disparaît tout seul dans 7 jours.
   `execute_autonomous()` REFUSE toute action hors de cette classe : une
   critique ne peut jamais passer par le chemin autonome.
"""
from __future__ import annotations

import asyncio
import os
import re
import time

ACTIONS_SSH_KEY = os.environ.get(
    "VEKTOR_ACTIONS_SSH_KEY", "/app/secrets/vektor_actions_ed25519"
)
ACTIONS_SSH_HOST = os.environ.get("PVE_SSH_HOST", "PVE_HOST")
ACTIONS_SSH_USER = os.environ.get("PVE_SSH_USER", "root")

# Whitelist : mot(s)-clé FR -> (libellé humain, action SSH formatée)
ACTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"\bred[ée]marr\w*\s+(?:le\s+)?(?:conteneur\s+|container\s+)?jellyfin\b", re.I),
        "docker_restart_102_jellyfin",
    ),
    # Redémarrage Tdarr (transcodage) — libère la charge disque/CPU du PVE
    (
        re.compile(r"\bred[ée]marr\w*\s+(?:le\s+)?(?:serveur\s+|n[oe]ud\s+|node\s+|conteneur\s+|transcodage\s+)?tdarr\b", re.I),
        "docker_restart_102_tdarr-node",
    ),
    (
        re.compile(r"\bred[ée]marr\w*\s+(?:le\s+)?(?:ct|conteneur|lxc)\s*(101|102|103|104|105|106)\b", re.I),
        "lxc_restart_{0}",
    ),
    # Rescan de bibliothèque Sonarr / Radarr
    (
        re.compile(r"\b(?:relance|rerafra[îi]ch\w*|rescan\w*|reindex\w*|re[\s-]?scan\w*)\s+(?:la\s+)?(?:biblioth[èe]que\s+(?:de\s+)?)?(sonarr|radarr)\b", re.I),
        "{0}_rescan",
    ),
    (
        re.compile(r"\b(?:scan|rerafra[îi]ch|rescan)\w*\s+(?:la\s+)?biblioth[èe]que\b", re.I),
        "sonarr_rescan",
    ),
    # Mises à jour des images Docker (scan puis apply sous confirmation OUI)
    (
        re.compile(r"\b(?:check|verifie|v[ée]rifie|regarde|cherche|liste|scan)\w*\s+(?:les\s+)?(?:mises?\s+[àa]\s+jour|updates?|nouvelles?\s+(?:versions?|images?))\b", re.I),
        "docker_updates_scan",
    ),
    (
        re.compile(r"^/(checkupdates|updates)\b", re.I),
        "docker_updates_scan",
    ),
    # Inventaire lecture seule des versions (commande /majlist)
    (
        re.compile(r"^/majlist\b", re.I),
        "docker_updates_list",
    ),
    (
        re.compile(r"\b(?:liste|montre|affiche)[\w-]*\s+(?:les\s+)?(?:conteneurs?|images?)\b", re.I),
        "docker_updates_list",
    ),
    (
        re.compile(r"\bversions?\s+(?:actuelle?s?\s+et\s+disponibles?\s+)?(?:des?\s+)?(?:conteneurs?|images?|stacks?)\b", re.I),
        "docker_updates_list",
    ),
    (
        re.compile(r"\b(?:applique|installe|d[ée]ploy\w*)\w*\s+(?:les\s+)?(?:mises?[\s-]?à?[\s-]?jours?|updates?|maj|mises?[\s-]?à?[\s-]?jours?)\b", re.I),
        "docker_updates_apply",
    ),
    (
        re.compile(r"\b(?:mets?|met)[\s-]+à[\s-]+jour\w*\s+(?:les\s+)?(?:conteneurs?|images?|stacks?|docker|tout)\b", re.I),
        "docker_updates_apply",
    ),
    (
        re.compile(r"\b(?:update|upgrade)\w*\s+(?:the\s+)?(?:containers?|images?|stacks?|docker|all)\b", re.I),
        "docker_updates_apply",
    ),
    (
        re.compile(r"^/(applyupdates|maj)\b", re.I),
        "docker_updates_apply",
    ),
    # ── Classe autonome : snapshots réversibles + inventaires (v1.7.0) ──
    # /snap [CT] : snapshot thin du disque du CT, expiration auto 7 jours.
    # Sans CT explicite : CT 103 (stack arr, la plus souvent mise à jour).
    # Le script PVE n'accepte que CT 101-107 et un label [a-z0-9-]{1,24}.
    (
        re.compile(r"^/snap\s+(101|102|103|104|105|106|107)\s*$", re.I),
        "lxc_snap_{0}_pre-update",
    ),
    (
        re.compile(r"^/snap\s*$", re.I),
        "lxc_snap_pre-update",
    ),
    (
        re.compile(
            r"\bsnapshot\s+(?:du\s+|de\s+|sur\s+)?(?:CT\s*)?(101|102|103|104|105|106|107)\b",
            re.I,
        ),
        "lxc_snap_{0}_pre-update",
    ),
    (
        re.compile(r"\b(?:fais?|prend|cr[ée]e|lance)\w*\s+(?:un\s+)?snapshot\b", re.I),
        "lxc_snap_pre-update",
    ),
    # Inventaire des snapshots à expiration (lecture seule)
    (
        re.compile(r"^/snapls\s*$", re.I),
        "lxc_snapls",
    ),
    (
        re.compile(r"\b(?:liste|montre|affiche)\w*\s+(?:les\s+)?snapshots?\b", re.I),
        "lxc_snapls",
    ),
    # Pause / reprise globale qBittorrent (commandes slash et langage naturel)
    (
        re.compile(r"^/(pause)\b", re.I),
        "qb_pause_103",
    ),
    (
        re.compile(r"^/(resume)\b", re.I),
        "qb_resume_103",
    ),
    (
        re.compile(r"\b(?:mets?|stoppe?|arr[êe]te?|pause|sus?pends?)\w*\s+(?:en\s+pause\s+)?(?:tous\s+|les\s+|des\s+|tout(?:es)?\s+)*(?:les\s+|des\s+)?t[ée]l[ée]chargements?\b", re.I),
        "qb_pause_103",
    ),
    (
        re.compile(r"\b(?:reprends?|relance|r[ée]active|red[ée]marre?)\w*\s+(?:tous\s+|les\s+|des\s+|tout(?:es)?\s+)*(?:les\s+|des\s+)?t[ée]l[ée]chargements?\b", re.I),
        "qb_resume_103",
    ),
]

# Classe autonome (v1.7.0) : actions exécutables SANS confirmation OUI.
# Critères stricts : réversible (snapshot avec expiration auto) ou purement
# informationnelle (inventaires, scans sans effet sur les conteneurs).
# Les actions générées `lxc_snap_<ct>_<label>` sont couvertes par le préfixe
# (« lxc_snapls » ne contient pas « lxc_snap_ », pas de faux positif).
AUTONOMOUS_EXACT = frozenset({
    "pve_fstrim",
    "lxc_snapls",
    "docker_updates_scan",
    "docker_updates_list",
})
AUTONOMOUS_PREFIXES = ("lxc_snap_",)


def is_autonomous(action: str) -> bool:
    """True si l'action peut être exécutée immédiatement (classe sûre)."""
    return action in AUTONOMOUS_EXACT or action.startswith(AUTONOMOUS_PREFIXES)


async def execute_autonomous(action: str) -> str:
    """Exécute immédiatement une action de la classe autonome.

    Garde-fou interne : une action critique reçue ici est REFUSÉE (elle doit
    repasser par propose() -> OUI). Le script PVE re-valide de toute façon
    le format, mais le refus côté app donne un message clair à l'utilisateur."""
    if not is_autonomous(action):
        return (
            "REFUS: cette action est critique — elle exige une double "
            "confirmation OUI (demande-la, puis confirme)."
        )
    return await _execute(action)


# Timeout de confirmation : la proposition expire (évite un OUI tardif qui
# validerait une action oubliée).
PENDING_TTL_S = 120

# En production (multi-utilisateurs) il faudrait une clé par conversation ;
# ici la whitelist Telegram ne compte qu'un utilisateur autorisé.
_PENDING: dict[str, tuple[str, float]] = {}


# CT par défaut pour un snapshot sans cible explicite : 103 (arr-stack,
# la stack la plus souvent mise à jour). Résolu côté app : le script PVE
# exige un CT numérique dans l'action.
DEFAULT_SNAP_CT = "103"


def detect_action(text: str) -> tuple[str, str] | None:
    """Reconnaît une action autorisée dans le texte.

    Renvoie (libellé, action_ssh) ou None. Ne tient pas compte du contexte :
    l'exécution reste bloquée tant qu'il n'y a pas de confirmation explicite
    (pour la classe critique) ; la classe autonome s'exécute immédiatement
    (voir is_autonomous).
    """
    for pattern, action in ACTION_PATTERNS:
        match = pattern.search(text)
        if match:
            formatted = action.format(*match.groups()) if match.groups() else action
            if formatted.startswith("lxc_snap_"):
                rest = formatted[len("lxc_snap_"):]
                if not rest.split("_", 1)[0].isdigit():
                    formatted = f"lxc_snap_{DEFAULT_SNAP_CT}_{rest}"
            return formatted, formatted
    return None


def propose(action: str, user_key: str) -> str:
    """Enregistre une proposition en attente et renvoie la question."""
    _PENDING[user_key] = (action, time.monotonic())
    if action.startswith("docker_restart_"):
        rest = action[len("docker_restart_"):]
        ct, name = rest.split("_", 1)
        human = f"redémarrage du conteneur `{name}` (CT {ct})"
    elif action.startswith("lxc_restart_"):
        human = f"redémarrage du LXC CT {action[len('lxc_restart_'):]}"
    elif action.endswith("_rescan"):
        app = action[: -len("_rescan")]
        pretty = "Sonarr" if app == "sonarr" else "Radarr"
        human = f"rescan de la bibliothèque {pretty} (CT 103)"
    elif action == "docker_updates_scan":
        human = "scan des mises à jour des images Docker (CT 103/104/111, rien ne redémarre)"
    elif action == "docker_updates_apply":
        human = ("mise à jour APPLIQUÉE des conteneurs Docker (CT 103/104/111) : "
                 "recréation de ceux qui ont une nouvelle image — plusieurs minutes, "
                 "jamais pendant un stream")
    elif action == "docker_updates_list":
        human = ("inventaire des versions des conteneurs Docker (CT 103/104/111) : "
                 "lecture seule, mais rafraîchit les refs d'images (pull)")
    elif action == "lxc_snapls":
        human = "inventaire des snapshots à expiration (lecture seule)"
    elif action == "pve_fstrim":
        human = "fstrim du thin pool backup-dumps (réversible, sans effet de bord)"
    elif action.startswith("lxc_snap_"):
        rest = action[len("lxc_snap_"):]
        ct, label = rest.split("_", 1)
        human = (f"création du snapshot `{label}` du CT {ct} "
                 "(réversible, purge automatique 7 jours)")
    elif action == "qb_pause_103":
        human = "pause de TOUS les téléchargements qBittorrent (CT 103)"
    elif action == "qb_resume_103":
        human = "reprise de TOUS les téléchargements qBittorrent (CT 103)"
    else:
        human = action
    return (
        f"⚠️ Action d'écriture demandée : {human}\n\n"
        "Cette action modifiera l'état de l'infrastructure.\n"
        "Confirme en répondant exactement **OUI** (expire dans 2 minutes)."
    )


def _expire() -> None:
    now = time.monotonic()
    for key in [k for k, (_, ts) in _PENDING.items() if now - ts > PENDING_TTL_S]:
        _PENDING.pop(key, None)


async def confirm_pending(text: str, user_key: str) -> str | None:
    """Si `text` est une confirmation et une action est en attente : exécute.

    Renvoie le résultat à envoyer à l'utilisateur, ou None si le texte n'est
    pas une confirmation (la conversation suit son cours normal).
    """
    _expire()
    if text.strip().upper() not in ("OUI", "OUI.", "YES"):
        return None
    pending = _PENDING.pop(user_key, None)
    if not pending:
        return (
            "Aucune action en attente de confirmation. "
            "Demande d'abord l'action, puis confirme avec OUI."
        )
    action, _ = pending
    result = await _execute(action)
    return f"Action `{action}` :\n{result}"


def _pending_notice(user_key: str) -> str | None:
    _expire()
    pending = _PENDING.get(user_key)
    if not pending:
        return None
    return (
        f"⚠️ L'action `{pending[0]}` est déjà en attente. "
        "Réponds **OUI** pour l'exécuter, ou attends 2 minutes qu'elle expire."
    )


async def _execute(action: str) -> str:
    if not os.path.exists(ACTIONS_SSH_KEY):
        return "Canal d'actions non configuré (clé absente). Refus."
    # Le scan/apply des updates pull de vraies images : plusieurs minutes.
    timeout = 900 if action.startswith("docker_updates_") else 120
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-i", ACTIONS_SSH_KEY,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/tmp/known_hosts_actions",
            "-o", "ConnectTimeout=15",
            f"{ACTIONS_SSH_USER}@{ACTIONS_SSH_HOST}",
            action,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, OSError):
        return "Canal d'actions indisponible pour le moment."
    text = stdout.decode(errors="replace").strip()
    return text if text else "Exécution terminée (sortie vide)."
