"""Contrôle qBittorrent : /pause / /resume manuels pour Telegram.

Côté API uniquement : le conteneur telegram n'a ni la clé SSH ni la base —
il ne fait que relayer l'affichage (POST /api/qb/{verb}). L'exécution passe
par le canal d'actions verrouillé (SSH forced-command `vektor-actions` sur
le PVE, whitelist fermée, exécute déjà sur le CT 107 — le suffixe _103 du
nom est historique). Le registre OUI est partagé avec le chat naturel :
« mets les téléchargements en pause » → OUI suit exactement le même chemin.

Garde-fou : /resume prévient si une lecture Jellyfin est en cours —
l'action reste possible (c'est l'utilisateur qui décide), mais elle n'est
plus jamais silencieuse (leçon du 26/09 : resume all pendant un film).
"""
from __future__ import annotations

import os
import re

import httpx

from . import actions
from .tools import seeds_report

JF_TOKEN = os.environ.get("VEKTOR_JELLYFIN_TOKEN", "")
JF_URL = os.environ.get("VEKTOR_JELLYFIN_URL", "http://192.168.1.75:8096")


async def qb_state() -> tuple[int, int]:
    """(total, paused) via le rapport seeds existant (lecture seule)."""
    report = await seeds_report()
    m = re.search(r"(\d+)\s*torrents", report)
    total = int(m.group(1)) if m else 0
    p = re.search(r"(\d+)\s*en pause", report)
    paused = int(p.group(1)) if p else 0
    return total, paused


def _extract_playing(sessions) -> int | None:
    """Compte les lectures effectives ; None si l'entrée est inexploitable."""
    if not isinstance(sessions, list):
        return None
    playing = 0
    for session in sessions:
        play_state = session.get("PlayState") or {}
        if not session.get("NowPlayingItem") or not play_state.get("PlayMethod"):
            continue
        if play_state.get("IsPaused"):
            continue
        playing += 1
    return playing


async def jellyfin_playing_count() -> int | None:
    """Nombre de lectures Jellyfin actives ; None si le check échoue.

    Un échec n'est JAMAIS traité comme une absence de lecture (leçon du
    26/09) : il force l'avertissement prudent."""
    if not JF_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                f"{JF_URL}/Sessions?activeWithinSeconds=120",
                headers={"Authorization": f'MediaBrowser Token="{JF_TOKEN}"'},
            )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        sessions = response.json()
    except ValueError:
        return None
    return _extract_playing(sessions)


async def proposal_for(verb: str, user_key: str) -> str:
    """Texte de proposition (avertissement inclus) + enregistrement OUI.

    Utilise le registre d'actions partagé : un « OUI » envoyé ensuite dans
    la même conversation exécute qb_{verb}_103 — même chemin que les
    actions détectées dans le langage naturel."""
    warning = ""
    if verb == "resume":
        playing = await jellyfin_playing_count()
        if playing is None:
            warning = (
                "⚠️ Impossible de vérifier si une lecture Jellyfin est en cours "
                "(check en échec). Vérifie avant de confirmer.\n\n"
            )
        elif playing > 0:
            warning = (
                f"⚠️ {playing} lecture(s) Jellyfin détectée(s) : reprendre les "
                "torrents va leur disputer la bande passante.\n\n"
            )
    human = {
        "pause": "pause de TOUS les téléchargements qBittorrent",
        "resume": "reprise de TOUS les téléchargements qBittorrent",
    }[verb]
    actions._PENDING[user_key] = (f"qb_{verb}_103", actions.time.monotonic())
    return warning + (
        f"⚠️ Action d'écriture demandée : {human}.\n"
        "Confirme en répondant exactement **OUI** (expire dans 2 minutes)."
    )


async def execute(verb: str) -> str:
    """pause | resume via le canal d'actions verrouillé. Retour lisible."""
    action = f"qb_{verb}_103"
    result = await actions._execute(action)
    total, paused = await qb_state()
    if verb == "pause":
        return f"{result}\n\nÉtat qBit : {paused}/{total} en pause."
    return f"{result}\n\nÉtat qBit : {total - paused}/{total} actifs."
