"""v1.5.0 : fallback LLM conditionnel + canal web."""

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import graph  # noqa: E402
from app.config import get_settings  # noqa: E402


class _TagsResponse:
    status_code = 200


async def test_fallback_choisi_si_joignable(monkeypatch):
    monkeypatch.setattr(get_settings(), "ollama_fallback_url", "http://fallback-host:11434")

    async def ok_get(self, url, **kwargs):
        assert url.endswith("/api/tags")
        return _TagsResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", ok_get)

    async def fake_pick(base, model):
        return f"llm@{base}"

    monkeypatch.setattr(graph, "_pick_llm", fake_pick)

    llm, source = await graph.select_llm()
    assert source == "fallback" and "fallback-host" in str(llm)


async def test_primaire_si_fallback_vide(monkeypatch):
    monkeypatch.setattr(get_settings(), "ollama_fallback_url", "")
    llm, source = await graph.select_llm()
    assert source == "primaire"


async def test_primaire_si_fallback_injoignable(monkeypatch):
    """Machine éteinte = ConnectError → primaire, jamais d'erreur visible."""
    monkeypatch.setattr(get_settings(), "ollama_fallback_url", "http://fallback-host:11434")

    async def refused(self, url, **kwargs):
        raise httpx.ConnectError("refused", request=None)

    monkeypatch.setattr(httpx.AsyncClient, "get", refused)
    llm, source = await graph.select_llm()
    assert source == "primaire"


async def test_canal_web_expose_page_et_chat(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as app_main

    class FakeMemory:
        async def ensure_conversation(self, *a, **k):
            return 42

        async def add_message(self, *a, **k):
            pass

        async def history(self, *a, **k):
            return []

        async def close(self):
            pass

    async def fake_agent(memory, text, history, user_key="default"):
        return f"réponse à : {text}"

    monkeypatch.setattr(app_main, "memory", FakeMemory())
    monkeypatch.setattr(app_main, "run_agent", fake_agent)

    # Sans contexte `with` : la lifespan (memory réelle + watcher) ne tourne pas
    client = TestClient(app_main.app)
    page = client.get("/web")
    assert page.status_code == 200 and "Vektor" in page.text and "api/web/chat" in page.text

    ok = client.post(
        "/api/web/chat",
        json={"text": "état de jellyfin ?"},
        headers={"X-Vektor-Token": "test-token"},
    )
    assert ok.status_code == 200 and "réponse à" in ok.json()["response"]

    refus = client.post(
        "/api/web/chat", json={"text": "salut"}, headers={"X-Vektor-Token": "mauvais"}
    )
    assert refus.status_code == 401
