#!/bin/bash
# pegaprox-cis-check — audit CIS-L1 PegaProx en lecture seule + détection
# de régression. Appelé par vektor-actions (canal SSH verrouillé), pensé
# pour la section « compliance » du rapport matinal de Vektor.
#
#   - Login API PegaProx (CT 110, https://192.168.1.110:5000) avec les
#     creds de /root/.pegaprox-api (600, deux lignes PEGA_USER/PEGA_PASS).
#   - GET /api/clusters/acc66e98/nodes/<node>/hardening?profile=cis-l1
#   - Compte pass/fail, compare a l'etat precedent
#     (/var/lib/vektor/pegaprox-cis-state.json) et signale toute
#     regression (un controle qui repasse en echec) ou amelioration.
#   - L'etat n'est ecrit QUE si le scan a reussi (jamais sur erreur) et
#     ce script ne MODIFIE jamais le systeme : le POST hardening de
#     PegaProx n'est jamais appele.
set -u

BASE="https://192.168.1.110:5000"
CLUSTER="acc66e98"
PROFILE="cis-l1"
STATE_DIR=/var/lib/vektor
STATE_FILE=$STATE_DIR/pegaprox-cis-state.json
CRED_FILE=/root/.pegaprox-api
CURL_TIMEOUT=15

mkdir -p "$STATE_DIR"
[ -r "$CRED_FILE" ] || { echo "CIS-L1 PegaProx : audit indisponible (creds absentes)"; exit 0; }
PEGA_USER=$(sed -n 's/^PEGA_USER=//p' "$CRED_FILE" | tr -d '"')
PEGA_PASS=$(sed -n 's/^PEGA_PASS=//p' "$CRED_FILE" | tr -d '"')
[ -n "$PEGA_USER" ] && [ -n "$PEGA_PASS" ] || { echo "CIS-L1 PegaProx : audit indisponible (creds invalides)"; exit 0; }

REQ=/tmp/pp-cis-req.$$.json
COOKIE=/tmp/pp-cis-ck.$$.txt
trap 'rm -f "$REQ" "$COOKIE"' EXIT
python3 - "$REQ" "$PEGA_USER" "$PEGA_PASS" <<'PYEOF'
import json, sys
json.dump({"username": sys.argv[2], "password": sys.argv[3]}, open(sys.argv[1], "w"))
PYEOF

LOGIN=$(curl -sk --max-time $CURL_TIMEOUT -c "$COOKIE" -X POST "$BASE/api/auth/login" \
  -H "Content-Type: application/json" -H "X-Requested-With: XMLHttpRequest" \
  --data @"$REQ" -w "\n%{http_code}" 2>/dev/null)
LOGIN_HTTP=$(printf '%s' "$LOGIN" | tail -n 1)
[ "$LOGIN_HTTP" = "200" ] || { echo "CIS-L1 PegaProx : audit indisponible (login HTTP ${LOGIN_HTTP:-vide})"; exit 0; }

NODE="${PEGA_NODE:-pve}"   # mono-noeud : GET /api/clusters/<id> = 405 (route POST only)

SCAN=$(curl -sk --max-time 60 -b "$COOKIE" -H "X-Requested-With: XMLHttpRequest" \
  "$BASE/api/clusters/$CLUSTER/nodes/$NODE/hardening?profile=$PROFILE&verbose=true" 2>/dev/null)
[ -n "$SCAN" ] || { echo "CIS-L1 PegaProx : audit indisponible (scan vide)"; exit 0; }

python3 - "$SCAN" "$STATE_FILE" <<'PYEOF'
import json, sys, time, os

try:
    d = json.loads(sys.argv[1])
    ctrls = d.get("controls") or {}
    states = [bool(c.get("status")) for c in ctrls.values()]
    if not states:
        raise ValueError("pas de statuts")
except Exception:
    print("CIS-L1 PegaProx : audit indisponible (reponse illisible)")
    raise SystemExit

total = len(states)
n_pass = sum(states)
n_fail = total - n_pass
fail_ids = sorted(k for k, c in ctrls.items() if not c.get("status"))

state_path = sys.argv[2]
prev = None
if os.path.exists(state_path):
    try:
        prev = json.load(open(state_path))
    except Exception:
        prev = None

def save():
    try:
        json.dump({"ts": int(time.time()), "pass": n_pass, "fail": n_fail,
                   "total": total, "fails": fail_ids}, open(state_path, "w"))
    except Exception:
        pass

if not prev or "fail" not in prev:
    save()
    print(f"OK|🛡️ CIS-L1 PegaProx : {n_pass}/{total} pass, {n_fail} fail (premier audit — baseline enregistrée)")
    raise SystemExit

old_fail = int(prev.get("fail", n_fail))
old_pass = int(prev.get("pass", n_pass))
old_fails = set(prev.get("fails") or [])
newly = [k for k in fail_ids if k not in old_fails]
fixed = sorted(old_fails - set(fail_ids))
save()
try:
    age_h = int((time.time() - int(prev.get("ts", time.time()))) / 3600)
except Exception:
    age_h = 0
since = f" (dernier audit il y a ~{age_h}h)" if age_h >= 1 else ""

if newly:
    print(f"RED|🔴 CIS-L1 PegaProx EN RÉGRESSION : {n_pass}/{total} pass{since} (avant : {old_pass}/{total})")
    print(f"RED|   Nouvellement en échec : {', '.join(newly[:6])}" + (" …" if len(newly) > 6 else ""))
    if fixed:
        print(f"RED|   (mais {len(fixed)} contrôles repassés au vert : {', '.join(fixed[:4])})")
elif n_fail < old_fail:
    print(f"OK|🟢 CIS-L1 PegaProx amélioré : {n_fail} fail{since} contre {old_fail} avant — au vert : {', '.join(fixed[:6])}")
elif n_fail == old_fail:
    print(f"OK|🛡️ CIS-L1 PegaProx : {n_pass}/{total} pass{since} — stable")
else:
    print(f"OK|🛡️ CIS-L1 PegaProx : {n_pass}/{total} pass{since} (total de contrôles modifié)")
PYEOF
exit 0
