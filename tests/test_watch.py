"""Tests des outils de supervision élargis (lecture seule via API)."""

from app import watch


# ── Backups ───────────────────────────────────────────────────────────────

async def test_backups_api_injoignable_message_clair(monkeypatch):
    async def fail_node():
        return None

    monkeypatch.setattr(watch, "_pve_node", fail_node)
    report = await watch.backups_report()
    assert "injoignable ou non configurée" in report


async def test_backups_liste_et_age(monkeypatch):
    async def fake_node():
        return "pve"

    async def fake_get(path):
        if path.endswith("/storage"):
            return [{"storage": "backup-dumps", "content": "backup"}]
        if "content?volid=1" in path:
            return [
                {"volid": "backup-dumps:backup/vzdump-lxc-103.tar.zst", "ctime": watch.time.time() - 3600, "size": 2**30},
                {"volid": "backup-dumps:backup/vzdump-lxc-104.tar.zst", "ctime": watch.time.time() - 10 * 86400, "size": 3**30},
            ]
        return None

    monkeypatch.setattr(watch, "_pve_node", fake_node)
    monkeypatch.setattr(watch, "_pve_get", fake_get)
    report = await watch.backups_report()
    assert "vzdump-lxc-103" in report
    assert "vzdump-lxc-104" in report
    assert "🔴" in report and "🟢" in report  # vieux backup signalé


async def test_backups_ignorer_les_stockages_sans_content_backup(monkeypatch):
    async def fake_node():
        return "pve"

    async def fake_get(path):
        if path.endswith("/storage"):
            return [{"storage": "local", "content": "iso,vztmpl"}]
        return None

    monkeypatch.setattr(watch, "_pve_node", fake_node)
    monkeypatch.setattr(watch, "_pve_get", fake_get)
    report = await watch.backups_report()
    assert "Aucun fichier de backup" in report


# ── DNS (DoH) ─────────────────────────────────────────────────────────────

class FakeDoHResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeDoHClient:
    def __init__(self, responses_by_url):
        self._responses = responses_by_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        return self._responses[url]


async def test_dns_sonde_les_resolveurs(monkeypatch):
    responses = {
        "http://a.test/dns-query": FakeDoHResponse(payload={"Status": 0, "Answer": [{"type": 1, "data": "93.184.216.34"}]}),
        "http://b.test/dns-query": FakeDoHResponse(status_code=503),
    }
    monkeypatch.setattr(watch, "DNS_RESOLVERS", {"a": "http://a.test/dns-query", "b": "http://b.test/dns-query"})
    monkeypatch.setattr(watch.httpx, "AsyncClient", lambda **k: FakeDoHClient(responses))
    report = await watch.dns_report()
    assert "🟢 a" in report and "93.184.216.34" in report
    assert "🔴 b" in report and "503" in report


async def test_dns_aucun_resolveur_configure(monkeypatch):
    monkeypatch.setattr(watch, "DNS_RESOLVERS", {})
    report = await watch.dns_report()
    assert "aucun résolveur" in report


# ── Monitoring (Healthchecks.io) ──────────────────────────────────────────

async def test_monitoring_non_configure_degrade_proprement(monkeypatch):
    monkeypatch.setattr(watch, "HC_API_URL", "")
    monkeypatch.setattr(watch, "HC_READ_TOKEN", "")
    report = await watch.monitoring_report()
    assert "non configuré" in report
    assert "VEKTOR_HC_READ_TOKEN" in report


async def test_monitoring_resume_les_checks(monkeypatch):
    monkeypatch.setattr(watch, "HC_API_URL", "http://hc.test")
    monkeypatch.setattr(watch, "HC_READ_TOKEN", "read-token")

    class FakeHCResponse:
        status_code = 200

        def json(self):
            return {"checks": [
                {"name": "stream-guard", "status": "up"},
                {"name": "nightly-backup", "status": "late"},
                {"name": "s3-sync", "status": "down"},
            ]}

    class FakeHCClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            assert "X-Api-Key" in kwargs["headers"]
            return FakeHCResponse()

    monkeypatch.setattr(watch.httpx, "AsyncClient", FakeHCClient)
    report = await watch.monitoring_report()
    assert "3 checks" in report
    assert "1 ok" in report and "1 en retard" in report and "1 down" in report
    assert "s3-sync" in report and "nightly-backup" in report


# ── Exposition au LLM (tool-calling) ─────────────────────────────────────

async def test_les_trois_nouveaux_outils_sont_exposes_lecture_seule():
    from app import graph

    names = {tool_item.name for tool_item in graph.READONLY_TOOLS}
    assert {"derniers_backups", "etat_dns", "etat_monitoring"} <= names
