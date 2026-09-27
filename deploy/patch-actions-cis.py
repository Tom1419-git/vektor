#!/usr/bin/env python3
"""Patch idempotent de vektor-actions : ajoute l'action pegaprox_cis.

pegaprox_cis -> /usr/local/bin/pegaprox-cis-check (audit CIS-L1 PegaProx
lecture seule + detection de regression, pour le rapport matinal).
"""
import sys

PATH = "/usr/local/bin/vektor-actions"

OLD_TAIL = "docker_updates_(scan|apply|list))$\""
NEW_TAIL = "docker_updates_(scan|apply|list)|pegaprox_cis)$\""

ANCHOR_CASE = "  lxc_snapls)"
NEW_CASE = """  pegaprox_cis)
    if [ -x /usr/local/bin/pegaprox-cis-check ]; then
      /usr/local/bin/pegaprox-cis-check
    else
      echo "ERREUR: script pegaprox-cis-check absent"
      exit 1
    fi
    ;;
  lxc_snapls)"""


def main() -> int:
    with open(PATH) as f:
        s = f.read()

    if "pegaprox_cis" in s:
        print("deja patche")
        return 0

    assert OLD_TAIL in s, "regex whitelist introuvable"
    s = s.replace(OLD_TAIL, NEW_TAIL, 1)

    assert ANCHOR_CASE in s, "anchor case lxc_snapls introuvable"
    s = s.replace(ANCHOR_CASE, NEW_CASE, 1)

    with open(PATH, "w") as f:
        f.write(s)
    print("patche : regex + case pegaprox_cis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
