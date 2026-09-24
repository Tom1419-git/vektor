"""Auto-test de démarrage : chaque commande du bot doit toucher l'API.

Le MockTransport simule l'API : 401 = chemin existant protégé par token,
405 = chemin existant en POST (sondé en GET), 404/injoignable = commande
muette (le bug v1.3.6 que cet auto-test rend visible au démarrage).
"""

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.telegram as tg  # noqa: E402


KNOWN_ENDPOINTS = {
    "/api/chat", "/api/status", "/api/model", "/api/seeds", "/api/backups",
    "/api/dns", "/api/monitoring", "/api/ping", "/api/reload-doc", "/api/forget",
}


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _healthy_api(request: httpx.Request) -> httpx.Response:
    if request.url.path in KNOWN_ENDPOINTS:
        if "X-Vektor-Token" in request.headers:
            return httpx.Response(200, json={"report": "ok", "card": "ok", "response": "ok"})
        return httpx.Response(401, json={"detail": "Unauthorized"})
    return httpx.Response(404, json={"detail": "Not Found"})


@pytest.mark.asyncio
async def test_autotest_ok_quand_tous_les_chemins_existent():
    problems = await tg.autotest_endpoints(transport=_mock(_healthy_api))
    assert problems == []


@pytest.mark.asyncio
async def test_autotest_detecte_une_commande_muette_404():
    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/dns":
            return httpx.Response(404, json={"detail": "Not Found"})
        return _healthy_api(request)

    problems = await tg.autotest_endpoints(transport=_mock(broken))
    assert len(problems) == 2  # sonde GET + sonde token
    assert any("/dns" in p and "404" in p for p in problems)


@pytest.mark.asyncio
async def test_autotest_detecte_url_injoignable_defaut_loopback():
    """Le bug v1.3.6 : défaut 127.0.0.1 = Connection refused dans le conteneur."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    problems = await tg.autotest_endpoints(transport=_mock(refuse))
    assert problems, "un endpoint injoignable doit être signalé"
    assert any("injoignable" in p for p in problems)


@pytest.mark.asyncio
async def test_autotest_detecte_token_invalide():
    def api_rejecte_tout_token(request: httpx.Request) -> httpx.Response:
        if request.url.path in KNOWN_ENDPOINTS:
            return httpx.Response(401, json={"detail": "Unauthorized"})
        return httpx.Response(404)

    problems = await tg.autotest_endpoints(transport=_mock(api_rejecte_tout_token))
    assert any("VEKTOR_API_TOKEN" in p for p in problems)


@pytest.mark.asyncio
async def test_autotendpoints_post_only_reste_verte():
    """/api/reload-doc et /api/forget sont en POST : sondés en GET -> 405 = chemin OK."""

    def post_only(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/api/reload-doc", "/api/forget"):
            return httpx.Response(405, json={"detail": "Method Not Allowed"})
        return _healthy_api(request)

    problems = await tg.autotest_endpoints(transport=_mock(post_only))
    assert problems == []


def test_table_autotest_couplee_aux_constantes_urls():
    """Toute constante *_URL du bot doit être sondée par l'auto-test."""
    attrs = {attr for attr, _ep, _what in tg._COMMAND_ENDPOINTS}
    url_attrs = {name for name in dir(tg) if name.endswith("_URL")}
    assert attrs == url_attrs, "une URL de commande n'est pas couverte par l'auto-test"
    for attr, endpoint, _what in tg._COMMAND_ENDPOINTS:
        assert getattr(tg, attr).endswith(endpoint), f"{attr} ne pointe pas sur {endpoint}"
