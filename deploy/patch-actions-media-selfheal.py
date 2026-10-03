#!/usr/bin/env python3
"""Patch idempotent de vektor-actions pour l'auto-réparation média.

Installer préalablement le handler en /usr/local/bin/vektor-media-selfheal.
Ce patch ajoute seulement des tokens fixes et ne lance aucune réparation.
"""
import sys

PATH = "/usr/local/bin/vektor-actions"
OLD_TAIL = (
    "|diagnose_(pve|net|media|services|failover|disk_(101|102|103|104|105|106|107|108|109|110|111)"
    "|docker_(101|102|103|104|105|106|107|108|109|110|111)))$\""
)
NEW_TAIL = (
    "|diagnose_(pve|net|media|services|failover|disk_(101|102|103|104|105|106|107|108|109|110|111)"
    "|docker_(101|102|103|104|105|106|107|108|109|110|111))"
    "|selfheal_media_(jellyfin|immich|authelia|reset_jellyfin|reset_immich|reset_authelia))$\""
)
MEDIA_OLD = '|selfheal_media_(jellyfin|immich|reset_jellyfin|reset_immich))$"'
MEDIA_NEW_GROUP = '|selfheal_media_(jellyfin|immich|authelia|reset_jellyfin|reset_immich|reset_authelia))$"'
PATCHED_MARKER = "selfheal_media_(jellyfin|immich|authelia|reset_jellyfin|reset_immich|reset_authelia)"
ANCHOR = "  diagnose_*)"
CASE = """  selfheal_media_*)
    if [ -x /usr/local/bin/vektor-media-selfheal ]; then
      /usr/local/bin/vektor-media-selfheal "$ACTION"
    else
      echo "ERROR: script vektor-media-selfheal absent"
      exit 1
    fi
    ;;
  diagnose_*)"""


def main() -> int:
    with open(PATH) as f:
        source = f.read()
    if PATCHED_MARKER in source:
        print("deja patche")
        return 0
    if MEDIA_OLD in source:
        # Whitelist média v1 déjà posée : ajouter seulement authelia aux tokens.
        assert ANCHOR in source, "case selfheal_media_*/diagnose_* introuvable"
        source = source.replace(MEDIA_OLD, MEDIA_NEW_GROUP, 1)
    else:
        assert OLD_TAIL in source, "fin de regex diagnose/whitelist introuvable"
        assert ANCHOR in source, "case diagnose_* introuvable"
        source = source.replace(OLD_TAIL, NEW_TAIL, 1)
        source = source.replace(ANCHOR, CASE, 1)
    with open(PATH, "w") as f:
        f.write(source)
    print("patche : whitelist média (authelia incluse) + dispatch contrôlé")
    return 0


if __name__ == "__main__":
    sys.exit(main())
