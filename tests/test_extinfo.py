"""Tests info externes v1.8.0 (APIs publiques sans clé).

Le client HTTP est mocké au niveau de la classe (httpx.AsyncClient utilisé
en context manager dans extinfo) : on simule les réponses JSON sans réseau.
"""
from unittest.mock import patch

import pytest

from app import extinfo, tools  # noqa: F401 (tools: vérif import côté routeur)


class _FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data


class _FakeAsyncClient:
    """Remplace httpx.AsyncClient : renvoie la réponse donnée (ou lève)."""

    def __init__(self, resp=None, exc=None, *args, **kwargs):
        self._resp, self._exc = resp, exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if self._exc:
            raise self._exc
        return self._resp


CF_OK = {"status": {"indicator": "none", "description": "All Systems Operational"}}
CF_MAJOR = {"status": {"indicator": "major", "description": "Elevated HTTP errors"}}
IP_OK = {"ip": "1.2.3.4"}
TIME_OK = {"unixtime": 1790880000, "utc_datetime": "2026-10-01T00:00:00+00:00"}


class TestCloudflare:
    async def test_status_ok(self):
        with patch_cf(_FakeAsyncClient(_FakeResp(CF_OK))):
            out = await extinfo.cloudflare_status()
        assert "🟢" in out and "Operational" in out

    async def test_status_incident_majeur(self):
        with patch_cf(_FakeAsyncClient(_FakeResp(CF_MAJOR))):
            out = await extinfo.cloudflare_status()
        assert "🟠" in out and "HTTP errors" in out

    async def test_status_http_500(self):
        with patch_cf(_FakeAsyncClient(_FakeResp({}, status=500))):
            out = await extinfo.cloudflare_status()
        assert "🔴" in out

    async def test_injoignable(self):
        with patch_cf(_FakeAsyncClient(exc=RuntimeError("boom"))):
            out = await extinfo.cloudflare_status()
        assert "🔴" in out


class TestIpPublique:
    async def test_ip_ok(self):
        with patch_cf(_FakeAsyncClient(_FakeResp(IP_OK))):
            out = await extinfo.public_ip()
        assert "1.2.3.4" in out

    async def test_ip_ko(self):
        with patch_cf(_FakeAsyncClient(exc=RuntimeError("x"))):
            out = await extinfo.public_ip()
        assert "🔴" in out


class TestHorloge:
    async def test_derive_ok(self):
        import time as _t
        fake = {"unixtime": _t.time()}
        with patch_cf(_FakeAsyncClient(_FakeResp(fake))):
            out = await extinfo.clock_check()
        assert "🟢" in out

    async def test_derive_grande(self):
        fake = {"unixtime": 0}  # 1970 : dérive énorme
        with patch_cf(_FakeAsyncClient(_FakeResp(fake))):
            out = await extinfo.clock_check()
        assert "🟠" in out


class TestRapportGlobal:
    async def test_rapport_ne_leve_jamais(self):
        async def boom(*a, **k):
            raise RuntimeError("x")
        with patch.object(extinfo, "cloudflare_status", boom), \
             patch.object(extinfo, "public_ip", boom), \
             patch.object(extinfo, "clock_check", boom):
            out = await extinfo.external_report()
        assert "Informations externes" in out

    async def test_rapport_complet(self):
        with patch_cf(_FakeAsyncClient(_FakeResp(CF_OK))):
            out = await extinfo.external_report()
        assert "Cloudflare" in out


def patch_cf(client):
    """Patch httpx.AsyncClient par une factory qui ignore les args (timeout…)."""
    import unittest.mock as m
    return m.patch.object(extinfo.httpx, "AsyncClient", lambda *a, **k: client)
