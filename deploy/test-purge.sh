#!/usr/bin/env bash
# test-purge.sh — test shell de la logique de purge de vektor-deploy.sh.
#
# Extrait le VRAI bloc « ── 6. Purge » du script de déploiement (pas une
# copie qui peut diverger) et l'exécute sur des fixtures : fausses releases
# datées (mtime), un .staging-* et un dossier non-v*. 4 cas :
#   1. nominal         : garde les 3 dernières, purge les plus anciennes
#   2. courante hors 3 : la release courante (pointeur version) est épargnée
#                        même si elle sort du top-3 mtime (plan de rollback)
#   3. peu de releases : rien à purger, aucune erreur
#   4. répertoire vide : aucune erreur (ls sans match)
# Utilisé par la CI (.github/workflows/ci.yml) ; exécutable aussi à la main :
#   bash deploy/test-purge.sh
set -euo pipefail

cd "$(dirname "$0")"

[[ -f vektor-deploy.sh ]] || { echo "FAIL: vektor-deploy.sh introuvable"; exit 1; }
bash -n vektor-deploy.sh || { echo "FAIL: erreur de syntaxe dans vektor-deploy.sh"; exit 1; }

# Extraction du bloc de purge réel. Si la structure du script change, ce
# test échoue : il faut alors mettre à jour le marqueur en même temps.
purge_block=$(awk '/6\. Purge/{f=1} f{print} f && /done < </{exit}' vektor-deploy.sh)
[[ -n "$purge_block" ]] || { echo "FAIL: bloc de purge introuvable (marqueur « 6. Purge »)"; exit 1; }

log() { echo "[test] $*"; }

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT

pass=0
fail=0

# Prépare un bac à sable vierge, pose les variables attendues par le bloc
# (RELEASES_DIR, KEEP_RELEASES, current) puis exécute le bloc extrait.
run_case() {
  local name="$1" current_rel="$2" entry
  shift 2
  RELEASES_DIR="$TMPROOT/$name/releases"
  KEEP_RELEASES=3
  current="$current_rel"
  mkdir -p "$RELEASES_DIR"
  for entry in "$@"; do
    mkdir -p "$RELEASES_DIR/${entry%%:*}"
    touch -t "${entry##*:}" "$RELEASES_DIR/${entry%%:*}"
  done
  eval "$purge_block" >/dev/null
}

assert_survivors() {
  local name="$1" expected actual
  shift
  expected=$(printf '%s\n' "$@" | sort)
  actual=$(ls -1A "$RELEASES_DIR" | sort)
  if [[ "$expected" == "$actual" ]]; then
    echo "PASS: $name"
    pass=$((pass + 1))
  else
    echo "FAIL: $name"
    echo "  attendu: $(echo "$expected" | tr '\n' ' ')"
    echo "  obtenu : $(echo "$actual" | tr '\n' ' ')"
    fail=$((fail + 1))
  fi
}

# Cas 1 — nominal : les 3 dernières gardées ; .staging-* et non-v* intacts.
run_case cas1 v1.5.7 \
  v1.5.3:202609220900 v1.5.4:202609230900 v1.5.5:202609240900 \
  v1.5.6:202609250900 v1.5.7:202609260900 \
  .staging-42:202609260900 knowledge-live:202609260900
assert_survivors cas1 v1.5.5 v1.5.6 v1.5.7 .staging-42 knowledge-live

# Cas 2 — la courante (v1.5.3) sort du top-3 mtime : épargnée quand même.
run_case cas2 v1.5.3 \
  v1.5.3:202609220900 v1.5.4:202609230900 v1.5.5:202609240900 \
  v1.5.6:202609250900 v1.5.7:202609260900 .staging-42:202609260900
assert_survivors cas2 v1.5.3 v1.5.5 v1.5.6 v1.5.7 .staging-42

# Cas 3 — moins de KEEP releases : rien à purger.
run_case cas3 v1.5.7 v1.5.6:202609250900 v1.5.7:202609260900
assert_survivors cas3 v1.5.6 v1.5.7

# Cas 4 — répertoire vide : aucune erreur.
run_case cas4 v1.5.7
assert_survivors cas4

echo
if (( fail == 0 )); then
  log "OK : $pass cas verts"
  exit 0
else
  log "ÉCHEC : $fail cas en erreur"
  exit 1
fi
