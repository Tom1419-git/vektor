"""Outils de supervision élargis — lecture seule via API (chantier #5 V2).

Trois périmètres ajoutés, tous par API dédiée, aucun shell générique :

1. Backups : liste des sauvegardes vzdump via l'API Proxmox (le token
   PVEAuditor existant a le droit de lecture storage/content).
2. DNS : résolution de sonde via les résolveurs configurés, en DoH
   (DNS-over-HTTPS) — pas de port 53 sortant, pas de binaire dig.
3. Monitoring : état des checks Healthchecks.io si un token lecture est
   configuré (VEKTOR_HC_READ_TOKEN + VEKTOR_HC_API_URL).

Chaque outil dégrade proprement : non configuré -> message clair, API en
erreur -> message d'état, jamais d'exception remontée au canal.
"""

import os
import time

import httpx

from .tools import _pve_get, _pve_node

HC_API_URL = os.environ.get("VEKTOR_HC_API_URL", "")
HC_READ_TOKEN = os.environ.get("VEKTOR_HC_READ_TOKEN", "")

DNS_PROBES = os.environ.get(
    "VEKTOR_DNS_PROBES", "example.com"
).split(",")

# Résolveurs à sonder (format `nom=url DoH`). DoH uniquement : JSON
# structuré (Status: 0 = NOERROR), pas de dépendance à dig/nslookup.
DNS_RESOLVERS: dict[str, str] = {}
for item in os.environ.get(
    "VEKTOR_DNS_RESOLVERS",
    "local=http://dns.local/dns-query",
).split(","):
    if "=" in item:
        name, url = item.split("=", 1)
        DNS_RESOLVERS[name.strip()] = url.strip()


# ── Backups (API Proxmox, lecture) ────────────────────────────────────────

async def backups_report() -> str:
    """Dernières sauvegardes vzdump vues par l'API Proxmox."""
    node = await _pve_node()
    if not node:
        return "Backups : API Proxmox injoignable ou non configurée."

    storages = await _pve_get(f"/nodes/{node}/storage")
    if not storages:
        return "Backups : liste des stockages indisponible."

    lines: list[str] = ["💾 **Derniers backups (vzdump)**"]
    found = False
    for storage in sorted(storages, key=lambda item: item.get("storage", "")):
        content = storage.get("content") or ""
        if "backup" not in content:
            continue
        name = storage.get("storage")
        snapshots = await _pve_get(
            f"/nodes/{node}/storage/{name}/content?volid=1"
        )
        if snapshots is None:
            continue
        backups = [
            item for item in snapshots
            if str(item.get("volid", "")).endswith((".tar.zst", ".tar.gz", ".tar", ".vma.zst"))
        ]
        backups.sort(key=lambda item: item.get("ctime") or 0, reverse=True)
        for item in backups[:3]:
            found = True
            volid = item.get("volid", "?")
            age_days = (time.time() - (item.get("ctime") or 0)) / 86400
            size_gib = (item.get("size") or 0) / 2**30
            flag = "🔴" if age_days > 2 else "🟢"
            lines.append(f"{flag} {volid} — {size_gib:.1f}G, il y a {age_days:.1f} j")
    if not found:
        lines.append("Aucun fichier de backup visible sur les stockages.")
    lines.append("\n_Lecture seule via API Proxmox (token PVEAuditor)._")
    return "\n".join(lines)


# ── DNS (sonde DoH) ───────────────────────────────────────────────────────

async def dns_report() -> str:
    """Résout une sonde sur chaque résolveur DoH configuré et compare."""
    if not DNS_RESOLVERS:
        return "DNS : aucun résolveur configuré (VEKTOR_DNS_RESOLVERS)."
    probe = DNS_PROBES[0].strip() or "example.com"

    lines: list[str] = [f"🌐 **DNS** — sonde `{probe}`"]
    async with httpx.AsyncClient(timeout=6) as client:
        for name, url in sorted(DNS_RESOLVERS.items()):
            try:
                response = await client.get(
                    url,
                    params={"name": probe, "type": "A"},
                    headers={"accept": "application/dns-json"},
                )
                if response.status_code != 200:
                    lines.append(f"🔴 {name} : HTTP {response.status_code}")
                    continue
                status = response.json().get("Status")
                answers = response.json().get("Answer") or []
                ips = [a.get("data") for a in answers if a.get("type") == 1]
                detail = ", ".join(ips[:2]) if ips else f"NOERROR, 0 A"
                icon = "🟢" if status == 0 else "🟡"
                lines.append(f"{icon} {name} : {detail}")
            except (httpx.HTTPError, ValueError) as exc:
                lines.append(f"🔴 {name} : injoignable ({exc.__class__.__name__})")
    lines.append("\n_Sonde DoH lecture seule — aucune modification DNS._")
    return "\n".join(lines)


# ── Monitoring (Healthchecks.io, lecture) ─────────────────────────────────

async def monitoring_report() -> str:
    """État des checks Healthchecks.io (token lecture dédié)."""
    if not (HC_API_URL and HC_READ_TOKEN):
        return (
            "Monitoring : non configuré. Renseigner VEKTOR_HC_API_URL et "
            "VEKTOR_HC_READ_TOKEN (token lecture seule) pour l'activer."
        )
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                f"{HC_API_URL}/api/v3/checks/",
                headers={"X-Api-Key": HC_READ_TOKEN},
            )
        if response.status_code != 200:
            return f"Monitoring : API répond HTTP {response.status_code}."
        checks = response.json().get("checks") or []
    except (httpx.HTTPError, ValueError) as exc:
        return f"Monitoring : injoignable ({exc.__class__.__name__})."

    if not checks:
        return "Monitoring : aucun check enregistré."
    lines = [f"🩺 **Monitoring** — {len(checks)} checks"]
    down = [c for c in checks if c.get("status") == "down"]
    late = [c for c in checks if c.get("status") == "late"]
    lines.append(
        f"🟢 {len(checks) - len(down) - len(late)} ok"
        + (f" · 🟡 {len(late)} en retard" if late else "")
        + (f" · 🔴 {len(down)} down" if down else "")
    )
    for check in (down + late)[:8]:
        lines.append(f"⚠️ {check.get('name', '?')} : {check.get('status')}")
    return "\n".join(lines)
