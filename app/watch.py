"""Outils de supervision élargis — lecture seule via API (chantier #5 V2).

Trois périmètres ajoutés, tous par API dédiée, aucun shell générique :

1. Backups : via le canal SSH à commande forcée (`vektor-status backups`
   sur le PVE). Note : l'API Proxmox filtre les vzdump par permission
   VMID-scoped et renvoie une liste vide pour un token PVEAuditor — le
   canal forcé est le seul chemin fiable, et il reste fermé (script
   sans argument libre côté hôte).
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

from .tools import _pve_get, _pve_node, _status_channel

HC_API_URL = os.environ.get("VEKTOR_HC_API_URL", "")
HC_READ_TOKEN = os.environ.get("VEKTOR_HC_READ_TOKEN", "")

DNS_PROBES = os.environ.get(
    "VEKTOR_DNS_PROBES", "example.com"
).split(",")

# Résolveurs à sonder (format `nom=url DoH`). DoH uniquement : JSON
# structuré (Status: 0 = NOERROR), pas de dépendance à dig/nslookup.
# Les résolveurs réels sont fournis via VEKTOR_DNS_RESOLVERS.
DNS_RESOLVERS: dict[str, str] = {}
for item in os.environ.get("VEKTOR_DNS_RESOLVERS", "").split(","):
    if "=" in item:
        name, url = item.split("=", 1)
        DNS_RESOLVERS[name.strip()] = url.strip()


# ── Backups (canal SSH forcé, lecture) ─────────────────────────────────

async def backups_report() -> str:
    """Dernières sauvegardes vzdump, via le canal SSH à commande forcée.

    L'API Proxmox filtre les vzdump par permission VMID-scoped : un token
    PVEAuditor reçoit une liste vide même avec Datastore.Audit. Le canal
    forcé (`vektor-status backups`, script fermé sur l'hôte) est le seul
    chemin fiable — et il n'autorise aucune commande libre."""
    raw = await _status_channel("vektor-status backups")
    if raw.startswith("Canal") or raw.startswith("Sortie"):
        return f"Backups : {raw[0].lower()}{raw[1:]}"

    lines: list[str] = ["💾 **Derniers backups (vzdump)**"]
    count = 0
    for line in raw.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        icon_raw, name, size, age = parts
        if icon_raw == "NONE":
            lines.append("Aucun fichier de backup visible sur les stockages.")
            break
        count += 1
        icon = "🟢" if icon_raw == "OK" else "🔴"
        lines.append(f"{icon} `{name}` — {size}, il y a {age}")
    if count:
        lines.append(f"\n{count} fichiers vus — 🔴 = plus de 2 jours.")
    lines.append("\n_Lecture seule via canal SSH forcé (script fermé)._")
    return "\n".join(lines)


# ── DNS (sonde DoH) ───────────────────────────────────────────────────────

def _encode_doh_query(name: str, qtype: str = "A") -> str:
    """Encode une requête RFC8484 minimale (QNAME + QTYPE) en base64url.

    Utilisé pour les résolveurs en mode wire (template avec {dns}) : le
    format JSON est utilisé sinon quand le résolveur le supporte."""
    import base64
    import struct

    qname = b"".join(bytes([len(label)]) + label.encode() for label in name.split("."))
    packet = struct.pack("!HHHHH", 0x1234, 0x0100, 1, 0, 0) + qname + b"\x00"
    packet += struct.pack("!HH", {"A": 1, "AAAA": 28, "TXT": 16}[qtype], 1)
    return base64.urlsafe_b64encode(packet).decode().rstrip("=")


async def dns_report() -> str:
    """Résout une sonde sur chaque résolveur configuré et compare.

    Deux modes par résolveur, choisi par l'URL :
    - template contenant {dns} : wire RFC8484 (paquet binaire en base64url)
    - sinon : JSON (?name=&type=, accept dns-json — Pi-hole v6, Cloudflare…)

    Aucun contact sur le port UDP 53 : uniquement HTTPS (443)."""
    if not DNS_RESOLVERS:
        return "DNS : aucun résolveur configuré (VEKTOR_DNS_RESOLVERS)."
    probe = DNS_PROBES[0].strip() or "example.com"

    lines: list[str] = [f"🌐 **DNS** — sonde `{probe}`"]
    async with httpx.AsyncClient(timeout=6, verify=False) as client:
        for name, url in sorted(DNS_RESOLVERS.items()):
            try:
                if "{dns}" in url:  # mode wire (RFC8484)
                    response = await client.get(
                        url.replace("{dns}", _encode_doh_query(probe)),
                        headers={"accept": "application/dns-message"},
                    )
                    if response.status_code != 200:
                        lines.append(f"🔴 {name} : HTTP {response.status_code}")
                        continue
                    message = response.content
                    rcode = message[3] & 0x0F if len(message) > 3 else 15
                    answers = (message[6] << 8) | message[7] if len(message) >= 8 else 0
                    detail = f"NOERROR, {answers} réponse(s)" if rcode == 0 else f"RCODE {rcode}"
                    icon = "🟢" if rcode == 0 else "🟡"
                else:  # mode JSON
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
                    detail = ", ".join(ips[:2]) if ips else "NOERROR, 0 A"
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
