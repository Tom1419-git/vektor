"""Informations externes utiles à l'exploitation du homelab (v1.8.0).

Sources PUBLIQUES sans clé (liste https://github.com/public-apis/public-apis),
choisies pour leur pertinence infrastructure réelle :

- Cloudflare status  (www.cloudflarestatus.com/api/v2/status.json)
  → le DNS/proxy de TOUS les vhosts publics dépend de Cloudflare ; un incident
    CF se confond avec une panne VPS. Réponse en < 1 s, sans clé.
- IP publique du foyer (api.ipify.org) → vérifier que l'IP Sunrise n'a pas
  changé (le VPS filtre SSH/CrowdSec sur l'IP maison via home-ip-monitor).
- Avertissements météo France (api.bad-wetten.de non ; api.weather.gov est US)
  → remplacé par l'horloge NTP (worldtimeapi.org) : une dérive d'horloge casse
    Kerberos-like (sessions Authelia), les cron vzdump et les pings Healthchecks.

Tout est lecture seule, sans secret, timeout court, dégradation gracieuse :
l'outil renvoie toujours un texte utile, jamais d'exception vers l'agent.
"""
from __future__ import annotations

import time

import httpx

_CF_STATUS = "https://www.cloudflarestatus.com/api/v2/status.json"
_IPIFY = "https://api.ipify.org?format=json"
_TIME = "https://worldtimeapi.org/api/timezone/Europe/Paris"

_TIMEOUT = httpx.Timeout(6.0)


async def _get_json(url: str) -> dict | None:
    """Lecture tolérante : toute erreur (réseau, JSON, DNS…) renvoie None."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url)
            if r.status_code == 200:
                return r.json()
    except Exception:  # noqa: BLE001 — lecture seule : jamais propager
        return None
    return None


async def cloudflare_status() -> str:
    """Statut global Cloudflare (incident en cours ?) — source : cloudflarestatus.com."""
    data = await _get_json(_CF_STATUS)
    if not data:
        return "🔴 Cloudflare status injoignable (si le DNS est down aussi, soupçon d'incident CF global)"
    st = data.get("status", {})
    indicator = st.get("indicator", "unknown")
    emoji = {"none": "🟢", "minor": "🟡", "major": "🟠", "critical": "🔴"}.get(indicator, "⚪")
    desc = st.get("description", "?")
    return f"{emoji} Cloudflare global : **{desc}** (indicator={indicator})"


async def public_ip() -> str:
    """IP publique actuelle du foyer — à comparer avec l'IP attendue (CrowdSec)."""
    data = await _get_json(_IPIFY)
    if not data or "ip" not in data:
        return "🔴 IP publique indisponible (api.ipify.org injoignable)"
    return f"🌐 IP publique du foyer : **{data['ip']}** — si elle a changé, home-ip-monitor doit l'avoir poussée au VPS (sinon vérifier CrowdSec)."


async def clock_check() -> str:
    """Dérive d'horloge du serveur Vektor vs NTP (les sessions SSO et les cron y sont sensibles)."""
    t0 = time.time()
    data = await _get_json(_TIME)
    if not data or "unixtime" not in data:
        return "🔴 worldtimeapi injoignable — vérifier la sortie Internet du VPS"
    drift = data["unixtime"] - t0
    drift = abs(drift)
    if drift < 2:
        return f"🟢 Horloge : dérive {drift:.2f} s (OK — crons vzdump/HC et sessions SSO fiables)"
    return f"🟠 Horloge : dérive {drift:.1f} s — à corriger (systemd-timesyncd), les pings HC et sessions SSO dérivent."


async def external_report() -> str:
    """Rapport combiné : les 3 sources publiques en parallèle sémantique."""
    lines = ["🌍 **Informations externes** (APIs publiques)"]
    for fn in (cloudflare_status, public_ip, clock_check):
        try:
            lines.append(await fn())
        except Exception as exc:  # jamais faire tomber le rapport
            lines.append(f"⚪ {fn.__name__} : erreur interne ({exc.__class__.__name__})")
    return "\n".join(lines)
