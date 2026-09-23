"""Tests du diagnostic /ping (chemin LLM maillon par maillon)."""

import httpx
import pytest

from app import ping


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeClient:
    """httpx.AsyncClient minimal : réponses par (méthode, url-part)."""

    def __init__(self, routes):
        self._routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        for pattern, response in self._routes.get("GET", []):
            if pattern in url:
                return response
        raise httpx.ConnectError("no route")

    async def post(self, url, json=None):
        for pattern, response in self._routes.get("POST", []):
            if pattern in url:
                return response
        raise httpx.ConnectError("no route")


@pytest.fixture
def healthy(monkeypatch):
    """Tous les maillons répondent (modèle = celui configuré du module)."""
    model = ping.OLLAMA_MODEL
    monkeypatch.setattr(
        ping.httpx,
        "AsyncClient",
        lambda **k: FakeClient({
            "GET": [
                ("/api/tags", FakeResponse(payload={"models": [{"name": model}]})),
                ("/api/ps", FakeResponse(payload={"models": [{"name": model}]})),
            ],
            "POST": [("/api/generate", FakeResponse(payload={"response": "OK"}))],
        }),
    )


async def test_ping_tous_maillons_verts(healthy):
    report = await ping.ping_report()
    assert "🟢 Bridge Ollama" in report
    assert "présent côté Ollama" in report
    assert "chargé en RAM" in report
    assert "✅" in report and "opérationnel" in report


async def test_ping_bridge_mort_nomme_le_maillon(monkeypatch):
    def boom(**k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ping.httpx, "AsyncClient", boom)
    report = await ping.ping_report()
    assert "🔴 Bridge Ollama" in report
    assert "vektor-ollama-bridge" in report  # la piste de réparation est citée
    assert "Premier maillon cassé" in report


async def test_ping_modele_absent_le_dit(monkeypatch):
    monkeypatch.setattr(
        ping.httpx,
        "AsyncClient",
        lambda **k: FakeClient({
            "GET": [
                ("/api/tags", FakeResponse(payload={"models": [{"name": "autre:7b"}]})),
                ("/api/ps", FakeResponse(payload={"models": []})),
            ],
            "POST": [("/api/generate", FakeResponse(payload={"response": "OK"}))],
        }),
    )
    report = await ping.ping_report()
    assert f"🔴 Modèle `{ping.OLLAMA_MODEL}` ABSENT" in report
    assert "Premier maillon cassé" in report


async def test_ping_modele_non_charge_est_un_avertissement_pas_une_panne(monkeypatch):
    model = ping.OLLAMA_MODEL
    monkeypatch.setattr(
        ping.httpx,
        "AsyncClient",
        lambda **k: FakeClient({
            "GET": [
                ("/api/tags", FakeResponse(payload={"models": [{"name": model}]})),
                ("/api/ps", FakeResponse(payload={"models": []})),  # pas en RAM
            ],
            "POST": [("/api/generate", FakeResponse(payload={"response": "OK"}))],
        }),
    )
    report = await ping.ping_report()
    assert "🟡" in report  # warning, pas 🔴
    assert "pas une panne" in report
