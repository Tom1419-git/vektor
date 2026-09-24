"""Outil jellyfin_search : recherche bibliothèque, lecture seule.

La clé est lue à l'appel (os.environ au runtime) : les tests injectent
leurs valeurs via monkeypatch.setenv sans jamais toucher au dépôt.
"""

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import tools  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeClient:
    captured = {}

    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {"Items": []}
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        FakeClient.captured = {"url": url, **kwargs}
        return FakeResponse(self._payload, self._status)


@pytest.fixture
def jellyfin_env(monkeypatch):
    monkeypatch.setenv("VEKTOR_JELLYFIN_TOKEN", "test-key")
    monkeypatch.setitem(tools.SERVICE_URLS, "jellyfin", "http://jellyfin.test:8096")
    FakeClient.captured = {}
    return jellyfin_env


async def test_recherche_trouve_un_film(monkeypatch, jellyfin_env):
    payload = {"Items": [
        {"Name": "Cars", "Type": "Movie", "ProductionYear": 2006},
        {"Name": "Cars 2", "Type": "Movie", "ProductionYear": 2011},
    ]}

    class Client(FakeClient):
        def __init__(self, **k):
            super().__init__(payload=payload)

    monkeypatch.setattr(tools.httpx, "AsyncClient", Client)
    result = await tools.jellyfin_search("cars")
    assert "Cars (2006)" in result and "Cars 2 (2011)" in result
    # lecture seule : simple GET /Items avec le token en header
    assert FakeClient.captured["url"].endswith("/Items")
    # Jellyfin 12+ : auth par Authorization MediaBrowser (X-Emby-Token -> 401)
    assert FakeClient.captured["headers"]["Authorization"] == 'MediaBrowser Token="test-key"'
    assert FakeClient.captured["params"]["searchTerm"] == "cars"


async def test_recherche_vide_message_clair(monkeypatch, jellyfin_env):
    monkeypatch.setattr(tools.httpx, "AsyncClient", lambda **k: FakeClient())
    result = await tools.jellyfin_search("film-inexistant")
    assert "Aucun titre trouvé" in result


async def test_recherche_sans_cle_message_sans_plantage(monkeypatch):
    monkeypatch.delenv("VEKTOR_JELLYFIN_TOKEN", raising=False)
    monkeypatch.setitem(tools.SERVICE_URLS, "jellyfin", "http://jellyfin.test:8096")
    result = await tools.jellyfin_search("cars")
    assert "non configurée" in result


async def test_recherche_service_injoignable_degrade(monkeypatch, jellyfin_env):
    class Boom(FakeClient):
        def __init__(self, **k):
            super().__init__()

        async def get(self, url, **kwargs):
            raise httpx.ConnectError("down", request=None)

    monkeypatch.setattr(tools.httpx, "AsyncClient", Boom)
    result = await tools.jellyfin_search("cars")
    assert "injoignable" in result
