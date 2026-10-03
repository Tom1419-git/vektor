#!/bin/bash
# Vektor selfheal media — fixed handlers called only by vektor-actions.
# State is persistent on the PVE host so API restarts cannot reset limits.
set -u
ACTION="${1:-}"
STATE_DIR=/var/lib/vektor/selfheal-media
COOLDOWN=1800
MAX_ATTEMPTS=2
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

case "$ACTION" in
  selfheal_media_reset_jellyfin|selfheal_media_reset_immich|selfheal_media_reset_authelia)
    SERVICE="${ACTION#selfheal_media_reset_}"
    case "$SERVICE" in
      jellyfin|immich|authelia) ;;
      *) echo "ERROR: service inconnue"; exit 2 ;;
    esac
    STATE="$STATE_DIR/$SERVICE.state"
    exec 9>"$STATE_DIR/$SERVICE.lock"
    flock -x 9
    rm -f "$STATE"
    echo "RESET: budget de tentatives $SERVICE réinitialisé après résolution Grafana."
    exit 0
    ;;
  selfheal_media_jellyfin)
    SERVICE=jellyfin
    CT=102
    STATE="$STATE_DIR/jellyfin.state"
    probe() {
      pct exec 102 -- sh -c 'systemctl is-active --quiet jellyfin && curl -fsS --max-time 5 http://127.0.0.1:8096/health >/dev/null'
    }
    restart() {
      pct exec 102 -- systemctl restart jellyfin
    }
    ;;
  selfheal_media_immich)
    SERVICE=immich
    CT=109
    STATE="$STATE_DIR/immich.state"
    # Check server/API only. Never restart DB/Redis/ML and never touch data.
    probe() {
      pct exec 109 -- docker exec immich_server node -e 'fetch("http://127.0.0.1:2283/api/server/ping").then(async r=>{const b=await r.json();process.exit(r.status===200&&b.res==="pong"?0:1)}).catch(()=>process.exit(1))'
    }
    restart() {
      pct exec 109 -- docker restart immich_server
    }
    ;;
  selfheal_media_authelia)
    SERVICE=authelia
    CT=104
    STATE="$STATE_DIR/authelia.state"
    # SSO Authelia (CT 104, conteneur Docker, image pinnee 4.39.28).
    # Probe = endpoint local /api/health ; restart = conteneur uniquement,
    # jamais la DB (CT 106) ni la config. Complementaire du gardien sso-switch
    # (VPS) qui bascule les sites sur la replique pendant la panne.
    probe() {
      pct exec 104 -- sh -c 'curl -fsS --max-time 5 http://127.0.0.1:9091/api/health >/dev/null'
    }
    restart() {
      pct exec 104 -- docker restart authelia
    }
    ;;
  *)
    echo "ERROR: action inconnue"
    exit 2
    ;;
esac

# Serialize requests; a fixed per-service state file counts across alert IDs.
exec 9>"$STATE_DIR/$SERVICE.lock"
flock -x 9
NOW=$(date +%s)
ATTEMPTS=0
LAST=0
if [ -f "$STATE" ]; then
  read -r ATTEMPTS LAST < "$STATE" || true
fi
if ! [[ "$ATTEMPTS" =~ ^[0-9]+$ && "$LAST" =~ ^[0-9]+$ ]]; then
  ATTEMPTS=0
  LAST=0
fi

if probe >/dev/null 2>&1; then
  rm -f "$STATE"
  echo "HEALTHY: $SERVICE répond sainement avant action ; aucun restart nécessaire. Budget de l’épisode réinitialisé."
  exit 0
fi
if [ "$ATTEMPTS" -ge "$MAX_ATTEMPTS" ]; then
  echo "MAX_ATTEMPTS: $SERVICE est toujours en panne après $ATTEMPTS/$MAX_ATTEMPTS tentatives ; escalade opérateur requise, aucun rollback de données effectué."
  exit 0
fi
if [ "$LAST" -gt 0 ] && [ $((NOW - LAST)) -lt "$COOLDOWN" ]; then
  REMAIN=$((COOLDOWN - (NOW - LAST)))
  echo "COOLDOWN: $SERVICE, prochaine tentative possible dans ${REMAIN}s (cooldown 30 min)."
  exit 0
fi

ATTEMPTS=$((ATTEMPTS + 1))
# Save before the disruptive action: interruption/crash cannot grant extra retries.
printf '%s %s\n' "$ATTEMPTS" "$NOW" > "$STATE.tmp"
chmod 600 "$STATE.tmp"
mv -f "$STATE.tmp" "$STATE"
if ! restart >/dev/null 2>&1; then
  echo "ERROR: restart $SERVICE échoué (tentative $ATTEMPTS/$MAX_ATTEMPTS)."
  exit 1
fi

# Health probes with bounded wait, so command success alone is not declared healthy.
for delay in 2 3 5 8 13 21; do
  sleep "$delay"
  if probe >/dev/null 2>&1; then
    echo "ATTEMPT $ATTEMPTS/$MAX_ATTEMPTS HEALTHY: $SERVICE redémarré et son contrôle local répond OK."
    exit 0
  fi
done
echo "UNHEALTHY: $SERVICE ne répond toujours pas après le restart (tentative $ATTEMPTS/$MAX_ATTEMPTS) ; aucune restauration DB/photos/config n'a été tentée."
exit 1
