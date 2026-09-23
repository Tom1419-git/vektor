"""Tests du routeur déterministe actuel (mots-clés) — comportement V1 conservé."""

from app import tools


async def test_status_question_triggers_proxmox(monkeypatch):
    calls = []

    async def fake_summary():
        calls.append("node")
        return "Noeud test : CPU 5%"

    async def fake_lxc():
        calls.append("lxc")
        return "CTs Proxmox :"

    async def fake_docker():
        calls.append("docker")
        return "CONTAINER ID"

    async def fake_storage():
        calls.append("storage")
        return "Stockages PVE :"

    monkeypatch.setattr(tools, "pve_summary", fake_summary)
    monkeypatch.setattr(tools, "pve_lxc_status", fake_lxc)
    monkeypatch.setattr(tools, "docker_inventory", fake_docker)
    monkeypatch.setattr(tools, "pve_storage_status", fake_storage)

    report = await tools.infra_live_report("donne-moi l'état des CTs et le stockage")
    # « CTs » + « stockage » sans mot-clé de nœud : pas de résumé PVE
    assert calls == ["lxc", "storage"]


async def test_explain_question_does_not_trigger_live(monkeypatch):
    async def fail_summary():
        raise AssertionError("le LLM ne doit pas être court-circuité")

    monkeypatch.setattr(tools, "pve_summary", fail_summary)
    report = await tools.infra_live_report("explique-moi le rôle du CT 103 dans l'architecture")
    assert report is None


async def test_service_check_hit_and_miss(monkeypatch):
    class FakeResponse:
        status_code = 200

    captured = {}

    async def fake_get(url, **kwargs):
        captured["url"] = url
        return FakeResponse()

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            return await fake_get(url, **kwargs)

    monkeypatch.setattr(tools.httpx, "AsyncClient", FakeClient)
    result = await tools.check_service("jellyfin")
    assert "HTTP 200" in result

    assert "inconnu" in await tools.check_service("service-fantome")
