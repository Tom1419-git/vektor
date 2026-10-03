#!/usr/bin/env python3
"""Failover DNS Cloudflare pour *.mayoraz-net.ch — côté VPS.

Armé UNIQUEMENT si /opt/caddy/cf.env contient CF_API_TOKEN et CF_ZONE_ID
(token avec permission Zone.DNS Edit). Sinon : exit 2 avec un message
clair — le script ne s'exécute jamais « à moitié ».

Modes :
  dns-failover switch  "raison libre"   -> A/AAAA des hostnames surveillés
        vers 198.51.100.1 / 100::6400:1 (TEST-NET, noires) : Cloudflare
        ne peut plus proxifier vers l'origine morte, sa page d'erreur en
        remplace chaque site (maintenance-level, TLS intact).
  dns-failover restore                  -> retour à 132.243.200.40 + purge
        des AAAA (source de vérité : seulement IPv4 du VPS).
  dns-failover status                   -> état courant des records.

Idempotent : re-basculer sans changement ne fait rien (flag proxy conservé).
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

CF_ENV = Path("/opt/caddy/cf.env")
HOSTNAMES = [
    "auth", "jellyfin", "jellyseerr", "n8n", "pdf", "terminal", "radarr",
    "sonarr", "prowlarr", "bazarr", "qbittorrent", "portainer", "proxmox",
    "pihole", "logs", "garmin-map", "grafana", "status", "files", "kylie",
    "vektor", "vault", "immich", "proxmenux", "pegaprox", "prometheus",
    "bitmagnet",
]
ZONE_NAME = "mayoraz-net.ch"
VPS_IP = "132.243.200.40"
BLACKHOLE_V4 = "198.51.100.1"
BLACKHOLE_V6 = "100::6400:1"
API = "https://api.cloudflare.com/client/v4"


def load_creds() -> tuple[str, str]:
    if not CF_ENV.exists():
        print("NON ARMÉ : /opt/caddy/cf.env absent.")
        print("Pour armer : créer cf.env avec CF_API_TOKEN=<token Zone.DNS Edit> et CF_ZONE_ID=<id zone mayoraz-net.ch>, chmod 600.")
        raise SystemExit(2)
    creds = {}
    for line in CF_ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    token, zone = creds.get("CF_API_TOKEN", ""), creds.get("CF_ZONE_ID", "")
    if not token or not zone:
        print("NON ARMÉ : CF_API_TOKEN / CF_ZONE_ID manquants dans cf.env")
        raise SystemExit(2)
    return token, zone


def cf_request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("success"):
        raise RuntimeError(f"API CF: {json.dumps(data.get('errors'))[:300]}")
    return data["result"]


def zone_records(token: str, zone: str) -> list[dict]:
    out, page = [], 1
    while True:
        res = cf_request("GET", f"/zones/{zone}/dns_records?per_page=100&page={page}", token)
        out.extend(res)
        if len(res) < 100:
            return out
        page += 1


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"switch", "restore", "status"}:
        print(__doc__)
        return 1
    mode = sys.argv[1]
    token, zone = load_creds()

    records = zone_records(token, zone)
    targets = [r for r in records if r["name"] == ZONE_NAME or r["name"].endswith(f".{ZONE_NAME}")]
    changed = 0

    if mode == "status":
        for r in targets:
            print(f"{r['name']:40} {r['type']:5} {r['content']:40} proxy={r['proxied']}")
        return 0

    if mode == "switch":
        reason = " ".join(sys.argv[2:]) or "raison non précisée"
        wanted = {4: BLACKHOLE_V4, 28: BLACKHOLE_V6}
    else:
        wanted = {4: VPS_IP, 28: None}  # restore : AAAA supprimés

    by_name = {}
    for r in targets:
        by_name.setdefault(r["name"], []).append(r)

    for name, recs in by_name.items():
        for rtype, ip in wanted.items():
            existing = [r for r in recs if r["type"] == ("A" if rtype == 4 else "AAAA")]
            if ip is None:
                for r in existing:
                    cf_request("DELETE", f"/zones/{zone}/dns_records/{r['id']}", token)
                    print(f"DELETE {name} AAAA {r['content']}")
                    changed += 1
                continue
            correct = [r for r in existing if r["content"] == ip]
            if correct:
                continue
            if existing:
                r = existing[0]
                cf_request("PATCH", f"/zones/{zone}/dns_records/{r['id']}", token,
                           {"content": ip})
                print(f"PATCH {name} {r['type']} {r['content']} -> {ip}")
                for extra in existing[1:]:
                    cf_request("DELETE", f"/zones/{zone}/dns_records/{extra['id']}", token)
                    print(f"DELETE doublon {name} {extra['type']} {extra['content']}")
            else:
                cf_request("POST", f"/zones/{zone}/dns_records", token,
                           {"type": "A" if rtype == 4 else "AAAA", "name": name,
                            "content": ip, "ttl": 300, "proxied": True})
                print(f"CREATE {name} {'A' if rtype == 4 else 'AAAA'} {ip} (proxy)")
            changed += 1

    print(f"--- {changed} changement(s) DNS ({mode}). Raison : {reason if mode == 'switch' else 'retour à la normale'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
