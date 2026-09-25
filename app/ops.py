"""Rapport d'exploitation /ops (v1.5.3).

Contrairement à /status (infra live), /ops répond à la question
« Vektor et sa propre chaîne de supervision sont-ils en bonne santé ? » :
version déployée, conteneurs de la stack, état des checks Healthchecks,
fraisheur du dernier backup vzdump, processus de l'API.

Tout est lecture seule et sans LLM : le rapport doit répondre même quand
le chemin d'inférence est cassé. Aucun identifiant, aucune valeur secrète
n'apparaît dans la sortie.
"""

import re
import time
from pathlib import Path

import psutil

from . import watch as watch_mod

# Fichier écrit par le déploiement (voir deploy/vektor-deploy.sh) :
# il contient le tag git déployé, et rien d'autre. Absent en dev : la
# section version est simplement omise (jamais d'erreur).
_VERSION_FILE = Path("version")

# Résumé monitoring de watch.monitoring_report : "🟢 N ok · 🟡 N en retard ·
# 🔴 N down" (sections retard et down optionnelles).
_MON_SUMMARY = re.compile(r"🟢 (\d+) ok(?:\s*·\s*🟡 (\d+) en retard)?(?:\s*·\s*🔴 (\d+) down)?")


def deployed_version() -> str | None:
    """Tag lu dans le fichier `version` (une ligne, best-effort)."""
    try:
        text = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _mark(ok: bool, ok_text: str, ko_text: str) -> str:
    return f"🟢 {ok_text}" if ok else f"🔴 {ko_text}"


def _docker_self_check() -> tuple[bool, str]:
    """Indique si l'API tourne dans un conteneur (heuristic /proc/1/cgroup).

    Dans un conteneur, /proc/1/cgroup contient des entrées cgroup du
    conteneur ; sur l'hôte, il est typiquement vide ou ne montre que la
    racine. Le détail n'affiche que la structure (aucun identifiant de
    conteneur ni chemin machine — rien de sensible dans la sortie)."""
    try:
        raw = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "cgroup illisible"
    entries = [line for line in raw.splitlines() if line.strip()]
    in_container = len(entries) >= 1
    return in_container, (
        "cgroup /proc/1 : entrée(s) cgroup (conteneur)"
        if in_container
        else "cgroup /proc/1 vide (hôte)"
    )


def _process_summary() -> str:
    proc = psutil.Process()
    with proc.oneshot():
        rss_mib = proc.memory_info().rss / 2**20
        threads = proc.num_threads()
        uptime_s = time.time() - proc.create_time()
    return (
        f"⚙️ Processus API : RSS {rss_mib:.0f} MiB, {threads} threads, "
        f"up {uptime_s / 3600:.1f} h"
    )


def _backups_age_line(backups_text: str) -> str:
    """Extrait la fraisheur du premier backup depuis le rapport /backups.

    Format du canal forcé : « icon|nom|taille|age » rendu par
    watch.backups_report sous la forme « il y a <age> ». On rejoue ce
    motif sans dépendre des unités exactes : si le format change, la
    section retombe sur un renvoi vers /backups (jamais d'erreur)."""
    for token in ("il y a ", "il y a"):
        idx = backups_text.find(token)
        if idx >= 0:
            rest = backups_text[idx + len(token) :].splitlines()[0].strip()
            age = rest.split("—")[0].strip(" *`")
            if age:
                return f"💾 Backups vzdump : dernier vu il y a {age} (détail : /backups)"
    return "💾 Backups vzdump : sortie non typique (détail : /backups)"


async def ops_report() -> str:
    """Rapport d'exploitation complet — lecture seule, sans LLM."""
    lines: list[str] = ["🛰️ **VEKTOR /ops — exploitation**"]

    version = deployed_version()
    if version:
        lines.append(f"🏷️ Version déployée : `{version}`")
    else:
        lines.append("🏷️ Version déployée : inconnue (fichier `version` absent — dev ?)")

    # L'API qui répond prouve par elle-même l'état de son conteneur.
    in_container, container_detail = _docker_self_check()
    lines.append(_mark(in_container, f"API vivante ({container_detail})", "API : cgroup illisible"))

    lines.append(_process_summary())

    # Monitoring : on parse le résumé « 🟢 N ok · 🟡 N en retard · 🔴 N down »
    # de watch.monitoring_report (contrat de format existant, testé).
    try:
        monitoring = await watch_mod.monitoring_report()
        if monitoring.startswith("Monitoring :"):
            # non configuré / API en erreur : afficher tel quel, verdict neutre
            lines.append(f"🟡 {monitoring.splitlines()[0]}")
        else:
            m = _MON_SUMMARY.search(monitoring)
            ok_n, late_n, down_n = (
                (int(m.group(i)) if m.group(i) else 0) for i in (1, 2, 3)
            )
            verdict = _mark(
                down_n == 0,
                f"Monitoring : {ok_n} check(s) OK"
                + (f", {late_n} en retard (détail : /monitoring)" if late_n else ""),
                f"Monitoring : {down_n} check(s) DOWN (détail : /monitoring)",
            )
            lines.append(verdict)
    except Exception as exc:  # noqa: BLE001 — le rapport ne doit jamais planter
        lines.append(f"🔴 Monitoring : erreur ({exc.__class__.__name__})")

    # Backups : fraisheur du dernier vzdump via le rapport existant.
    try:
        lines.append(_backups_age_line(await watch_mod.backups_report()))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"🔴 Backups : erreur ({exc.__class__.__name__})")

    lines.append("\n_Lecture seule, sans inférence LLM — répond même si le chemin LLM est cassé._")
    return "\n".join(lines)
