#!/bin/bash
# Import qBittorrent -> bitmagnet : extraction des infos torrents depuis le
# CT 107 (API qBit, bypass LAN vu de l'interieur du conteneur) puis POST
# /import vers bitmagnet (CT 111). Lecture seule sur qBit.
set -u
BITMAGNET="http://192.168.1.111:3333"
QBIT="http://127.0.0.1:8080"

echo "=== extraction qBit (CT 107) ==="
pct exec 107 -- docker exec qbittorrent sh -c \
  "curl -s --max-time 30 -b SID=bypass-lan-whitelist '$QBIT/api/v2/torrents/info?limit=2000'" \
  > /tmp/qbit-torrents.json
python3 - <<'PYEOF'
import json

raw = open("/tmp/qbit-torrents.json").read()
try:
    torrents = json.loads(raw)
except Exception:
    print("ERREUR: reponse qBit illisible :", raw[:120])
    raise SystemExit(1)
print("torrents qBit:", len(torrents))

out = []
for t in torrents:
    out.append({
        "infoHash": t.get("hash"),
        "name": t.get("name"),
        "size": t.get("size"),
        "source": "qbittorrent",
        "publishedAt": None,
    })
# supprimer les valeurs nulles
clean = [{k: v for k, v in o.items() if v is not None} for o in out]
# /import attend un objet Item (pas un tableau) : NDJSON = un JSON par ligne
with open("/tmp/bitmagnet-payload.json", "w") as fh:
    for o in clean:
        fh.write(json.dumps(o) + chr(10))
print("payload pret:", len(clean), "entrees NDJSON")
PYEOF

echo "=== POST /import vers bitmagnet ==="
curl -s --max-time 300 -H "Content-Type: application/json" -H "Connection: close" \
  --data-binary @/tmp/bitmagnet-payload.json "$BITMAGNET/import" -w "\nHTTP: %{http_code}\n" | tail -4
