#!/usr/bin/env python3
"""Patch idempotent de qbit_api_alive() dans vektor-stream-guard.

Bug : la fonction faisait `curl -o /dev/null` (sortie TOUJOURS vide, même
en cas de succès) et le test `[ -z "$(qbit_api_alive)" ]` la déclarait donc
muette à chaque cycle => restart de qBit toutes les 10 minutes.

Fix : capturer le code HTTP (`-w '%{http_code}'`) et comparer à 200.
"""
import sys

PATH = "/usr/local/bin/vektor-stream-guard"

OLD_FUNC = '''qbit_api_alive() {
  pct exec 107 -- docker exec qbittorrent sh -c \\
    "curl -s -o /dev/null --max-time $CURL_TIMEOUT -b SID=bypass-lan-whitelist http://localhost:8080/api/v2/app/version" 2>/dev/null
}'''

NEW_FUNC = '''qbit_api_alive() {
  pct exec 107 -- docker exec qbittorrent sh -c \\
    "curl -s -o /dev/null --max-time $CURL_TIMEOUT -w '%{http_code}' -b SID=bypass-lan-whitelist http://localhost:8080/api/v2/app/version" 2>/dev/null
}'''

OLD_MUTED = 'if [ -z "$(qbit_api_alive)" ]; then'
NEW_MUTED = 'if [ "$(qbit_api_alive)" != "200" ]; then'
OLD_ALIVE = 'if [ -n "$(qbit_api_alive)" ]; then'
NEW_ALIVE = 'if [ "$(qbit_api_alive)" = "200" ]; then'


def main() -> int:
    with open(PATH) as f:
        s = f.read()

    if NEW_MUTED in s or NEW_ALIVE in s:
        print("deja patche")
        return 0

    assert OLD_FUNC in s, "fonction qbit_api_alive introuvable"
    s = s.replace(OLD_FUNC, NEW_FUNC, 1)

    n_muted = s.count(OLD_MUTED)
    n_alive = s.count(OLD_ALIVE)
    s = s.replace(OLD_MUTED, NEW_MUTED)
    s = s.replace(OLD_ALIVE, NEW_ALIVE)

    with open(PATH, "w") as f:
        f.write(s)
    print(f"patche : fonction + {n_muted} test(s) muet + {n_alive} test(s) vivant")
    return 0


if __name__ == "__main__":
    sys.exit(main())
