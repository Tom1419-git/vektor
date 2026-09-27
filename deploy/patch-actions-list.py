#!/usr/bin/env python3
"""Patch idempotent de vektor-actions : autorise docker_updates_list.

Le case docker_updates_*) route deja tous les modes vers le script ; seule
la regex whitelist (commande SSH forcee) doit elargir scan|apply -> +list.
"""
import sys

PATH = "/usr/local/bin/vektor-actions"

OLD = "docker_updates_(scan|apply)"
NEW = "docker_updates_(scan|apply|list)"


def main() -> int:
    with open(PATH) as f:
        s = f.read()

    if "docker_updates_(scan|apply|list)" in s:
        print("deja patche")
        return 0

    assert OLD in s, "regex scan|apply introuvable"
    s = s.replace(OLD, NEW, 1)

    with open(PATH, "w") as f:
        f.write(s)
    print("patche : whitelist regex +list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
