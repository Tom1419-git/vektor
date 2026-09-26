#!/usr/bin/env bash
# vektor-deploy.sh — déploiement pull-based de Vektor depuis GitHub.
#
# MODÈLE (aucun secret chez GitHub, aucune clé SSH privée dans les
# secrets du dépôt) :
#   - Le VPS tire HTTPS (public) : github -> VPS, jamais l'inverse.
#   - Ne déploie qu'un tag v* dont la CI GitHub est verte (API publique,
#     sans authentification).
#   - Jamais touché : /opt/vektor/.env (secrets), secrets/, knowledge-live/
#     (contenu live remplacé au besoin depuis le repo knowledge/).
#   - Rétention : les KEEP_RELEASES dernières releases sont conservées
#     dans /opt/vektor-releases/<tag>, les plus anciennes purgées après
#     chaque déploiement réussi (jamais la release courante, plan de
#     rollback — voir DEPLOIEMENT.md).
#
# Installation (VPS) :
#   install -m 0755 deploy/vektor-deploy.sh /usr/local/bin/vektor-deploy
#   cp deploy/vektor-deploy.timer deploy/vektor-deploy.service /etc/systemd/system/
#   systemctl daemon-reload && systemctl enable --now vektor-deploy.timer
#
# Manuel : vektor-deploy [--force]   (--force : déploie HEAD même sans tag)

set -euo pipefail

REPO_SLUG="Tom1419-git/vektor"
DEPLOY_DIR="/opt/vektor"
RELEASES_DIR="/opt/vektor-releases"
COMPOSE_FILE="compose.yml"
API_CONTAINER="vektor-api"
LOCK_FILE="/run/vektor-deploy.lock"
HEALTH_TIMEOUT=90
KEEP_RELEASES=3

log() { echo "[vektor-deploy] $(date '+%F %T') $*"; }

# L'API ne publie aucun port vers l'hôte (Caddy passe par le réseau
# Docker) : le health check s'exécute DANS le conteneur, en loopback.
health_ok() {
  docker exec "$API_CONTAINER" python - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5)
PY
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another deploy is running, exiting"
  exit 0
fi

force=0
[[ "${1:-}" == "--force" ]] && force=1

# Version courante (pointeur écrit après health OK) : sert au skip des
# tours inutiles ET à épargner la release de rollback pendant la purge.
current=""
[[ -f "$DEPLOY_DIR/version" ]] && current=$(cat "$DEPLOY_DIR/version")

command -v docker >/dev/null || { log "docker absent"; exit 1; }
command -v curl >/dev/null || { log "curl absent"; exit 1; }
command -v jq >/dev/null || { log "jq absent (apt install jq)"; exit 1; }
[[ -f "$DEPLOY_DIR/.env" ]] || { log ".env absent — abort"; exit 1; }

# ── 1. Tag cible : le plus récent, CI verte, pas encore déployé ─────────
tag=""
if [[ $force -eq 1 ]]; then
  tag="HEAD"
  log "--force : déploiement de HEAD (sans vérification CI)"
else
  latest_tag=$(curl -fsS --max-time 15 \
    "https://api.github.com/repos/$REPO_SLUG/tags?per_page=5" \
    | jq -r '[.[].name | select(test("^v[0-9]"))][0] // empty')
  [[ -n "$latest_tag" ]] || { log "aucun tag v* trouvé"; exit 0; }

  if [[ "$latest_tag" == "$current" ]]; then
    log " déjà déployé ($current) — rien à faire"
    exit 0
  fi

  ci_state=$(curl -fsS --max-time 15 \
    "https://api.github.com/repos/$REPO_SLUG/commits/$latest_tag/check-runs" \
    | jq -r '[.check_runs[].conclusion] | if length == 0 then "none" else (if all(. == "success") then "success" else "not-green" end) end')
  case "$ci_state" in
    success) log "CI verte sur $latest_tag → déploiement" ;;
    none)    log "CI pas encore lancée sur $latest_tag — retentera au prochain tour"; exit 0 ;;
    *)       log "CI PAS VERTE sur $latest_tag ($ci_state) — pas de déploiement"; exit 0 ;;
  esac
  tag="$latest_tag"
fi

# ── 2. Télécharger et préparer le répertoire du release ────────────────
stage="$RELEASES_DIR/.staging-$$"
release_dir="$RELEASES_DIR/$tag"
mkdir -p "$stage"
cleanup() { rm -rf "$stage"; }
trap cleanup EXIT

log "téléchargement du tarball $tag"
curl -fL --max-time 120 -o "$stage/src.tar.gz" \
  "https://github.com/$REPO_SLUG/archive/refs/tags/$tag.tar.gz" \
  || { log "téléchargement impossible — abort"; exit 1; }
tar -xzf "$stage/src.tar.gz" -C "$stage"
src=$(echo "$stage"/*/)

mkdir -p "$RELEASES_DIR"
rm -rf "$release_dir"
mv "$src" "$release_dir"
echo "$tag" > "$release_dir/version"
chmod +x "$release_dir/deploy/vektor-deploy.sh" 2>/dev/null || true

# ── 3. Copier les pièces à ne jamais retélécharger ──────────────────────
# .env (secrets) et secrets/ (clés) vivent dans /opt/vektor, jamais dans
# git. knowledge-live/ est régénéré depuis knowledge/ du repo.
install -m 600 "$DEPLOY_DIR/.env" "$release_dir/.env"
[[ -d "$DEPLOY_DIR/secrets" ]] && cp -a "$DEPLOY_DIR/secrets" "$release_dir/secrets"
rm -rf "$release_dir/knowledge-live"
mkdir -p "$release_dir/knowledge-live"
cp -a "$DEPLOY_DIR/knowledge-live/." "$release_dir/knowledge-live/" 2>/dev/null || \
  cp -a "$release_dir/knowledge/." "$release_dir/knowledge-live/"
chmod 600 "$release_dir/.env"

# ── 4. Basculer et rebuild ──────────────────────────────────────────────
log "rebuild + recreate des conteneurs depuis $release_dir"
if ! (cd "$release_dir" && docker compose -p vektor -f "$COMPOSE_FILE" build --pull \
      && docker compose -p vektor -f "$COMPOSE_FILE" --profile telegram up -d); then
  log "ÉCHEC du build/up — ancienne version laissée en place"
  exit 1
fi

# ── 5. Health check post-deploy ─────────────────────────────────────────
log "health check (< ${HEALTH_TIMEOUT}s)"
healthy=0
for _ in $(seq 1 $((HEALTH_TIMEOUT / 5))); do
  if health_ok; then
    healthy=1; break
  fi
  sleep 5
done

if [[ $healthy -eq 1 ]]; then
  # Pointeur de version courante : c'est lui qui empêche le re-déploiement
  # du même tag à chaque tour du watcher. SANS lui, le watcher reboucle.
  echo "$tag" > "$DEPLOY_DIR/version"
  log "✅ $tag déployé et healthy"
  # Conteneurs orphelins d'anciennes versions : nettoyage best-effort
  (cd "$release_dir" && docker compose -p vektor -f "$COMPOSE_FILE" up -d --remove-orphans) >/dev/null 2>&1 || true

  # ── 6. Purge des anciennes releases (garder les KEEP_RELEASES dernières) ─
  # Uniquement après un deploy RÉUSSI (le chemin rollback ci-dessus peut
  # encore avoir besoin d'une ancienne release). La release courante est
  # épargnée explicitement : mtime et ordre logique peuvent diverger.
  while read -r old; do
    base=$(basename "$old")
    if [[ "$base" != "${current:-}" ]]; then
      log "purge ancienne release $base"
      rm -rf "$old"
    fi
  done < <(ls -1dt "$RELEASES_DIR"/v* 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)))
else
  log "🔴 health check KO après $HEALTH_TIMEOUT s — ROLLBACK automatique"
  if [[ -n "${current:-}" && -d "$RELEASES_DIR/$current" ]]; then
    (cd "$RELEASES_DIR/$current" && docker compose -p vektor -f "$COMPOSE_FILE" --profile telegram up -d) \
      && log "rollback vers $current effectué" \
      || log "ÉCHEC DU ROLLBACK — intervention manuelle requise (recreate depuis $RELEASES_DIR/$current)"
  else
    log "pas de release précédente connue — intervention manuelle requise"
  fi
  exit 1
fi
