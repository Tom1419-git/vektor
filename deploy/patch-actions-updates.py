#!/usr/bin/env python3
"""Patch idempotent de vektor-actions : ajoute les actions docker_updates_*.

  docker_updates_scan               -> scan (pull + compare) CT 103/104
  docker_updates_apply              -> apply (compose up -d) CT 103/104

Ajoute au : regex whitelist + case d'exécution. Timeout généreux (le scan
pull de vraies images : plusieurs minutes possibles).
"""
import sys

PATH = "/usr/local/bin/vektor-actions"

OLD_TAIL = "lxc_snapls)$\""
NEW_TAIL = "lxc_snapls|docker_updates_(scan|apply))$\""

ANCHOR_CASE = "  lxc_snapls)"
NEW_CASE = """  docker_updates_*)
    MODE="${ACTION#docker_updates_}"
    if [ -x /usr/local/bin/vektor-docker-updates ]; then
      /usr/local/bin/vektor-docker-updates "$MODE"
    else
      echo "ERREUR: script vektor-docker-updates absent"
      exit 1
    fi
    ;;
  lxc_snapls)"""


def main() -> int:
    with open(PATH) as f:
        s = f.read()

    if "docker_updates_" in s:
        print("deja patche")
        return 0

    assert OLD_TAIL in s, "regex whitelist introuvable"
    s = s.replace(OLD_TAIL, NEW_TAIL, 1)

    assert ANCHOR_CASE in s, "anchor case lxc_snapls introuvable"
    s = s.replace(ANCHOR_CASE, NEW_CASE, 1)

    with open(PATH, "w") as f:
        f.write(s)
    print("patche : regex + case docker_updates_")
    return 0


if __name__ == "__main__":
    sys.exit(main())
