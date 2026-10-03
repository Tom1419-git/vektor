#!/bin/bash
# caddy-guard — gardien du Caddyfile du VPS.
# Chaque minute : caddy validate. Si KO -> restauration du dernier snapshot
# valide (écriture in-place, inode préservée) + reload + alerte Vektor.
# Jamais de mv/rename sur le Caddyfile (bind-mount fichier, piège inode).
set -u
BASE=/opt/caddy
CF="$BASE/Caddyfile"
SNAPDIR="$BASE/guard-snapshots"
LOG=/var/log/caddy-guard.log
INCIDENT=/var/lib/caddy-guard/incident

ts() { date '+%F %T'; }
log() { echo "$(ts) $*" >> "$LOG"; }

validate() { docker exec caddy-caddy-1 caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; }

post_vektor_alert() {
  ip=$(docker inspect vektor-api --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null)
  secret=$(grep -E '^VEKTOR_SELFHEAL_SECRET=' /opt/vektor/.env 2>/dev/null | head -1 | cut -d= -f2-)
  [ -n "$ip" ] && [ -n "$secret" ] || return 0
  curl -s -m 30 -X POST "http://$ip:8000/api/webhook/grafana" \
    -H "Content-Type: application/json" -H "X-Vektor-Selfheal: $secret" \
    -d "{\"alerts\":[{\"fingerprint\":\"caddy-guard-restore\",\"title\":\"Caddyfile corrompu restauré automatiquement par caddy-guard\",\"status\":\"firing\",\"labels\":{\"severity\":\"critical\"}}]}" \
    >/dev/null 2>&1
}

if validate; then
  mkdir -p "$SNAPDIR"
  latest=$(ls -1t "$SNAPDIR"/snap-*.conf 2>/dev/null | head -1)
  # snapshot si aucun, ou si le fichier a changé depuis le dernier snapshot
  if [ -z "$latest" ] || ! cmp -s "$CF" "$latest"; then
    cp "$CF" "$SNAPDIR/snap-$(date +%Y%m%d%H%M%S).conf"
    ls -1t "$SNAPDIR"/snap-*.conf | tail -n +13 | xargs -r rm -f
    log "snapshot de config valide pris"
  fi
  if [ -f "$INCIDENT" ]; then
    log "config redevenue valide — incident caddy-guard clos"
    rm -f "$INCIDENT"
    printf '{"active": false}\n' > "$BASE/config/maintenance/incident.json"
    # résolution côté Vektor
    ip=$(docker inspect vektor-api --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null)
    secret=$(grep -E '^VEKTOR_SELFHEAL_SECRET=' /opt/vektor/.env 2>/dev/null | head -1 | cut -d= -f2-)
    if [ -n "$ip" ] && [ -n "$secret" ]; then
      curl -s -m 30 -X POST "http://$ip:8000/api/webhook/grafana" \
        -H "Content-Type: application/json" -H "X-Vektor-Selfheal: $secret" \
        -d '{"alerts":[{"fingerprint":"caddy-guard-restore","title":"Caddyfile corrompu restauré automatiquement par caddy-guard","status":"resolved","labels":{"severity":"critical"}}]}' \
        >/dev/null 2>&1
    fi
  fi
  exit 0
fi

# --- validate KO : restauration ---
latest=$(ls -1t "$SNAPDIR"/snap-*.conf 2>/dev/null | head -1)
if [ -z "$latest" ]; then
  log "VALIDATE KO et AUCUN snapshot — intervention manuelle requise"
  exit 1
fi
log "VALIDATE KO — restauration depuis $latest"
cat "$latest" > "$CF"
if ! validate; then
  log "le snapshot restauré est lui aussi KO — NON reload, intervention manuelle"
  exit 1
fi
docker exec caddy-caddy-1 caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
  && log "reload OK après restauration" || log "reload en échec après restauration"
printf '{"active": true, "title": "Config reverse-proxy restaurée automatiquement", "message": "Le Caddyfile est devenu invalide ; un snapshot valide a été remis en place par le gardien.", "since": "%s"}\n' "$(ts)" \
  > "$BASE/config/maintenance/incident.json"
touch "$INCIDENT"
post_vektor_alert
