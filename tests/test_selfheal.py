"""Tests de l'auto-réparation Vektor (webhook Grafana)."""

import pytest

from app import selfheal


def test_match_thin_pool_92_donne_fstrim():
    assert selfheal._match_rule("Thin pool LVM CRITIQUE > 92% (pve/backup-dumps)") == "pve_fstrim"


def test_match_thin_pool_85_ne_donne_rien():
    assert selfheal._match_rule("Thin pool LVM > 85% (pve/backup-dumps)") is None


def test_match_backup_echec_ne_donne_rien():
    """Les échecs de backup ne se « réparent » pas automatiquement."""
    assert selfheal._match_rule("Backup vzdump échec") is None


def test_match_inconnu_ne_donne_rien():
    assert selfheal._match_rule("RAM LXC > 90%") is None


@pytest.mark.asyncio
async def test_webhook_warning_ne_declenche_rien(monkeypatch):
    executed = []

    async def fake_execute(action):
        executed.append(action)
        return "ok"

    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    payload = {
        "alerts": [{
            "title": "Thin pool LVM > 85% (pve/backup-dumps)",
            "status": "firing",
            "labels": {"severity": "warning"},
        }]
    }
    summary = await selfheal.handle_grafana_webhook(payload)
    assert executed == []
    assert summary["acted"] == []
    assert summary["skipped"] == ["Thin pool LVM > 85% (pve/backup-dumps)"]


@pytest.mark.asyncio
async def test_webhook_critical_declenche_une_fois_puis_cooldown(monkeypatch):
    executed = []

    async def fake_execute(action):
        executed.append(action)
        return "RESULT: thin 92% -> 40%"

    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    monkeypatch.setattr(selfheal, "_tg", _noop_tg)
    selfheal._LAST_RUN.clear()
    payload = {
        "alerts": [{
            "title": "Thin pool LVM CRITIQUE > 92% (pve/backup-dumps)",
            "status": "firing",
            "labels": {"severity": "critical"},
        }]
    }
    summary = await selfheal.handle_grafana_webhook(payload)
    assert executed == ["pve_fstrim"]
    assert "pve_fstrim" in summary["acted"][0]
    # 2e passage immédiat : cooldown -> rien
    summary2 = await selfheal.handle_grafana_webhook(payload)
    assert executed == ["pve_fstrim"]
    assert summary2["acted"] == []
    assert any("cooldown" in s for s in summary2["skipped"])


@pytest.mark.asyncio
async def test_webhook_resolved_ne_declenche_rien(monkeypatch):
    async def fail_execute(action):
        raise AssertionError("un resolved ne doit rien exécuter")

    monkeypatch.setattr(selfheal.actions, "_execute", fail_execute)
    payload = {
        "alerts": [{
            "title": "Thin pool LVM CRITIQUE > 92%",
            "status": "resolved",
            "labels": {"severity": "critical"},
        }]
    }
    summary = await selfheal.handle_grafana_webhook(payload)
    assert summary["acted"] == []


@pytest.mark.asyncio
async def test_webhook_charge_garbage_sans_casser():
    summary = await selfheal.handle_grafana_webhook({"alerts": [None, {}, {"title": "x"}]})
    assert summary["acted"] == []


async def _noop_tg(text: str) -> None:
    return None
