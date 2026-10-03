#!/usr/bin/env python3
"""Pont Uptime Kuma -> Vektor (webhook /api/webhook/grafana).

- Lit kuma.db en local (monitors actifs + 2 derniers heartbeats).
- Un monitor DOWN durable (2 beats KO, ou beat KO > 90 s) devient une
  alerte "Grafana" envoyée au webhook selfheal de Vektor : Vektor raconte
  l'incident sur Telegram et applique la règle d'auto-réparation
  correspondante (jellyfin/immich/authelia) s'il en existe une.
- Les monitors "cert - *" partent en severity=warning : Vektor rapporte
  sans jamais réparer (un certificat qui expire ne se « répare » pas en
  redémarrant un conteneur).
- State persistant /var/lib/kuma-bridge/state.json : 1 seul firing par
  incident, re-notification toutes les RE NOTIFY_S (3 fois max), puis
  silence ; résolution automatique au retour UP.
- Requête HTTP vers le conteneur vektor-api via son IP docker (l'API ne
  publie aucun port sur l'hôte). Secret lu dans /opt/vektor/.env.
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DB = Path("/opt/monitoring/data/uptime-kuma/kuma.db")
STATE_DIR = Path("/var/lib/kuma-bridge")
STATE_FILE = STATE_DIR / "state.json"
LOG = Path("/var/log/kuma-bridge.log")
ENV_FILE = Path("/opt/vektor/.env")
RENOTIFY_S = 600
MAX_NOTIFY = 3
RESOLVE_GRACE_S = 60
FLAP_S = 90
SQLITE_TIMEOUT = ["-cmd", ".timeout 5000", DB]


def log(msg: str) -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.write_text("")
        with open(LOG, "a") as fh:
            fh.write(f"{time.strftime('%F %T')} {msg}\n")
    except OSError:
        pass


def sqlite_rows(query: str) -> list[tuple]:
    out = subprocess.run(
        ["sqlite3", *SQLITE_TIMEOUT, "-json", query],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return []
    return json.loads(out.stdout)


def vektor_api_ip() -> str | None:
    out = subprocess.run(
        ["docker", "inspect", "vektor-api",
         "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    # vektor-api peut être sur plusieurs réseaux : la 1re IP suffit (l'API
    # écoute sur toutes les interfaces du conteneur).
    tokens = out.stdout.split()
    return tokens[0] if tokens else None


def selfheal_secret() -> str:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("VEKTOR_SELFHEAL_SECRET="):
            return line.split("=", 1)[1].strip()
    return ""


def post_alert(ip: str, secret: str, fingerprint: str, title: str,
               status: str, severity: str, since_iso: str) -> dict | None:
    payload = json.dumps({
        "alerts": [{
            "fingerprint": fingerprint,
            "title": title,
            "status": status,
            "startsAt": since_iso,
            "labels": {"severity": severity},
        }],
    }).encode()
    req = urllib.request.Request(
        f"http://{ip}:8000/api/webhook/grafana",
        data=payload,
        headers={"Content-Type": "application/json", "X-Vektor-Selfheal": secret},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        log(f"POST {status} {fingerprint} ({title}) -> {json.dumps(body, ensure_ascii=False)[:200]}")
        return body
    except Exception as exc:
        log(f"POST {status} {fingerprint} ERREUR: {exc.__class__.__name__}: {exc}")
        return None


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_FILE.write_text(json.dumps(state, indent=1))
    STATE_FILE.chmod(0o600)


def main() -> int:
    monitors = sqlite_rows(
        "SELECT id,name FROM monitor WHERE active=1 ORDER BY id;"
    )
    if not monitors:
        log("aucun monitor actif lu (DB verrouillée ou vide) — passe")
        return 0

    beats = sqlite_rows(
        "SELECT monitor_id,status,time FROM heartbeat h1 "
        "WHERE time = (SELECT MAX(time) FROM heartbeat WHERE monitor_id = h1.monitor_id);"
    )
    last = {int(b["monitor_id"]): (int(b["status"]), b["time"]) for b in beats}
    names = {int(m["id"]): m["name"] for m in monitors}

    ip = vektor_api_ip()
    secret = selfheal_secret()
    if not ip or not secret:
        log(f"config incomplète (ip={bool(ip)}, secret={bool(secret)}) — passe")
        return 1

    state = load_state()
    now = time.time()

    for mid, (status, beat_time) in last.items():
        if mid not in names:
            # Monitor supprimé de Kuma : ses vieux heartbeats restent dans la
            # table et ressortent du MAX(time) — jamais alertés.
            continue
        key = f"kuma-{mid}"
        name = names.get(mid, f"monitor {mid}")
        entry = state.get(key, {})

        if status != 0:
            if entry and not entry.get("resolved"):
                post_alert(ip, secret, key, f"{name} rétabli", "resolved",
                           entry.get("severity", "critical"), entry.get("since", ""))
                entry["resolved"] = True
                entry["resolved_at"] = now
            continue

        # Anti-flap : un beat DOWN de moins de FLAP_S attend le prochain cycle
        # (si le service récupère entre-temps, aucune alerte ne part jamais).
        try:
            age = now - time.mktime(time.strptime(beat_time, "%Y-%m-%d %H:%M:%S.%f"))
        except ValueError:
            age = FLAP_S + 1
        if age < FLAP_S:
            continue

        if entry and not entry.get("resolved"):
            sent = entry.get("sent", 0)
            if sent >= MAX_NOTIFY:
                continue
            if now - entry.get("posted_at", 0) < RENOTIFY_S:
                continue
            resp = post_alert(ip, secret, key, f"{name} down (Kuma)", "firing",
                              entry.get("severity", "critical"), entry.get("since", beat_time))
            if resp is None:
                continue  # POST échoué : budget non consommé, retenté au prochain cycle
            entry["posted_at"] = now
            entry["sent"] = sent + 1
            if entry["sent"] >= MAX_NOTIFY:
                log(f"{key} ({name}) : max re-notifications atteint, silence")
        else:
            severity = "warning" if name.startswith("cert -") else "critical"
            resp = post_alert(ip, secret, key, f"{name} down (Kuma)", "firing",
                              severity, beat_time)
            if resp is None:
                continue  # échec POST : aucun état écrit, retenté au prochain cycle
            state[key] = {
                "posted_at": now, "sent": 1, "resolved": False,
                "severity": severity, "since": beat_time, "title": name,
            }

    # Monitors disparus de Kuma (supprimés) : clôture automatique.
    # L'incident d'un monitor absent ne peut plus être suivi — on le referme
    # pour ne pas bloquer l'état à jamais (résolution envoyée à Vektor).
    for key, entry in list(state.items()):
        if not entry.get("resolved") and key.startswith("kuma-"):
            try:
                mid_gone = int(key.split("-", 1)[1])
            except ValueError:
                continue
            if mid_gone not in last:
                post_alert(ip, secret, key,
                           f"{entry.get('title', 'monitor')} supprimé de Kuma — incident clos",
                           "resolved", entry.get("severity", "critical"), entry.get("since", ""))
                entry["resolved"] = True
                entry["resolved_at"] = now
                log(f"{key} : monitor supprimé de Kuma, incident clos")

    # purge des incidents résolus anciens (> 24 h)
    for key in list(state):
        if state[key].get("resolved") and now - state[key].get("resolved_at", 0) > 86400:
            del state[key]
    if state:
        save_state(state)
    elif STATE_FILE.exists():
        STATE_FILE.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
