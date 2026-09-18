"""Actions d'écriture avec double confirmation obligatoire.

Flux :
1. L'utilisateur demande une action (« redémarre jellyfin »).
2. `detect_action()` la reconnaît (whitelist stricte) et renvoie une PROPOSITION.
3. Rien n'est exécuté : Vektor demande de confirmer avec « OUI ».
4. `confirm_pending()` n'exécute que si l'utilisateur renvoie exactement OUI
   dans la même conversation, et uniquement l'action proposée (jamais déduite
   du texte de confirmation).

L'exécution passe par un canal SSH à commande forcée : la clé ne peut lancer
que /usr/local/bin/vektor-actions sur le PVE, qui re-valide un format fermé
(CT 101-106, blacklist des services critiques). Aucune commande libre.
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
    # Pause / reprise globale qBittorrent
    (
        re.compile(r"\b(?:mets?|stoppe?|arr[êe]te?|pause|sus?pends?)\w*\s+(?:en\s+pause\s+)?(?:tous\s+|les\s+|des\s+|tout(?:es)?\s+)*(?:les\s+|des\s+)?t[ée]l[ée]chargements?\b", re.I),
        "qb_pause_103",
    ),
    (
        re.compile(r"\b(?:reprends?|relance|r[ée]active|red[ée]marre?)\w*\s+(?:tous\s+|les\s+|des\s+|tout(?:es)?\s+)*(?:les\s+|des\s+)?t[ée]l[ée]chargements?\b", re.I),
        "qb_resume_103",
    ),
]

# Timeout de confirmation : la proposition expire (évite un OUI tardif qui
# validerait une action oubliée).
PENDING_TTL_S = 120

# En production (multi-utilisateurs) il faudrait une clé par conversation ;
# ici la whitelist Telegram ne compte qu'un utilisateur autorisé.
_PENDING: dict[str, tuple[str, float]] = {}


def detect_action(text: str) -> tuple[str, str] | None:
    """Reconnaît une action autorisée dans le texte.

    Renvoie (libellé, action_ssh) ou None. Ne tient pas compte du contexte :
    l'exécution reste bloquée tant qu'il n'y a pas de confirmation explicite.
    """
    for pattern, action in ACTION_PATTERNS:
        match = pattern.search(text)
        if match:
            formatted = action.format(*match.groups()) if match.groups() else action
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
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except (asyncio.TimeoutError, OSError):
        return "Canal d'actions indisponible pour le moment."
    text = stdout.decode(errors="replace").strip()
    return text if text else "Exécution terminée (sortie vide)."
