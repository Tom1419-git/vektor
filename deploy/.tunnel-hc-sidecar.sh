#!/bin/sh
# tunnel-hc — surveille cloudflared et pinge Healthchecks tant que le
# tunnel est prêt. Idée de surveillance :
#   - toutes les 60 s : GET http://cloudflared:20241/ready
#   - status 200 avec readyConnections > 0  -> ping Healthchecks (up)
#   - sinon (processus mort, pas de connexion edge)         -> PAS de ping
# Si les pings s'arrêtent, Healthchecks passe en "down" après la grâce
# et alerte Telegram (chaîne de notifications existante).
#
# Variables d'env : HC_PING_URL (obligatoire), TUNNEL_METRICS (défaut
# http://cloudflared:20241). Pas de secret dans ce fichier.
set -u

HC_PING_URL="${HC_PING_URL:-}"
TUNNEL_METRICS="${TUNNEL_METRICS:-http://cloudflared:20241}"
INTERVAL="${INTERVAL:-60}"

[ -n "$HC_PING_URL" ] || { echo "ERREUR: HC_PING_URL manquant"; exit 1; }

echo "$(date '+%F %T') tunnel-hc démarré : metrics=$TUNNEL_METRICS ping=${HC_PING_URL%/fail}"

while true; do
  READY=$(curl -s --max-time 10 "$TUNNEL_METRICS/ready" 2>/dev/null)
  CONNS=$(printf '%s' "$READY" | grep -oE '"readyConnections":[0-9]+' | grep -oE '[0-9]+$')
  if [ -n "$CONNS" ] && [ "$CONNS" -gt 0 ]; then
    HTTP=$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' "$HC_PING_URL" 2>/dev/null)
    echo "$(date '+%F %T') tunnel OK ($CONNS connexions edge) -> ping HC: $HTTP"
  else
    echo "$(date '+%F %T') tunnel PAS PRÊT (ready=$READY) -> pas de ping (Healthchecks alertera)"
  fi
  sleep "$INTERVAL"
done
