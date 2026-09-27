#!/bin/bash
# vektor-docker-updates — scan / apply des mises à jour d'images Docker.
#
#   scan  : docker compose pull de chaque stack compose-managée (CT 103/104),
#           puis liste des conteneurs dont l'ID image courant diffère de
#           l'ID fraîchement pullé -> « MAJ disponible » ou « à jour ».
#           NE REDÉMARRE RIEN.
#   list  : comme scan (pull + comparaison des digests distants) mais
#           affiche PILE PAR PILE chaque conteneur : tag courant de son
#           image vs digest de la dernière version disponible. Rapport
#           lecture seule (/majlist) : n'alimente ni OUI ni apply.
#   apply : pour chaque stack avec au moins un conteneur à image plus récente
#           (hors blacklist), docker compose up -d (recrée avec la nouvelle
#           image). Compose-only : qbittorrent (hors compose) jamais touché.
#
# Sécurité :
#   - CT whitelistés : 103 (arr-stack), 104 (tools). Rien d'autre.
#   - Blacklist de noms : authelia, portainer, healthchecks, sftpgo.
#   - Garde-fou stream (apply) : refuse si une lecture Jellyfin est en cours
#     (état du stream-guard, rafraîchi à chaque tick 60 s).
#   - Appelé uniquement par vektor-actions (canal SSH verrouillé).
# Design : BLACKLIST/MODE voyagent par ENVIRONNEMENT (hérité par pct exec),
# zéro substitution de chaîne dans le snippet (gotcha quotes).
set -u
export MODE="${1:-scan}"

case "$MODE" in
  scan|apply|list) ;;
  *) echo "REFUS: mode inconnu (scan|apply)"; exit 1 ;;
esac

CTS="103 104"
export BLACKLIST='^(authelia|portainer|healthchecks|sftpgo)$'

if [ "$MODE" = "apply" ]; then
  # Garde-fou stream : le stream-guard écrit son état à chaque tick.
  if [ -f /var/lib/vektor/stream-guard-sess.json ]; then
    NPLAYING=$(python3 -c "import json;print(len([s for s in json.load(open('/var/lib/vektor/stream-guard-sess.json')) if s.get('NowPlayingItem')]))" 2>/dev/null || echo 0)
    [ "${NPLAYING:-0}" -gt 0 ] && { echo "REFUS: lecture Jellyfin en cours ($NPLAYING stream) — réessaie plus tard"; exit 1; }
  fi
fi

DISCOVER='
for d in $(docker ps --format "{{.Names}}" | while read n; do
    docker inspect "$n" -f "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}" 2>/dev/null
  done | sort -u); do
  [ -f "$d/docker-compose.yml" ] || [ -f "$d/compose.yml" ] || continue
  cd "$d" 2>/dev/null && echo "$d"
done'

SNIPPET='
for d in $(docker ps --format "{{.Names}}" | while read n; do
    docker inspect "$n" -f "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}" 2>/dev/null
  done | sort -u); do
  [ -f "$d/docker-compose.yml" ] || [ -f "$d/compose.yml" ] || continue
  base=$(basename "$d")
  cd "$d" 2>/dev/null || continue
  if [ "$MODE" = "list" ]; then
    # Rapport detaille : tag courant vs digest de la derniere image dispo.
    # Meme semantique que scan : apres pull, un tag qui ne pointe plus vers
    # l image du conteneur signale une version plus recente disponible.
    for n in $(docker compose ps --quiet 2>/dev/null); do
      name=$(docker inspect "$n" -f "{{.Name}}" | sed "s|^/||")
      echo "$name" | grep -qiE "$BLACKLIST" && continue
      src=$(docker inspect "$n" -f "{{.Config.Image}}")
      version=${src#*:}
      [ "$version" = "$src" ] && version=latest
      img=$(docker inspect "$n" -f "{{.Image}}")
      newid=$(docker image inspect "$src" -f "{{.Id}}" 2>/dev/null)
      if [ -z "$newid" ]; then
        echo "    $name : $version (image introuvable localement)"
      elif [ "$img" = "$newid" ]; then
        echo "    $name : $version (a jour)"
      else
        digest=$(docker image inspect "$newid" -f "{{index .RepoDigests 0}}" 2>/dev/null | sed "s|^.*@||; s|^sha256:||; s|^\(............\).*|\1|")
        echo "    $name : $version -> NOUVELLE IMAGE dispo (digest ${digest:-?})"
      fi
    done
    continue
  fi
  todo=""
  for n in $(docker compose ps --quiet 2>/dev/null); do
    name=$(docker inspect "$n" -f "{{.Name}}" | sed "s|^/||")
    echo "$name" | grep -qiE "$BLACKLIST" && continue
    img=$(docker inspect "$n" -f "{{.Image}}")
    src=$(docker inspect "$n" -f "{{.Config.Image}}")
    newid=$(docker image inspect "$src" -f "{{.Id}}" 2>/dev/null)
    [ -n "$newid" ] && [ "$img" != "$newid" ] && todo="$todo $name"
  done
  if [ -z "$todo" ]; then
    echo "  $base : a jour"
  elif [ "$MODE" = "scan" ]; then
    echo "  $base : MAJ disponible ->$todo"
  else
    echo "  $base : mise a jour de$todo"
    docker compose up -d 2>&1 | tail -2
  fi
done
'

for CT in $CTS; do
  echo "=== CT $CT ==="
  if [ "$MODE" != "apply" ]; then
    # Pull de toutes les stacks (silencieux) : met à jour les refs locales
    pct exec "$CT" -- bash -c "$DISCOVER" 2>/dev/null | while IFS= read -r d; do
      pct exec "$CT" -- bash -c "cd '$d' 2>/dev/null && docker compose pull --quiet" >/dev/null 2>&1
    done
  fi
  pct exec "$CT" -- bash -c "$SNIPPET" 2>&1 | grep -v "^WARNING"
done
echo "=== FIN ($MODE) ==="
