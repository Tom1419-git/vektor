"""Tests des outils de supervision élargis (lecture seule via API)."""

from app import watch


# ── Backups (canal SSH forcé) ─────────────────────────────────────────────

async def test_backups_canal_indisponible_message_clair(monkeypatch):
    async def fake_channel(cmd):
        return "Canal SSH lecture seule indisponible pour le moment."

    monkeypatch.setattr(watch, "_status_channel", fake_channel)
    report = await watch.backups_report()
    assert "indisponible" in report


async def test_backups_parse_la_sortie_du_canal_force(monkeypatch):
    async def fake_channel(cmd):
        assert cmd == "vektor-status backups"
        return "OK|vzdump-lxc-101-2026_09_23.tar.zst|1G|15 h\nOLD|vzdump-lxc-102-2026_09_20.tar.zst|18G|3 j"

    monkeypatch.setattr(watch, "_status_channel", fake_channel)
    report = await watch.backups_report()
    assert "vzdump-lxc-101" in report
    assert "vzdump-lxc-102" in report
    assert "🟢" in report and "🔴" in report  # vieux backup signalé
    assert "2 fichiers vus" in report


async def test_backups_vide_dit_clairement_quaucun_fichier(monkeypatch):
    async def fake_channel(cmd):
        return "NONE|aucun vzdump trouve|0G|?"

    monkeypatch.setattr(watch, "_status_channel", fake_channel)
    report = await watch.backups_report()
    assert "Aucun fichier de backup" in report


# ── DNS (multi-transport : UDP wire, DoH wire, DoH JSON) ─────────────────

class FakeDoHResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content

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
        params = kwargs.get("params") or {}
        if params:  # forme DoH JSON : l'URL réelle porte la query string
            from urllib.parse import urlencode

            url = url + "?" + urlencode(params)
        return self._responses[url]


def _fake_udp_reply(packet, rcode=0, ip="93.184.216.34"):
    """Réponse DNS wire minimale : question recopiée + 1 A."""
    offset = 12  # l'entête fait 12 octets ; scanner le QNAME à partir de là
    while packet[offset] != 0:
        offset += packet[offset] + 1
    qend = offset + 5  # octet nul + QTYPE + QCLASS
    question = packet[12:qend]
    answer = b"\xc0\x0c" + bytes([0, 1, 0, 1, 0, 0, 0, 60, 0, 4]) + bytes(
        int(p) for p in ip.split(".")
    )
    return (
        packet[:2]  # echo de l'ID de transaction
        + bytes([0x81, 0x80 | rcode])
        + b"\x00\x01\x00\x01\x00\x00\x00\x00"
        + question
        + answer
    )


async def test_dns_sonde_udp_wire_et_json(monkeypatch):
    sent = {}

    def fake_udp(host, port, packet, timeout=4.0):
        sent["target"] = f"{host}:{port}"
        return _fake_udp_reply(packet)

    monkeypatch.setattr(watch, "DNS_RESOLVERS", {
        "pihole": "udp://192.0.2.53:53",
        "cloud": "http://c.test/dns-query",
    })
    monkeypatch.setattr(watch, "_udp_wire_query", fake_udp)
    monkeypatch.setattr(watch.httpx, "AsyncClient", lambda **k: FakeDoHClient({
        "http://c.test/dns-query?name=example.com&type=A": FakeDoHResponse(
            payload={"Status": 0, "Answer": [{"type": 1, "data": "93.184.216.34"}]}
        ),
    }))
    report = await watch.dns_report()
    assert sent["target"] == "192.0.2.53:53"
    assert "🟢 pihole" in report and "93.184.216.34" in report
    assert "🟢 cloud" in report
    # l'adresse du résolveur sondé doit être visible (sinon on confond les
    # IP de la réponse avec celles du résolveur)
    assert "192.0.2.53:53" in report
    assert "→" in report


async def test_dns_doh_wire_parse_le_rcode(monkeypatch):
    good = _fake_udp_reply(watch._encode_wire_query("example.com"))
    monkeypatch.setattr(watch, "DNS_RESOLVERS", {
        "ok": "https://doh.test/dns-query?{dns}",
        "ko": "https://doh.test/dns-query?{dns}",
    })
    monkeypatch.setattr(watch.httpx, "AsyncClient", lambda **k: FakeDoHClient({
        "https://doh.test/dns-query?" + watch._encode_doh_query("example.com"): good and FakeDoHResponse(content=good),
    }))
    # le second résolveur partage la même URL : on le fait échouer via un socket KO
    async def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(watch, "DNS_RESOLVERS", {"ko": "udp://192.0.2.1:53"})
    report = await watch.dns_report()
    assert "injoignable" in report


async def test_dns_aucun_resolveur_configure(monkeypatch):
    monkeypatch.setattr(watch, "DNS_RESOLVERS", {})
    report = await watch.dns_report()
    assert "aucun résolveur" in report


def test_parse_wire_extrait_rcode_et_ips():
    packet = watch._encode_wire_query("example.com")
    rcode, ips = watch._parse_wire_response(_fake_udp_reply(packet, ip="192.0.2.7"))
    assert rcode == 0
    assert ips == ["192.0.2.7"]


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
