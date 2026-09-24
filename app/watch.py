"""Outils de supervision élargis — lecture seule via API (chantier #5 V2).

Trois périmètres ajoutés, tous par API dédiée, aucun shell générique :

1. Backups : via le canal SSH à commande forcée (`vektor-status backups`
   sur le PVE). Note : l'API Proxmox filtre les vzdump par permission
   VMID-scoped et renvoie une liste vide pour un token PVEAuditor — le
   canal forcé est le seul chemin fiable, et il reste fermé (script
   sans argument libre côté hôte).
2. DNS : résolution de sonde sur chaque résolveur configuré, dans le
   protocole qu'il parle réellement (défini par l'URL) :
   `udp://hôte:port` = DNS wire UDP (Pi-hole :53, unbound :5335),
   `https://…{dns}` = DoH wire RFC 8484, autre https = DoH JSON.
3. Monitoring : état des checks Healthchecks.io si un token lecture est
   configuré (VEKTOR_HC_READ_TOKEN + VEKTOR_HC_API_URL).

Chaque outil dégrade proprement : non configuré -> message clair, API en
erreur -> message d'état, jamais d'exception remontée au canal.
"""

import asyncio
import ipaddress
import os
import socket
import struct
import time

import httpx

from .tools import _pve_get, _pve_node, _status_channel

HC_API_URL = os.environ.get("VEKTOR_HC_API_URL", "")
HC_READ_TOKEN = os.environ.get("VEKTOR_HC_READ_TOKEN", "")

DNS_PROBES = os.environ.get(
    "VEKTOR_DNS_PROBES", "example.com"
).split(",")

# Résolveurs à sonder, fournis via VEKTOR_DNS_RESOLVERS au format
# `nom=url`. Le schéma de l'URL choisit le transport :
#   udp://192.0.2.10:53            -> DNS wire UDP (résolveurs du LAN)
#   https://…/dns-query?{dns}      -> DoH wire RFC 8484 (template {dns})
#   https://…/dns-query            -> DoH JSON (Pi-hole v6 exposé, Cloudflare)
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


# ── DNS (sonde multi-transport) ──────────────────────────────────────────

def _encode_wire_query(name: str, qtype: int = 1) -> bytes:
    """Construit une requête DNS wire minimale (RFC 1035, RD=1).

    Entête complète de 12 octets : ID, flags, QDCOUNT/ANCOUNT/NSCOUNT/ARCOUNT."""
    qname = b"".join(bytes([len(label)]) + label.encode() for label in name.split("."))
    header = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    return header + qname + b"\x00" + struct.pack("!HH", qtype, 1)


def _parse_wire_response(message: bytes) -> tuple[int, list[str]]:
    """Extrait le RCODE et les adresses A/AAAA d'une réponse DNS wire."""
    if len(message) < 12:
        return 15, []  # RCODE 15 = réponse illisible
    rcode = message[3] & 0x0F
    qdcount = (message[4] << 8) | message[5]
    ancount = (message[6] << 8) | message[7]
    offset = 12
    for _ in range(qdcount):  # sauter la section Question
        while offset < len(message) and message[offset] != 0:
            offset += message[offset] + 1
        offset += 5  # octet nul + QTYPE + QCLASS
    ips: list[str] = []
    for _ in range(ancount):
        if offset >= len(message):
            break
        if message[offset] & 0xC0:  # nom compressé (pointeur 2 octets)
            offset += 2
        else:
            while offset < len(message) and message[offset] != 0:
                offset += message[offset] + 1
            offset += 1
        rtype, _rclass, _ttl, rdlength = struct.unpack(
            "!HHIH", message[offset:offset + 10]
        )
        offset += 10
        rdata = message[offset:offset + rdlength]
        offset += rdlength
        if rtype == 1 and rdlength == 4:
            ips.append(str(ipaddress.IPv4Address(rdata)))
        elif rtype == 28 and rdlength == 16:
            ips.append(str(ipaddress.IPv6Address(rdata)))
    return rcode, ips


def _udp_wire_query(host: str, port: int, packet: bytes, timeout: float = 4.0) -> bytes:
    """Envoie une requête DNS wire en UDP et retourne la réponse brute."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (host, port))
        data, _ = sock.recvfrom(4096)
    return data


def _encode_doh_query(name: str, qtype: str = "A") -> str:
    """Encode une requête RFC 8484 minimale (QNAME + QTYPE) en base64url."""
    import base64

    return base64.urlsafe_b64encode(_encode_wire_query(name)).decode().rstrip("=")


async def dns_report() -> str:
    """Résout une sonde sur chaque résolveur configuré et compare.

    Le transport suit le schéma de l'URL (voir DNS_RESOLVERS) : UDP wire
    pour les résolveurs du LAN, DoH wire ou JSON pour les publics. Chaque
    ligne montre le RCODE et les premières adresses obtenues."""
    if not DNS_RESOLVERS:
        return "DNS : aucun résolveur configuré (VEKTOR_DNS_RESOLVERS)."
    probe = DNS_PROBES[0].strip() or "example.com"

    lines: list[str] = [f"🌐 **DNS** — sonde `{probe}` posée à chaque résolveur"]
    async with httpx.AsyncClient(timeout=6, verify=False) as client:
        for name, url in sorted(DNS_RESOLVERS.items()):
            try:
                if url.startswith("udp://"):  # DNS wire UDP (LAN)
                    host, port = url[6:].rsplit(":", 1)
                    message = await asyncio.to_thread(
                        _udp_wire_query, host, int(port), _encode_wire_query(probe)
                    )
                    rcode, ips = _parse_wire_response(message)
                elif "{dns}" in url:  # DoH wire (RFC 8484)
                    response = await client.get(
                        url.replace("{dns}", _encode_doh_query(probe)),
                        headers={"accept": "application/dns-message"},
                    )
                    if response.status_code != 200:
                        lines.append(f"🔴 {name} : HTTP {response.status_code}")
                        continue
                    rcode, ips = _parse_wire_response(response.content)
                else:  # DoH JSON
                    response = await client.get(
                        url,
                        params={"name": probe, "type": "A"},
                        headers={"accept": "application/dns-json"},
                    )
                    if response.status_code != 200:
                        lines.append(f"🔴 {name} : HTTP {response.status_code}")
                        continue
                    rcode = response.json().get("Status", 15)
                    answers = response.json().get("Answer") or []
                    ips = [a.get("data", "") for a in answers if a.get("type") in (1, 28)]
                detail = (
                    ", ".join(ips[:2])
                    if ips
                    else ("NOERROR, 0 adresse" if rcode == 0 else f"RCODE {rcode}")
                )
                icon = "🟢" if rcode == 0 else "🟡"
                # Afficher l'adresse du résolveur sondé : les IP listées après
                # la flèche sont la RÉPONSE à la sonde (ex. example.com est
                # hébergé chez Cloudflare -> toujours 104.20.x/172.66.x),
                # jamais les adresses des résolveurs eux-mêmes.
                cible = f" ({url[6:]})" if url.startswith("udp://") else ""
                lines.append(f"{icon} {name}{cible} : {probe} → {detail}")
            except (httpx.HTTPError, ValueError, OSError) as exc:
                lines.append(f"🔴 {name} : injoignable ({exc.__class__.__name__})")
    lines.append(
        "\n_Après la flèche : adresses renvoyées POUR la sonde "
        "(pas les IP des résolveurs). Lecture seule, aucune modification DNS._"
    )
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
