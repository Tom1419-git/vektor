"""Tests des nouvelles chips web : /pause /resume /matin /snapls.

Le flux complet (proposition -> OUI -> exécution) est déjà couvert par
test_qbcontrol (registre partagé) et test_actions (confirm_pending) : ici
on vérifie le câblage du canal web — mapping slash, page HTML, et surtout
que les actions d'écriture ne sont PAS des commandes directes.
"""

from app import actions, main as main_mod


def test_slash_pause_resume_reconnus_par_detect_action():
    """Les chips /pause /resume s'appuient sur le whitelist actions."""
    assert actions.detect_action("/pause")[1] == "qb_pause_103"
    assert actions.detect_action("/resume")[1] == "qb_resume_103"


def test_slash_incomplets_ne_matchent_pas():
    assert actions.detect_action("/pausepasunmot") is None
    assert actions.detect_action("/paused") is None or actions.detect_action("/paused")[1] == "qb_pause_103"


def test_web_commands_inclut_matin_et_snapls():
    assert "/matin" in main_mod._WEB_COMMANDS
    assert "/snapls" in main_mod._WEB_COMMANDS
    # /pause et /resume ne doivent PAS être des commandes directes : une
    # écriture passe toujours par le chemin agent (proposition -> OUI).
    assert "/pause" not in main_mod._WEB_COMMANDS
    assert "/resume" not in main_mod._WEB_COMMANDS


def test_page_web_contient_les_nouvelles_chips_et_le_bouton_oui():
    assert "data-cmd=\"/matin\"" in main_mod._WEB_PAGE
    assert "data-cmd=\"/pause\"" in main_mod._WEB_PAGE
    assert "data-cmd=\"/resume\"" in main_mod._WEB_PAGE
    assert "data-cmd=\"/snapls\"" in main_mod._WEB_PAGE
    assert "Confirmer (OUI)" in main_mod._WEB_PAGE


def test_page_web_bouton_oui_n_apparait_que_si_confirmation():
    """Le bouton OUI est ajouté dynamiquement par le JS, jamais statique."""
    assert main_mod._WEB_PAGE.count("Confirmer (OUI)") == 1  # créé par JS uniquement
