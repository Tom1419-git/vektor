"""Tests du rapport matinal narratif."""

import httpx
import pytest

from app import morning


def _checks(*pairs):
    return [{"name": n, "status": s} for n, s in pairs]


@pytest.mark.asyncio
async def test_morning_nuit_calme(monkeypatch):
    async def fake_checks():
        return _checks(("vzdump-backup", "up"), ("vektor-api", "up"), ("stream-guard", "up"))

    async def fake_lvm():
        return ["🟢 pve/backup-dumps : 77% — confortable, trim mensuel en place"]

    monkeypatch.setattr(morning, "_hc_checks", fake_checks)
    monkeypatch.setattr(morning, "_lvm_lines", fake_lvm)
    report = await morning.morning_report()
    assert "12" not in report
    assert "3 checks" in report
    assert "Rien à faire" in report
    assert "77%" in report


@pytest.mark.asyncio
async def test_morning_avec_check_down(monkeypatch):
    async def fake_checks():
        return _checks(("vzdump-backup", "down"), ("vektor-api", "up"))

    async def fake_lvm():
        return []

    monkeypatch.setattr(morning, "_hc_checks", fake_checks)
    monkeypatch.setattr(morning, "_lvm_lines", fake_lvm)
    report = await morning.morning_report()
    assert "vzdump-backup" in report
    assert "Commence par" in report


@pytest.mark.asyncio
async def test_morning_sur_seuil_critique(monkeypatch):
    async def fake_checks():
        return _checks(("x", "up"))

    async def fake_lvm():
        return ["🔴 pve/data : 95% — action requise (fstrim, purge, extension)"]

    monkeypatch.setattr(morning, "_hc_checks", fake_checks)
    monkeypatch.setattr(morning, "_lvm_lines", fake_lvm)
    report = await morning.morning_report()
    assert "action requise" in report


def test_extract_textfile_metrics():
    raw = '\n'.join([
        "# HELP x",
        'lvm_thin_data_percent{pool="pve/backup-dumps"} 77.28',
        'lvm_vg_free_bytes{vg="pve"} 0',
    ])
    pools, vg_free = morning._parse_textfile(raw)
    assert pools == {"pve/backup-dumps": 77.28}
    assert vg_free == {"pve": 0.0}


def test_extract_textfile_metrics_garbage():
    pools, vg_free = morning._parse_textfile("n'importe quoi")
    assert pools == {} and vg_free == {}
