"""Tests du module d'actions : détection, confirmation OUI, expiration."""

import asyncio

from app import actions


async def test_detect_action_redemarrage_jellyfin():
    label, action = actions.detect_action("redémarre jellyfin stp")
    assert action == "docker_restart_102_jellyfin"


async def test_detect_action_hors_whitelist():
    assert actions.detect_action("éteins le serveur") is None
    assert actions.detect_action("supprime tout") is None


async def test_lxc_restart_capture_le_numero():
    _, action = actions.detect_action("redémarre le CT 102")
    assert action == "lxc_restart_102"


async def test_confirmation_sans_action_en_attente():
    actions._PENDING.clear()
    reply = await actions.confirm_pending("OUI", "test:user")
    assert "Aucune action en attente" in reply


async def test_flux_complet_propose_puis_confirme(monkeypatch):
    actions._PENDING.clear()
    executed = []

    async def fake_execute(action):
        executed.append(action)
        return "OK action simulée"

    monkeypatch.setattr(actions, "_execute", fake_execute)
    propose_reply = actions.propose("docker_restart_102_jellyfin", "test:user")
    assert "OUI" in propose_reply
    assert executed == []

    confirm_reply = await actions.confirm_pending("OUI", "test:user")
    assert executed == ["docker_restart_102_jellyfin"]
    assert "OK action simulée" in confirm_reply


async def test_confirmation_expire_apres_ttl(monkeypatch):
    actions._PENDING.clear()

    async def fail_execute(action):
        raise AssertionError("l'action expirée ne doit pas s'exécuter")

    monkeypatch.setattr(actions, "_execute", fail_execute)
    actions.propose("docker_restart_102_jellyfin", "test:user")
    # Vieillir la proposition au-delà du TTL
    action, _ = actions._PENDING["test:user"]
    actions._PENDING["test:user"] = (action, actions.time.monotonic() - actions.PENDING_TTL_S - 1)
    reply = await actions.confirm_pending("OUI", "test:user")
    assert "Aucune action en attente" in reply
    assert "test:user" not in actions._PENDING


async def test_texte_qui_n_est_pas_une_confirmation_ne_consomme_pas():
    actions._PENDING.clear()
    actions.propose("docker_restart_102_jellyfin", "test:user")
    assert await actions.confirm_pending("oui mais non", "test:user") is None
    assert "test:user" in actions._PENDING
    actions._PENDING.clear()
