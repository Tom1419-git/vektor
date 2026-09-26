"""Tests du contrôle qBittorrent (/pause / /resume Telegram)."""

import pytest

from app import actions, qbcontrol


def _seeds(monkeypatch, total: int, paused: int) -> None:
    async def fake_report():
        return (
            f"🌱 **Seeds** — {total} torrents\n"
            f"🟢 actifs : {total - paused}\n⏸️ {paused} en pause"
        )

    monkeypatch.setattr(qbcontrol, "seeds_report", fake_report)


async def test_qb_state_parse_le_rapport_seeds(monkeypatch):
    _seeds(monkeypatch, total=255, paused=12)
    assert await qbcontrol.qb_state() == (255, 12)


async def test_qb_state_sur_rapport_illisible(monkeypatch):
    async def fake_report():
        return "rapport inattendu"

    monkeypatch.setattr(qbcontrol, "seeds_report", fake_report)
    assert await qbcontrol.qb_state() == (0, 0)


async def test_extract_playing_compte_les_lectures():
    sessions = [
        {"UserName": "a", "PlayState": {"PlayMethod": "DirectPlay", "IsPaused": False}, "NowPlayingItem": {"Name": "x"}},
        {"UserName": "b", "PlayState": {"PlayMethod": "DirectPlay", "IsPaused": True}, "NowPlayingItem": {"Name": "y"}},
        {"UserName": "c", "NowPlayingItem": {"Name": "z"}},
        {"UserName": "d"},
    ]
    assert qbcontrol._extract_playing(sessions) == 1


def test_extract_playing_sur_entree_invalide():
    assert qbcontrol._extract_playing(None) is None
    assert qbcontrol._extract_playing({"oops": 1}) is None


async def test_resume_warning_si_lecture_en_cours(monkeypatch):
    async def playing():
        return 2

    monkeypatch.setattr(qbcontrol, "jellyfin_playing_count", playing)
    proposal = await qbcontrol.proposal_for("resume", "tg:42")
    assert "2 lecture(s)" in proposal


async def test_resume_warning_sur_check_en_echec(monkeypatch):
    """Un check qui échoue NE JAMAIS signifier « pas de lecture » (26/09)."""
    async def broken():
        return None

    monkeypatch.setattr(qbcontrol, "jellyfin_playing_count", broken)
    proposal = await qbcontrol.proposal_for("resume", "tg:42")
    assert "Impossible de vérifier" in proposal


async def test_resume_sans_lecture_pas_davertissement(monkeypatch):
    async def idle():
        return 0

    monkeypatch.setattr(qbcontrol, "jellyfin_playing_count", idle)
    actions._PENDING.clear()
    proposal = await qbcontrol.proposal_for("resume", "tg:42")
    assert "Impossible" not in proposal
    assert "OUI" in proposal
    # la proposition a enregistré l'action dans le registre partagé
    assert actions._PENDING["tg:42"][0] == "qb_resume_103"


async def test_execute_pause_passe_par_le_canal_actions(monkeypatch):
    calls = []

    async def fake_execute(action):
        calls.append(action)
        return "RESULT: 255/255 torrents en pause"

    _seeds(monkeypatch, total=255, paused=255)
    monkeypatch.setattr(actions, "_execute", fake_execute)
    reply = await qbcontrol.execute("pause")
    assert calls == ["qb_pause_103"]
    assert "255/255 en pause" in reply


async def test_execute_resume_passe_par_le_canal_actions(monkeypatch):
    calls = []

    async def fake_execute(action):
        calls.append(action)
        return "RESULT: 255/255 torrents actifs"

    _seeds(monkeypatch, total=255, paused=0)
    monkeypatch.setattr(actions, "_execute", fake_execute)
    reply = await qbcontrol.execute("resume")
    assert calls == ["qb_resume_103"]
    assert "255/255 actifs" in reply


async def test_flux_tg_complet_proposal_puis_oui(monkeypatch):
    """GET /proposal -> registre OUI -> POST /qb/{verb} -> exécution."""
    from fastapi import HTTPException
    import app.main as main_mod

    actions._PENDING.clear()

    async def fake_execute(action):
        return "RESULT: 255/255 torrents en pause"

    _seeds(monkeypatch, total=255, paused=255)
    monkeypatch.setattr(actions, "_execute", fake_execute)

    # 1. proposition (ce que fait /pause via l'API)
    reply = await main_mod.qb_proposal_endpoint("pause", "test-token", "42", "telegram")
    assert "OUI" in reply["proposal"]
    assert actions._PENDING["telegram:42"][0] == "qb_pause_103"

    # 2. confirmation : exécute l'action en attente
    result = await main_mod.qb_action_endpoint("pause", "test-token", "42", "telegram")
    assert "255/255" in result["result"]
    assert "telegram:42" not in actions._PENDING


async def test_post_sans_proposition_refuse(monkeypatch):
    """Pas d'exécution sans la double confirmation (registre vide)."""
    import app.main as main_mod

    actions._PENDING.clear()
    result = await main_mod.qb_action_endpoint("pause", "test-token", "42", "telegram")
    assert "Aucune action en attente" in result["result"]


async def test_post_ne_consomme_pas_une_autre_action(monkeypatch):
    """Le POST /qb/pause ne doit pas consommer une action différente."""
    import app.main as main_mod

    actions._PENDING.clear()
    actions._PENDING["telegram:42"] = ("docker_restart_102_jellyfin", actions.time.monotonic())

    async def fail_execute(action):
        raise AssertionError("l'action d'un autre type ne doit pas s'exécuter")

    monkeypatch.setattr(actions, "_execute", fail_execute)
    result = await main_mod.qb_action_endpoint("pause", "test-token", "42", "telegram")
    assert "autre action" in result["result"]
    assert "telegram:42" in actions._PENDING
    actions._PENDING.clear()


async def test_verb_inconnu_404():
    from fastapi import HTTPException
    import app.main as main_mod

    with pytest.raises(HTTPException) as exc:
        await main_mod.qb_action_endpoint("purge", "test-token", "42", "telegram")
    assert exc.value.status_code == 404
