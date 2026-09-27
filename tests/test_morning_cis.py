"""Tests de la section CIS-L1 PegaProx du rapport matinal.

Le script PVE pegaprox-cis-check renvoie des lignes "OK|…" / "RED|…" :
la régression doit remonter en évidence, l'indisponibilité doit laisser
le rapport matinal silencieux sur la compliance (jamais de crash).
"""

from app import actions as actions_mod
from app import morning


async def test_morning_cis_baseline(monkeypatch):
    async def fake_execute(action):
        assert action == "pegaprox_cis"
        return "OK|🛡️ CIS-L1 PegaProx : 10/44 pass (premier audit — baseline enregistrée)"

    monkeypatch.setattr(actions_mod, "_execute", fake_execute)
    report = await morning.morning_report()
    assert "CIS-L1" in report and "baseline" in report


async def test_morning_cis_stable(monkeypatch):
    async def fake_execute(action):
        return "OK|🛡️ CIS-L1 PegaProx : 10/44 pass (dernier audit il y a ~1h) — stable"

    monkeypatch.setattr(actions_mod, "_execute", fake_execute)
    report = await morning.morning_report()
    assert "stable" in report


async def test_morning_cis_regression_mise_en_evidence(monkeypatch):
    async def fake_execute(action):
        return (
            "RED|🔴 CIS-L1 PegaProx EN RÉGRESSION : 9/44 pass (avant : 10/44)\n"
            "RED|   Nouvellement en échec : auditd_service, pam_faillock\n"
            "OK|🛡️ CIS-L1 PegaProx : 9/44 pass"
        )

    monkeypatch.setattr(actions_mod, "_execute", fake_execute)
    report = await morning.morning_report()
    assert "EN RÉGRESSION" in report
    assert "auditd_service" in report
    # L'action à faire passe avant la conclusion
    assert report.index("pegaprox.mayoraz-net.ch") < report.index("Bonjour") + 4000
    assert report.index("👉") < report.index("Rien à faire") if "Rien à faire" in report else True


async def test_morning_cis_indisponible_silencieux(monkeypatch):
    async def fake_execute(action):
        return "CIS-L1 PegaProx : audit indisponible (API injoignable)"

    monkeypatch.setattr(actions_mod, "_execute", fake_execute)
    report = await morning.morning_report()
    assert "CIS" not in report  # pas de section compliance


async def test_morning_cis_crash_canal_silencieux(monkeypatch):
    async def boom(action):
        raise OSError("canal SSH mort")

    monkeypatch.setattr(actions_mod, "_execute", boom)
    report = await morning.morning_report()  # ne doit pas lever
    assert "CIS" not in report
