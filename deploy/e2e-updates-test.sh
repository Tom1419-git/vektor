#!/usr/bin/env bash
# Test E2E du flux updates Docker via l'API chat (memes agent et registre
# _PENDING que Telegram). Sequence : check -> OUI (scan) -> applique -> OUI.
set -u
API=http://127.0.0.1:8011/api/web/chat
TOKEN=$(grep '^VEKTOR_API_TOKEN=' /opt/vektor/.env | head -n1 | cut -d= -f2- | tr -d '"')
OUT=/tmp/vektor-e2e-0927
mkdir -p "$OUT"

send() {
  local text="$1" max="$2" file="$3"
  echo "--- envoi : $text"
  python3 - "$API" "$TOKEN" "$text" "$max" "$file" <<'PYEOF'
import json, sys, urllib.request
api, token, text, mx, out = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
req = urllib.request.Request(
    api, data=json.dumps({"text": text}).encode(),
    headers={"Content-Type": "application/json", "X-Vektor-Token": token})
try:
    with urllib.request.urlopen(req, timeout=mx) as r:
        body = json.loads(r.read().decode())
except Exception as exc:
    body = {"response": f"ERREUR HTTP: {exc}", "conversation_id": ""}
with open(out, "w", encoding="utf-8") as fh:
    fh.write(body["response"])
print(body["response"])
PYEOF
  echo
}

echo "================ 1/4 : check les mises a jour ================"
send "check les mises à jour" 30 "$OUT/1-propose-scan.txt"
echo "================ 2/4 : OUI -> scan ================"
send "OUI" 960 "$OUT/2-resultat-scan.txt"
echo "================ 3/4 : applique les mises a jour ================"
send "applique les mises à jour" 30 "$OUT/3-propose-apply.txt"
echo "================ 4/4 : OUI -> apply ================"
send "OUI" 960 "$OUT/4-resultat-apply.txt"
echo "================ fin ================"
