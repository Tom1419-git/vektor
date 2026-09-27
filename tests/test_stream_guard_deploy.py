"""Régression : la copie versionnée du stream-guard embarque le fix infirmier.

Bug v1.6.3 : qbit_api_alive() faisait `curl -o /dev/null` SANS -w '%{http_code}'
(sortie toujours vide) et le test `[ -z ... ]` déclarait donc qBit muet à
chaque cycle => restart de qBit toutes les 10 minutes. Le fix doit rester
embarqué dans deploy/vektor-stream-guard.sh (source de vérité, installée par
scp sur pve:/usr/local/bin/vektor-stream-guard) — jamais seulement via un
patch in-situ (patch-nurse-qbit.py = historique d'avant versioning).
"""

import shutil
import subprocess
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "deploy" / "vektor-stream-guard.sh"


def _script() -> str:
    assert GUARD.exists(), f"{GUARD} absent"
    return GUARD.read_text(encoding="utf-8")


def test_shebang_en_premiere_ligne():
    """Un en-tête placé AVANT le shebang rend le script non exécutable
    (Exec format error, status=203/EXEC) : le guard systemd meurt au
    premier tick. Vécu le 27/09 lors du versioning — régression interdite."""
    assert _script().splitlines()[0] == "#!/bin/bash"


def test_syntaxe_bash_valide():
    assert shutil.which("bash"), "bash requis"
    proc = subprocess.run(["bash", "-n", str(GUARD)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_qbit_api_alive_capture_le_code_http():
    """Le fix : -w '%{http_code}' dans le curl de qbit_api_alive."""
    s = _script()
    start = s.index("qbit_api_alive() {")
    end = s.index("}", s.index("http://localhost:8080/api/v2/app/version", start))
    func = s[start:end]
    assert "-w '%{http_code}'" in func, "qbit_api_alive doit capturer le code HTTP"
    assert "-o /dev/null" in func  # sortie muette voulue, capture via -w


def test_aucun_test_vacuum_sur_la_sortie_de_qbit_api_alive():
    """Les comparaisons -z/-n sur qbit_api_alive = le bug v1.6.3 revient."""
    s = _script()
    assert '[ -z "$(qbit_api_alive)" ]' not in s
    assert '[ -n "$(qbit_api_alive)" ]' not in s
    # Et les deux branches de l'infirmier comparent bien au code HTTP :
    assert '[ "$(qbit_api_alive)" != "200" ]' in s  # muet -> restart
    assert '[ "$(qbit_api_alive)" = "200" ]' in s   # vivant -> pas d'action


def test_infirmier_embarque_avec_garde_fous():
    """L'infirmier existe, cadencé par compteur, avec re-test anti-hoquet."""
    s = _script()
    assert "QBIT_NURSE_EVERY_TICKS" in s
    assert "stream-guard-nurse.counter" in s
    assert "INFIRMIER" in s
    # Re-test 10 s après le premier échec avant tout restart
    assert s.count("sleep 10") >= 1
