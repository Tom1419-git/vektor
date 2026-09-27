#!/bin/bash
# ════════════════════════════════════════════════════════════════
# COPIE VERSIONNÉE — source de vérité du stream-guard déployé sur PVE.
#
# Install :
#   scp deploy/vektor-stream-guard.sh pve:/usr/local/bin/vektor-stream-guard
#   ssh pve -- chmod +x /usr/local/bin/vektor-stream-guard
#
# Le shebang DOIT rester en ligne 1 (exec systemd) : cet en-tête est
# inséré APRÈS la ligne #!/bin/bash, jamais avant.
#
# Le fix infirmier qBit (bug v1.6.3, corrigé le 27/09 : curl SANS
# -w '%{http_code}' => sortie toujours vide => qBit déclaré muet à
# chaque cycle => restart qBit toutes les 10 min) est EMBARQUÉ dans ce
# fichier : qbit_api_alive() renvoie le code HTTP et les tests comparent
# = 200 / != 200. deploy/patch-nurse-qbit.py reste en historique
# (patch in-situ d'avant versioning) — inutile après une install depuis
# cette copie.
#
# Secrets : AUCUN dans ce fichier (creds lues dans /root/.vektor-*).
# ════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# vektor-stream-guard — priorise le streaming Jellyfin sur qBittorrent
#
# Toutes les minutes (systemd timer) :
#   1. Lit les sessions Jellyfin (CT 102, API /Sessions, header
#      Authorization: MediaBrowser — format 12.x).
#   2. Si au moins une session Transcode / DirectStream / DirectPlay
#      est active (non en pause) : pause globale qBittorrent (CT 103)
#      et notification Telegram Vektor.
#   3. Si plus aucune lecture ET que la pause vient du guard :
#      reprise globale qBittorrent et notification Telegram.
#   4. Hysteresis : reprise seulement apres PAUSE_RELEASE_DELAY_S
#      sans lecture (evite les micro-coupures entre episodes).
#
# Etat : /var/lib/vektor/stream-guard.state (mode + timestamp)
# Creds : /root/.vektor-jf (cle Jellyfin), /root/.vektor-qb (qBit),
#         /root/.vektor-tg (token bot + chat id Telegram)
# Logs  : journalctl -u vektor-stream-guard.service
# ═══════════════════════════════════════════════════════════════
set -u

STATE_DIR=/var/lib/vektor
STATE_FILE=$STATE_DIR/stream-guard.state
EVENT_LOG=$STATE_DIR/stream-guard.log   # PAUSE|epoch|detail / RESUME|epoch|duree_s
PAUSE_RELEASE_DELAY_S=180      # 3 min sans lecture avant reprise
RECONCILE_EVERY_S=60          # vérification « toujours en pause » chaque tick (60 s)
CURL_TIMEOUT=12
SESS_TMP=/tmp/vektor-guard-sess.json
QB_TMP=/tmp/vektor-guard-qb.json

mkdir -p "$STATE_DIR"
touch "$STATE_FILE"

log() { echo "$(date '+%F %T') $*"; }

# ── Credentials ──
[ -r /root/.vektor-jf ] && JF_KEY=$(cat /root/.vektor-jf) || { log "ERREUR: cle Jellyfin absente"; exit 1; }
[ -r /root/.vektor-tg ] && read -r TG_TOKEN TG_CHAT < /root/.vektor-tg || TG_TOKEN=""

tg_send() {
  [ -n "$TG_TOKEN" ] || return 0
  curl -s --max-time "$CURL_TIMEOUT" -o /dev/null \
    "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TG_CHAT}" \
    --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

# ── 1. Sessions Jellyfin (header MediaBrowser, format 12.x) ──
pct exec 102 -- curl -s --max-time "$CURL_TIMEOUT" \
  -H "Authorization: MediaBrowser Token=\"${JF_KEY}\"" \
  "http://127.0.0.1:8096/Sessions?activeWithinSeconds=120" > "$SESS_TMP" 2>/dev/null

PLAYING=$(python3 - "$SESS_TMP" <<'PYEOF'
import json, sys
try:
    sessions = json.load(open(sys.argv[1]))
except Exception:
    print("ERROR"); raise SystemExit
names = []
playing = 0
for s in sessions:
    ps = s.get("PlayState") or {}
    method = ps.get("PlayMethod")
    item = s.get("NowPlayingItem")
    if not item or not method:
        continue  # session sans lecture effective (web ouvert, API interne)
    if ps.get("IsPaused"):
        continue  # lecture en pause : pas de charge reelle
    if method in ("Transcode", "DirectStream", "DirectPlay"):
        playing += 1
        title = str(item.get("Name", "?"))[:40]
        names.append(f"{s.get('UserName', '?')} ({method}) : {title}")
print(playing)
for n in names:
    print(n)
PYEOF
)

if [ "$PLAYING" = "ERROR" ] || [ -z "$PLAYING" ]; then
  log "ERREUR: reponse Jellyfin illisible (pas d'action par securite)"
  exit 0
fi
N_PLAYING=$(printf '%s\n' "$PLAYING" | head -1)
DETAILS=$(printf '%s\n' "$PLAYING" | tail -n +2 | head -3)

# ── 2. Helper qBittorrent : auth + action globale ──
qb_all() {  # $1 = pause | resume ; affiche "stopped total"
  # bypass_auth_subnet_whitelist=192.168.1.0/24 : appel direct sans login
  # (creds /root/.vektor-qb mortes depuis la rotation qBit du 23/09)
  local VERB="$1"
  # qBit tourne en DOCKER BRIDGE sur le CT 107 : vu du CT, un appel a
  # localhost:8181 (port publishé) arrive depuis la gateway Docker
  # (172.17.0.1), hors whitelist LAN -> 403. Le bypass LAN ne marche
  # que depuis l INTERIEUR du conteneur (localhost = 127.0.0.1 y est
  # couvert par bypass_local_auth=true) : passer par docker exec.
  pct exec 107 -- docker exec qbittorrent sh -c \
    "curl -s --max-time $CURL_TIMEOUT -o /dev/null -b SID=bypass-lan-whitelist \
     -d hashes=all http://localhost:8080/api/v2/torrents/${VERB}" 2>/dev/null
  sleep 2
  pct exec 107 -- docker exec qbittorrent sh -c \
    "curl -s --max-time $CURL_TIMEOUT -b SID=bypass-lan-whitelist \
     http://localhost:8080/api/v2/torrents/info" 2>/dev/null > "$QB_TMP"
  python3 - "$QB_TMP" "$VERB" <<'PYEOF'
import json, sys
try:
    ts = json.load(open(sys.argv[1]))
except Exception:
    print("-1 -1 err"); raise SystemExit
stopped = sum(1 for t in ts if str(t.get("state", "")).startswith(("paused", "stopped")))
total = len(ts)
# effet = la commande a-t-elle produit l'état attendu ? (un torrent déjà
# stoppé volontairement ne fait pas d'un resume un échec)
if sys.argv[2] == "pause":
    effect = "no" if (total > 0 and stopped == 0) else "yes"
else:
    effect = "no" if (total > 0 and stopped == total) else "yes"
print(stopped, total, effect)
PYEOF
}

qb_check() {  # lecture seule : affiche "stopped total" (aucune commande)
  pct exec 107 -- docker exec qbittorrent sh -c \
    "curl -s --max-time $CURL_TIMEOUT -b SID=bypass-lan-whitelist \
     http://localhost:8080/api/v2/torrents/info" 2>/dev/null > "$QB_TMP"
  python3 - "$QB_TMP" <<'PYEOF'
import json, sys
try:
    ts = json.load(open(sys.argv[1]))
except Exception:
    print("-1 -1"); raise SystemExit
stopped = sum(1 for t in ts if str(t.get("state", "")).startswith(("paused", "stopped")))
print(stopped, len(ts))
PYEOF
}

# ── 2bis. Infirmier qBittorrent : si l'API ne répond plus (conteneur
# unhealthy / crash Docker), redémarrer le conteneur via le canal d'actions
# verrouillé. Toutes les 10 ticks (~10 min), JAMAIS pendant un stream.
QBIT_NURSE_EVERY_TICKS=10
QBIT_NURSE_COUNTER_FILE=/var/lib/vektor/stream-guard-nurse.counter

qbit_api_alive() {
  pct exec 107 -- docker exec qbittorrent sh -c \
    "curl -s -o /dev/null --max-time $CURL_TIMEOUT -w '%{http_code}' -b SID=bypass-lan-whitelist http://localhost:8080/api/v2/app/version" 2>/dev/null
}

N=$(cat "$QBIT_NURSE_COUNTER_FILE" 2>/dev/null || echo 0)
N=$((N + 1))
if [ "$N" -ge "$QBIT_NURSE_EVERY_TICKS" ]; then
  N=0
  if [ "$(qbit_api_alive)" != "200" ]; then
    # re-test 10 s plus tard pour éviter un redémarrage sur un hoquet réseau
    sleep 10
    if [ "$(qbit_api_alive)" != "200" ]; then
      log "INFIRMIER : API qBittorrent muette 2x — restart du conteneur"
      if pct status 107 2>/dev/null | grep -q "running"; then
        pct exec 107 -- docker restart qbittorrent >/dev/null 2>&1
        sleep 8
        if [ "$(qbit_api_alive)" = "200" ]; then
          log "INFIRMIER : qBittorrent relancé et répond"
          tg_send "🔧 Vektor — qBittorrent ne répondait plus : conteneur redémarré automatiquement, il répond à nouveau."
        else
          log "INFIRMIER : restart effectué mais l'API reste muette (escalade manuelle)"
          tg_send "⚠️ Vektor — qBittorrent redémarré mais toujours muet : investigation manuelle requise."
        fi
      else
        log "INFIRMIER : CT 107 arrêté — pas d'action du guard (CT level géré ailleurs)"
      fi
    fi
  fi
fi
echo "$N" > "$QBIT_NURSE_COUNTER_FILE"

# ── 3. Machine a etats ──
NOW=$(date +%s)
read -r MODE SINCE < "$STATE_FILE" 2>/dev/null || { MODE=idle; SINCE=$NOW; }
[ -n "${MODE:-}" ] || MODE=idle

if [ "$N_PLAYING" -gt 0 ]; then
  if [ "$MODE" != "paused" ]; then
    log "Lecture detectee ($N_PLAYING) : pause qBittorrent"
    read -r STOPPED TOTAL EFFECT < <(qb_all pause)
    if [ "$TOTAL" -ge 0 ] 2>/dev/null && [ "$EFFECT" != "no" ]; then
      echo "paused $NOW" > "$STATE_FILE"
      MSG="🎬 Vektor — mode streaming
Jellyfin diffuse :
${DETAILS:-($N_PLAYING session(s))}

⏸️ qBittorrent PAUSE (${STOPPED}/${TOTAL} torrents) pour prioriser ta bande passante.
(seuls les torrents sont mis en pause : le LAN, les services et Jellyfin ne sont pas touchés)
Je reprendrai les téléchargements 3 min après la fin de la lecture."
      tg_send "$MSG"
      printf 'PAUSE|%s|%s\n' "$NOW" "$(printf '%s' "$DETAILS" | head -1)" >> "$EVENT_LOG"
      log "PAUSE appliquee : ${STOPPED}/${TOTAL} — notif envoyee"
    elif [ "$EFFECT" = "no" ]; then
      log "ECHEC D'EFFET : pause commandée mais 0 torrent stopped (refus qBit ?)"
      tg_send "⚠️ Vektor — stream-guard : pause qBit commandée mais SANS EFFET (0 torrent en pause). Vérifier l'accès API qBittorrent."
    else
      log "ERREUR: authentification qBittorrent echouee (pause non appliquee)"
    fi
  else
    ELAPSED_ANCHOR=$(( NOW - SINCE ))
    REFRESH_ANCHOR=1
    if [ "$ELAPSED_ANCHOR" -gt "$PAUSE_RELEASE_DELAY_S" ]; then
      # Pause ancienne (jamais reprise : session precedente interrompue, arret
      # du guard, etc.) et nouvelle lecture detectee : informer comme une
      # vraie PAUSE (et re-pauser au cas ou les torrents auraient repris).
      log "Lecture sur pause ancienne (${ELAPSED_ANCHOR}s) : pause + notification"
      read -r STOPPED TOTAL EFFECT < <(qb_all pause)
      if [ "$TOTAL" -ge 0 ] 2>/dev/null && [ "$EFFECT" != "no" ]; then
        MSG="🎬 Vektor — mode streaming
Jellyfin diffuse :
${DETAILS:-($N_PLAYING session(s))}

⏸️ qBittorrent PAUSE (${STOPPED}/${TOTAL} torrents) pour prioriser ta bande passante.
(seuls les torrents sont mis en pause : le LAN, les services et Jellyfin ne sont pas touchés)
Je reprendrai les téléchargements 3 min après la fin de la lecture."
        tg_send "$MSG"
        printf 'PAUSE|%s|%s\n' "$NOW" "$(printf '%s' "$DETAILS" | head -1)" >> "$EVENT_LOG"
        log "PAUSE appliquee : ${STOPPED}/${TOTAL} — notif envoyee"
      else
        # Pas de rafraichissement d'ancre : nouvelle tentative a la prochaine passe.
        log "ERREUR: authentification qBittorrent echouee (pause non appliquee, retry a la prochaine passe)"
        REFRESH_ANCHOR=0
      fi
    fi
    # Réconciliation : une action EXTERNE (humain, autre script, reboot
    # avec resume-all) peut avoir repris les torrents pendant la lecture.
    # L'ancre étant rafraîchie à chaque tick, la branche « pause ancienne »
    # ne se déclenche jamais en séance continue : sans cette vérification
    # le guard ne s'apercevrait JAMAIS d'une reprise externe (vécu le
    # 26/09 : resume all externe à 16:09, séance sans pause 16:09→17:39).
    LAST_RECON=$(cat "$STATE_DIR/stream-guard.reconcile" 2>/dev/null || echo 0)
    if [ $(( NOW - LAST_RECON )) -ge "$RECONCILE_EVERY_S" ]; then
      echo "$NOW" > "$STATE_DIR/stream-guard.reconcile"
      read -r RSTOPPED RTOTAL < <(qb_check)
      if [ "$RTOTAL" -gt 0 ] 2>/dev/null && [ "$RSTOPPED" -eq 0 ]; then
        log "RECONCILIATION : 0/${RTOTAL} stopped pendant une lecture — re-pause"
        read -r STOPPED TOTAL EFFECT < <(qb_all pause)
        if [ "$TOTAL" -ge 0 ] 2>/dev/null && [ "$EFFECT" != "no" ]; then
          tg_send "🔁 Vektor — stream-guard : les torrents avaient été repris par une action externe pendant la lecture. Je re-pause (${STOPPED}/${TOTAL})."
          printf 'PAUSE|%s|%s\n' "$NOW" "reconciliation (reprise externe détectée)" >> "$EVENT_LOG"
          log "PAUSE appliquée (réconciliation) : ${STOPPED}/${TOTAL}"
        fi
      else
        log "Réconciliation : ${RSTOPPED}/${RTOTAL} stopped, pause toujours effective"
      fi
    fi
    # Ancre rafraîchie tant que la lecture dure : l'hysteresis de reprise
    # comptera depuis la fin REELLE de la lecture, pas du debut de la pause.
    if [ "$REFRESH_ANCHOR" = "1" ]; then
      echo "paused $NOW" > "$STATE_FILE"
    fi
    log "Lecture en cours ($N_PLAYING) : deja en pause"
  fi
else
  if [ "$MODE" = "paused" ]; then
    ELAPSED=$(( NOW - SINCE ))
    if [ "$ELAPSED" -ge "$PAUSE_RELEASE_DELAY_S" ]; then
      log "Aucune lecture depuis ${ELAPSED}s : reprise qBittorrent"
      read -r STOPPED TOTAL EFFECT < <(qb_all resume)
      if [ "$TOTAL" -ge 0 ] 2>/dev/null && [ "$EFFECT" != "no" ]; then
        echo "idle $NOW" > "$STATE_FILE"
        ACTIVE=$(( TOTAL - STOPPED ))
        MSG="▶️ Vektor — fin de streaming
Plus aucune lecture Jellyfin depuis 3 min.

✅ qBittorrent RELANCE (${ACTIVE}/${TOTAL} torrents actifs)."
        tg_send "$MSG"
        # ts = SINCE = fin REELLE de la lecture (l'ancre est rafraichie a chaque
        # tick de lecture) ; le digest calcule la duree RESUME.ts - PAUSE.ts.
        printf 'RESUME|%s|%s\n' "$SINCE" "$(( ELAPSED - PAUSE_RELEASE_DELAY_S ))" >> "$EVENT_LOG"
        log "RESUME applique : ${ACTIVE}/${TOTAL} actifs (fin de lecture a $(date -d @"$SINCE" '+%T'), hysteresis ${ELAPSED}s) — notif envoyee"
      else
        log "ERREUR: authentification qBittorrent echouee (reprise non appliquee)"
      fi
    else
      log "Plus de lecture mais delai de securite non ecoule (${ELAPSED}s/${PAUSE_RELEASE_DELAY_S}s)"
    fi
  else
    log "Aucune lecture, mode $MODE : rien a faire"
  fi
fi
