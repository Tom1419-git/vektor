#!/bin/bash
# dns-watchdog — auto-réparation du résolveur failover du CT 101.
#
# Contexte (gotcha vécu) : le cache NXDOMAIN de pihole-failover (.70)
# casse la résolution de certains domaines (ex: ghcr.io mal résolu le
# 27/09). Fix manuel documenté = docker restart unbound-failover
# pihole-failover. Ce watchdog l'automatise.
#
# Logique (toutes les 5 min via systemd timer) :
#   1. Sonde 3 domaines témoins via 127.0.0.1:53 (pihole local) :
#      un "global" (cloudflare.com), un "répandu" (github.com), le
#      domaine maison (mayoraz-net.ch). Un échec = NOERROR sans
#      réponse OU NXDOMAIN OU timeout.
#   2. Si >= 2 échecs consécutifs (fichier d'état) ET anti-flap OK
#      (pas plus d'1 restart / 30 min) :
#        - docker restart unbound-failover pihole-failover
#        - re-sonde : notifie Telegram (rétabli / toujours KO)
#   3. Si le problème disparaît tout seul avant l'action : nettoie
#      l'état et note dans le journal (pas de notification).
#
# Creds Telegram : /root/.vektor-tg (token + chat_id, même format que
# le stream-guard). Secrets : AUCUN dans ce fichier.
# Logs : journalctl -u dns-watchdog.service
set -u

TG_CREDS=/root/.vektor-tg
STATE_DIR=/var/lib/dns-watchdog
STATE_FILE=$STATE_DIR/fail-count
LAST_ACTION_FILE=$STATE_DIR/last-action
FLAP_WINDOW_S=1800        # 30 min entre deux interventions max
FAIL_THRESHOLD=2          # échecs consécutifs avant d'agir
DIG_TIMEOUT=4

DOMAINS="cloudflare.com github.com mayoraz-net.ch"

mkdir -p "$STATE_DIR"

log() { echo "$(date '+%F %T') $*" >&2; }

# Garde-fou : les comparaisons numériques ne doivent JAMAIS voir une
# chaîne (sinon le else part sur un faux chemin, vécu le 28/09).
is_int() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

tg_send() {
  if [ -r "$TG_CREDS" ]; then
    read -r TG_TOKEN TG_CHAT < "$TG_CREDS"
    if [ -n "${TG_TOKEN:-}" ]; then
      rc=0
      curl -s --max-time 10 -o /dev/null \
        "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT}" \
        --data-urlencode "text=$1" || rc=$?
      log "TG envoi (rc=$rc)"
    fi
  fi
}

probe() {
  local fails=0
  for d in $DOMAINS; do
    out=$(dig +time=$DIG_TIMEOUT +tries=1 @"127.0.0.1" "$d" A 2>/dev/null)
    status=$(printf '%s' "$out" | grep -oE "status: [A-Z]+" | head -1)
    answer=$(printf '%s' "$out" | grep -cE "ANSWER: [1-9]")
    if [ -z "$status" ]; then
      log "SONDE $d : timeout/pas de reponse"
      fails=$((fails + 1))
    elif printf '%s' "$status" | grep -q "NXDOMAIN"; then
      log "SONDE $d : NXDOMAIN"
      fails=$((fails + 1))
    elif [ "$answer" -eq 0 ]; then
      log "SONDE $d : NOERROR sans reponse"
      fails=$((fails + 1))
    else
      log "SONDE $d : OK"
    fi
  done
  echo "$fails"
}

FAILS=$(probe)
is_int "$FAILS" || { log "resultat de sonde invalide ($FAILS) : abandon prudent de la passe"; exit 0; }
log "echecs de la passe : $FAILS / $(echo $DOMAINS | wc -w)"

if [ "$FAILS" -lt "$FAIL_THRESHOLD" ]; then
  if [ -f "$STATE_FILE" ]; then
    log "retour a la normale sans intervention : nettoyage de l'etat"
    rm -f "$STATE_FILE"
  fi
  exit 0
fi

# Seuil atteint : incrémenter le compteur d'échecs consécutifs
PREV=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
CONSEC=$((PREV + 1))
echo "$CONSEC" > "$STATE_FILE"
if [ "$CONSEC" -lt "$FAIL_THRESHOLD" ]; then
  log "passe $CONSEC/$FAIL_THRESHOLD en echec : on attend la prochaine passe (evite un restart sur un hoquet)"
  exit 0
fi

# Anti-flap : pas plus d'une action par fenêtre
NOW=$(date +%s)
LAST=$(cat "$LAST_ACTION_FILE" 2>/dev/null || echo 0)
if [ $((NOW - LAST)) -lt "$FLAP_WINDOW_S" ]; then
  log "anti-flap : action deja effectuee il y a $(( (NOW - LAST) / 60 )) min, on attend"
  exit 0
fi

log "DEGRADATION CONFIRMEE ($FAILS sondes KO) : restart unbound-failover + pihole-failover"
docker restart unbound-failover pihole-failover >/dev/null 2>&1
sleep 8
echo "$NOW" > "$LAST_ACTION_FILE"
rm -f "$STATE_FILE"

FAILS2=$(probe)
is_int "$FAILS2" || FAILS2=$FAIL_THRESHOLD   # si re-sonde illisible : assumer le pire (escalade)
if [ "$FAILS2" -lt "$FAIL_THRESHOLD" ]; then
  log "RETABLI apres restart ($FAILS2 sondes KO restantes)"
  tg_send "🔧 Watchdog DNS — résolveur .70 dégradé (${FAILS} sondes KO) : unbound-failover + pihole-failover redémarrés automatiquement, tout répond à nouveau."
else
  log "TOUJOURS DEGRADE apres restart ($FAILS2 sondes KO) : escalade manuelle"
  tg_send "🔴 Watchdog DNS — résolveur .70 dégradé (${FAILS} sondes KO) : restart automatique effectué MAIS ça reste dégradé (${FAILS2} KO). Investigation manuelle requise."
fi
