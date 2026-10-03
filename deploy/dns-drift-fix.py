#!/usr/bin/env python3
"""Gardien de dérive DNS (drift-fix) — timer quotidien côté VPS.

Si l'IP publique actuelle du VPS diffère de celle en mémoire, ou si des
records A surveillés ne pointent plus vers l'IP publique, re-synchronise
via l'API Cloudflare et notifie Vektor (webhook selfheal, rapport "dit").
Sans /opt/caddy/cf.env : exit 0 silencieux (fonctionnalité non armée).
State : /var/lib/dns-drift/state.json (dernière IP vue).
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CF_ENV = Path("/opt/caddy/cf.env")
STATE_DIR = Path("/var/lib/dns-drift")
STATE_FILE = STATE_DIR / "state.json"
LOG = Path("/var/log/dns-drift.log")
_loader = importlib.machinery.SourceFileLoader("dns_failover", "/usr/local/bin/dns-failover")
_spec = importlib.util.spec_from_loader("dns_failover", _loader)
_dns = importlib.util.module_from_spec(_spec)
_loader.exec_module(_dns)
API, HOSTNAMES, ZONE_NAME = _dns.API, _dns.HOSTNAMES, _dns.ZONE_NAME
cf_request, load_creds, zone_records = _dns.cf_request, _dns.load_creds, _dns.zone_records


def log(msg: str) -> None:
    try:
        with open(LOG, "a") as fh:
            fh.write(f"{time.strftime('%F %T')} {msg}\n")
    except OSError:
        pass


def public_ip() -> str:
    for host in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(host, timeout=10) as resp:
                ip = resp.read().decode().strip()
            if ip:
                return ip
        except Exception:
            continue
    raise SystemExit("impossible de déterminer l'IP publique")


def notify_vektor(title: str, fingerprint: str, status: str) -> None:
    ip = subprocess.run(
        ["docker", "inspect", "vektor-api", "--format",
         "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"],
        capture_output=True, text=True, timeout=10, check=False,
    ).stdout.split()
    secret = ""
    for line in Path("/opt/vektor/.env").read_text().splitlines():
        if line.startswith("VEKTOR_SELFHEAL_SECRET="):
            secret = line.split("=", 1)[1].strip()
    if not ip or not secret:
        return
    payload = json.dumps({
        "alerts": [{"fingerprint": fingerprint, "title": title, "status": status,
                    "labels": {"severity": "warning"}}],
    }).encode()
    req = urllib.request.Request(
        f"http://{ip[0]}:8000/api/webhook/grafana", data=payload,
        headers={"Content-Type": "application/json", "X-Vektor-Selfheal": secret},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as exc:
        log(f"notify_vektor KO: {exc.__class__.__name__}: {exc}")


def main() -> int:
    if not CF_ENV.exists():
        return 0  # non armé : no-op silencieux pour le timer
    token, zone = load_creds()
    current = public_ip()
    last = ""
    try:
        last = json.loads(STATE_FILE.read_text()).get("last_ip", "")
    except (OSError, ValueError):
        pass

    records = zone_records(token, zone)
    drifted = [
        r for r in records
        if r["type"] == "A" and r["name"] != ZONE_NAME
        and r["name"].endswith(f".{ZONE_NAME}")
        and r["name"].split(".")[0] in HOSTNAMES
        and r["content"] != current
    ]

    if not drifted and (last == current or not last):
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        STATE_FILE.write_text(json.dumps({"last_ip": current}))
        return 0

    fixed = 0
    for r in drifted:
        cf_request("PATCH", f"/zones/{zone}/dns_records/{r['id']}", token,
                   {"content": current})
        log(f"{r['name']}: {r['content']} -> {current}")
        fixed += 1

    if last and last != current:
        log(f"rotation IP publique détectée : {last} -> {current} ({fixed} records corrigés)")
        notify_vektor(
            f"IP publique VPS changée ({last} -> {current}) ; DNS re-synchronisé ({fixed} records)",
            "dns-drift-ip-rotation", "firing")
    elif fixed:
        log(f"{fixed} record(s) A dérivé(s) corrigé(s) vers {current}")
        notify_vektor(
            f"{fixed} record(s) DNS dérivé(s) re-pointé(s) vers {current} par dns-drift-fix",
            "dns-drift-correction", "firing")

    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_FILE.write_text(json.dumps({"last_ip": current}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
