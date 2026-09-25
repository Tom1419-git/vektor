"""v1.5.3 : commande /ops (exploitation Vektor, lecture seule, sans LLM)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import main as app_main  # noqa: E402
from app import ops  # noqa: E402


# ── Module ops ───────────────────────────────────────────────────────────


def test_deployed_version_absente_en_dev(monkeypatch, tmp_path):
    """Pas de fichier version → section 'inconnue', jamais d'erreur."""
    monkeypatch.setattr(ops, "_VERSION_FILE", tmp_path / "version-inexistante")
    assert ops.deployed_version() is None


def test_deployed_version_lue_et_trimée(monkeypatch, tmp_path):
    f = tmp_path / "version"
    f.write_text("v1.5.3\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_VERSION_FILE", f)
    assert ops.deployed_version() == "v1.5.3"


def test_marque_vert_et_rouge():
    assert ops._mark(True, "ok", "ko").startswith("🟢")
    assert ops._mark(False, "ok", "ko").startswith("🔴")


def test_docker_self_check_ne_plante_jamais():
    """Lecture réelle de /proc/1/cgroup : bool + détail, quel que soit l'OS."""
    in_container, detail = ops._docker_self_check()
    assert isinstance(in_container, bool)
    assert isinstance(detail, str) and detail


def test_docker_self_check_logique_fichier_fake(tmp_path, monkeypatch):
    """Logique pure : injecte un /proc/1/cgroup de conteneur (depth >= 1)."""
    fake = tmp_path / "cgroup"
    fake.write_text("0::/docker/abc123\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_VERSION_FILE", ops._VERSION_FILE)  # no-op garde-fou
    original_read = ops.Path.read_text

    def fake_read_text(self, *a, **k):
        if str(self).endswith("/proc/1/cgroup"):
            return fake.read_text(*a, **k)
        return original_read(self, *a, **k)

    monkeypatch.setattr(ops.Path, "read_text", fake_read_text)
    in_container, detail = ops._docker_self_check()
    assert in_container is True
    assert "conteneur" in detail


def test_process_summary_ne_plante_pas():
    summary = ops._process_summary()
    assert summary.startswith("⚙️") and "Processus API" in summary


def test_backups_age_line_format_canal():
    line = ops._backups_age_line("🟢 vzdump-lxc-103... — 1.2G, il y a 5 h")
    assert "il y a 5 h" in line and "/backups" in line


def test_backups_age_line_format_non_typique():
    line = ops._backups_age_line("sortie bizarre sans age")
    assert "/backups" in line  # retombe proprement, jamais d'erreur


async def test_ops_report_complet_avec_monitoring_fake(monkeypatch):
    """Le rapport agrège : version absente (dev) + monitoring + backups fakes."""

    async def fake_monitoring():
        # Format réel de watch.monitoring_report (contrat parsé par ops)
        return "🩺 **Monitoring** — 12 checks\n🟢 12 ok"

    async def fake_backups():
        return "🟢 `vzdump-lxc-103` — 1.2G, il y a 5 h"

    monkeypatch.setattr(ops.watch_mod, "monitoring_report", fake_monitoring)
    monkeypatch.setattr(ops.watch_mod, "backups_report", fake_backups)

    report = await ops.ops_report()
    assert report.startswith("🛰️")
    assert "Version déployée : inconnue" in report
    assert "Processus API" in report
    assert "12 check(s) OK" in report
    assert "il y a 5 h" in report


async def test_ops_report_survit_aux_erreurs(monkeypatch):
    """Monitoring ou backups en échec → ligne rouge, rapport quand même rendu."""

    async def boom():
        raise RuntimeError("panne simulée")

    monkeypatch.setattr(ops.watch_mod, "monitoring_report", boom)
    monkeypatch.setattr(ops.watch_mod, "backups_report", boom)

    report = await ops.ops_report()
    assert report.startswith("🛰️")
    assert "Monitoring : erreur (RuntimeError)" in report
    assert "Backups : erreur (RuntimeError)" in report


# ── Surfaces API + canal web ─────────────────────────────────────────────


def test_endpoint_api_ops_exige_et_accepte_token(monkeypatch):
    async def fake_ops():
        return "🛰️ rapport de test"

    monkeypatch.setattr(app_main, "ops_report", fake_ops)
    client = TestClient(app_main.app)

    refus = client.get("/api/ops")
    assert refus.status_code == 401

    ok = client.get("/api/ops", headers={"X-Vektor-Token": "test-token"})
    assert ok.status_code == 200 and "rapport de test" in ok.json()["report"]


async def test_canal_web_commande_ops(monkeypatch):
    called = []

    async def fake_ops():
        called.append("ops")
        return "🛰️ rapport ops web"

    async def fail_agent(*a, **k):
        raise AssertionError("une commande ne doit pas passer par le LLM")

    monkeypatch.setitem(app_main._WEB_COMMANDS, "/ops", fake_ops)
    monkeypatch.setattr(app_main, "run_agent", fail_agent)

    client = TestClient(app_main.app)
    r = client.post(
        "/api/web/chat",
        json={"text": "/ops"},
        headers={"X-Vektor-Token": "test-token"},
    )
    assert r.status_code == 200 and "rapport ops web" in r.json()["response"]
    assert called == ["ops"]


def test_chip_ops_dans_la_page_web():
    client = TestClient(app_main.app)
    page = client.get("/web")
    assert page.status_code == 200
    assert 'data-cmd="/ops"' in page.text
