#!/usr/bin/env python3
"""Patch failover du Caddyfile VPS (inode-safe, idempotent, restaurable).

- Insère un bloc handle_errors 502/503/504 dans le snippet (access_log) :
  les 27 sites qui l'importent servent alors une page de garde statique
  (fichier /config/maintenance/index.html monté dans le conteneur) sur
  erreur d'infrastructure, SANS masquer l'état réel (status 502 conservé
  pour que Kuma/les moniteurs continuent de voir DOWN et alertent Vektor).
- Ajoute un site racine mayoraz-net.ch (page de maintenance file_server)
  uniquement si le domaine se résout en DNS.
- Idempotent : réexécution sans effet si déjà patché.
- Écriture in-place (open(w)+fsync) : le Caddyfile est un bind-mount
  FICHIER dans le conteneur — JAMAIS de rename/os.replace (piège inode
  du 03/10 matin).
- La validation `caddy validate` et le reload sont orchestrés par
  l'appelant ; en cas de validate KO, restaurer avec --restore <backup>.

Usage :
  python3 patch-caddy-failover.py                     # applique
  python3 patch-caddy-failover.py --restore <backup>  # restaure in-place
"""
import os
import socket
import sys
from pathlib import Path

CADDYFILE = Path("/opt/caddy/Caddyfile")

HANDLE_ERRORS = [
    "\thandle_errors 502 503 504 {",
    "\t\trewrite * /index.html",
    "\t\tfile_server {",
    "\t\t\troot /config/maintenance",
    "\t\t}",
    "\t}",
]

APEX = [
    "",
    "mayoraz-net.ch {",
    "\timport access_log",
    "\timport comp",
    "\ttls internal",
    "\troot * /config/maintenance",
    "\tfile_server",
    "}",
]


def write_in_place(path: Path, content: str) -> None:
    """Bind-mount fichier : truncate+write = même inode. Jamais de rename."""
    with open(path, "w") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--restore":
        backup = Path(sys.argv[2])
        write_in_place(CADDYFILE, backup.read_text())
        print(f"RESTAURE: {backup} -> {CADDYFILE} (in-place, inode préservée)")
        return 0

    text = CADDYFILE.read_text()
    changed = []

    if "handle_errors" in text:
        print("handle_errors: déjà présent, insertion ignorée")
    else:
        lines = text.split("\n")
        start = next(
            (i for i, ln in enumerate(lines) if ln.strip() == "(access_log) {"),
            None,
        )
        if start is None:
            print("ERREUR: snippet (access_log) introuvable — aucun changement")
            return 1
        depth = 0
        end = None
        for i in range(start, len(lines)):
            depth += lines[i].count("{") - lines[i].count("}")
            if depth == 0:
                end = i
                break
        if end is None:
            print("ERREUR: accolades déséquilibrées dans (access_log)")
            return 1
        lines[end:end] = HANDLE_ERRORS
        text = "\n".join(lines)
        changed.append(f"handle_errors 502/503/504 inséré dans (access_log) avant la ligne {end + 1}")

    if "\nmayoraz-net.ch {" in text or text.startswith("mayoraz-net.ch {"):
        print("site racine: déjà présent")
    else:
        try:
            ip = socket.gethostbyname("mayoraz-net.ch")
        except OSError:
            print("site racine: IGNORÉ (mayoraz-net.ch ne se résout pas en DNS)")
        else:
            text = text.rstrip("\n") + "\n" + "\n".join(APEX) + "\n"
            changed.append(f"site racine mayoraz-net.ch ajouté (DNS -> {ip})")

    if not changed:
        print("aucun changement")
        return 0

    write_in_place(CADDYFILE, text)
    for item in changed:
        print("OK:", item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
