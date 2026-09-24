import json, sqlite3, urllib.request

db = sqlite3.connect("/root/healthchecks/hc.sqlite")
rows = db.execute("SELECT api_key, owner_id FROM accounts_project ORDER BY id").fetchall()
api_key = next((k for k, _ in rows if k), None)
if not api_key:
    raise SystemExit("PAS_DE_CLE_ADMIN")

payload = json.dumps({
    "name": "vektor-telegram",
    "slug": "vektor-telegram",
    "tags": "vektor bot",
    "desc": "Heartbeat du bot Telegram Vektor : ping toutes les 5 min depuis le conteneur. Down = bot mort (invisible ailleurs).",
    "schedule": "*/5 * * * *",
    "tz": "Europe/Zurich",
    "grace": 900,
}).encode()
req = urllib.request.Request(
    "http://192.168.1.61:8010/api/v1/checks/",
    data=payload,
    method="POST",
    headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
)
try:
    resp = json.load(urllib.request.urlopen(req))
except urllib.error.HTTPError as exc:
    detail = exc.read().decode()[:200]
    if "already exists" not in detail:
        raise SystemExit(f"ERREUR {exc.code}: {detail}")
    list_req = urllib.request.Request(
        "http://192.168.1.61:8010/api/v1/checks/vektor-telegram",
        headers={"X-Api-Key": api_key},
    )
    resp = json.load(urllib.request.urlopen(list_req))

ping_url = resp.get("ping_url", "")
print("check cree/Recupere:", resp.get("name"), "| status:", resp.get("status"))
print("PING_URL_LONGUEUR:", len(ping_url))

if ping_url:
    import base64
    open("/tmp/hc-ping-url.b64", "w").write(base64.b64encode(ping_url.encode()).decode())
    print("ping_url place dans /tmp/hc-ping-url.b64 (base64, pour transfert VPS)")
