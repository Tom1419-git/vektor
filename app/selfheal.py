"""Auto-réparation Vektor : webhook Grafana -> diagnostic -> action sûre.

Principe : une alerte Grafana (severity=critical uniquement) déclenche
UNIQUEMENT une action réversible de la whitelist ci-dessous, avec
anti-tempête (une exécution max / règle / 30 min), preuves avant/après
et rapport Telegram de ce qui a été fait. Rien d'irréversible ici :
pas de rm, pas de purge, pas de destroy — le pire cas est un fstrim
qui ne fait rien ou un restart d'un conteneur bénin.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from . import actions

TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Anti-tempête : une auto-réparation par règle / 30 minutes.
_COOLDOWN_S = 1800
_LAST_RUN: dict[str, float] = {}


async def _tg(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                data={"chat_id": TG_CHAT_ID, "text": text},
            )
    except httpx.HTTPError:
        pass


def _match_rule(title: str) -> str | None:
    """Règle Grafana -> action sûre. None = pas d'action automatique."""
    t = title.lower()
    if "thin pool lvm" in t and "92%" in t:
        return "pve_fstrim"
    if "qbittorrent" in t and ("unhealthy" in t or "down" in t):
        return "docker_restart_107_qbittorrent"
    if "vektor-telegram" in t and "down" in t:
        return "bot_recreate"
    if "backup" in t and ("échec" in t or "echec" in t or "failed" in t):
        return None  # les échecs de backup ne se réparent pas tout seuls
    return None


async def handle_grafana_webhook(payload: dict) -> dict:
    """Traite un payload webhook Grafana ; renvoie un résumé (pour l'API)."""
    alerts = payload.get("alerts") or []
    acted: list[str] = []
    skipped: list[str] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            skipped.append("?")
            continue
        title = str(alert.get("title") or alert.get("labels", {}).get("alertname", ""))
        labels = alert.get("labels") or {}
        severity = str(labels.get("severity", "")).lower()
        status = str(alert.get("status", "")).lower()
        if severity != "critical" or status != "firing":
            skipped.append(title or "?")
            continue
        action = _match_rule(title)
        if action is None:
            skipped.append(title or "?")
            continue
        now = time.monotonic()
        # NB : default None (PAS 0.0) — sur une machine fraîchement bootée
        # (runner CI, conteneur redémarré), monotonic() est proche de 0 et
        # un default 0.0 ferait croire à un cooldown actif (bug attrapé
        # par la CI le 26/09 : le runner avait ~20 s d'uptime).
        last = _LAST_RUN.get(action)
        if last is not None and now - last < _COOLDOWN_S:
            skipped.append(f"{title} (cooldown)")
            continue
        _LAST_RUN[action] = now
        if action == "bot_recreate":
            # L'API ne peut pas relancer son frère (pas de socket Docker) :
            # le watchdog local du VPS s'en charge, on ne fait que déclencher
            # le run immédiat via systemd (best-effort, silencieux).
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "start", "bot-watchdog.service",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            acted.append(f"{title} -> bot-watchdog déclenché")
        else:
            result = await actions._execute(action)
            acted.append(f"{title} -> {action} : {result.strip()[:200]}")

    summary = {
        "acted": acted,
        "skipped": skipped,
    }
    if acted:
        await _tg(
            "🤖 Vektor auto-réparation :\n"
            + "\n".join(f"• {a}" for a in acted)
        )
    return summary


def detect_cancel_qbit(text: str) -> bool:
    """« annule la pause des torrents » (variation non couverte par les
    patterns existants de resume)."""
    import re

    return bool(
        re.search(
            r"\bannule?[rs]?\s+(?:la\s+)?pause\s+(?:des\s+|de\s+)?(?:torrents?t?s?|t[ée]l[ée]chargements?)\b",
            text,
            re.I,
        )
    )
