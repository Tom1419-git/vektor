"""Tests du endpoint /api/reload-doc (réindexation à chaud)."""

import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main as app_main  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_reload_doc_reindexe_et_compte(client, monkeypatch):
    calls = []

    async def fake_index(memory):
        calls.append(memory)
        return 57

    monkeypatch.setattr(app_main, "index_knowledge", fake_index)
    response = await client.post(
        "/api/reload-doc",
        headers={"X-Vektor-Token": app_main.get_settings().vektor_api_token},
    )
    assert response.status_code == 200
    assert "57 extraits" in response.json()["report"]
    assert calls and calls[0] is app_main.memory  # sur la vraie mémoire


async def test_reload_doc_echec_reponse_claire_et_pas_de_perte(client, monkeypatch):
    async def boom(memory):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(app_main, "index_knowledge", boom)
    response = await client.post(
        "/api/reload-doc",
        headers={"X-Vektor-Token": app_main.get_settings().vektor_api_token},
    )
    assert response.status_code == 500
    assert "index précédent conservé" in response.json()["detail"]


async def test_reload_doc_sans_token_refuse(client):
    response = await client.post("/api/reload-doc")
    assert response.status_code == 401


async def test_reload_doc_est_un_post_pas_un_get(client):
    response = await client.get(
        "/api/reload-doc",
        headers={"X-Vektor-Token": app_main.get_settings().vektor_api_token},
    )
    assert response.status_code == 405
