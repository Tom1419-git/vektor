#!/bin/bash
# bm-retag-daily — tague les nouveaux torrents du crawl DHT (CT 111).
#
# Reutilise /usr/local/bin/bm-tagger.py en mode DELTA : export des
# torrents sans AUCUN tag (les nouveaux du crawl), putTags = ajout,
# aucun risque pour l'existant. Heartbeat Healthchecks si HC_PING_URL
# est defini (/etc/default/bm-retag) : down = job mort pendant 25 h.
# Logs : journalctl -u bm-retag.service
set -u
[ -f /etc/default/bm-retag ] && . /etc/default/bm-retag

DELTA=/tmp/bm-delta.ndjson
LOG=/tmp/bm-retag.log

# NB : NOT EXISTS (anti-join PK) et PAS "not in" (vecu : plan pourri,
# >10 min sur 190k lignes -> kill par TimeoutStartSec avant la fin).
docker exec bitmagnet-postgres psql -U postgres -d bitmagnet -t -A -c \
  "select row_to_json(t) from (select encode(t.info_hash,'hex') as h, t.name from torrents t where not exists (select 1 from torrent_tags g where g.info_hash = t.info_hash)) t" \
  > "$DELTA" 2>/dev/null

N=$(wc -l < "$DELTA")
echo "$(date '+%F %T') nouveaux torrents a taguer : $N"

if [ "$N" -gt 0 ]; then
  if python3 /usr/local/bin/bm-tagger.py "$DELTA" apply >"$LOG" 2>&1; then
    echo "$(date '+%F %T') taguage OK : $N torrents"
    grep -E "^TERMINE|^groupe" "$LOG" | tail -3
  else
    echo "$(date '+%F %T') ERREUR taguage (details ci-dessous)"
    tail -5 "$LOG"
    [ -n "${HC_PING_URL:-}" ] && curl -s --max-time 10 -o /dev/null "${HC_PING_URL}/fail"
    rm -f "$DELTA" "$LOG"
    exit 1
  fi
else
  echo "$(date '+%F %T') rien a taguer"
fi

[ -n "${HC_PING_URL:-}" ] && { curl -s --max-time 10 -o /dev/null "$HC_PING_URL" && echo "$(date '+%F %T') heartbeat HC envoye"; }
rm -f "$DELTA" "$LOG"
echo "$(date '+%F %T') fin bm-retag-daily"
