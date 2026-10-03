"""Tests de l'auto-réparation et des rapports d'incident Vektor."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main, selfheal


def test_match_thin_pool_92_donne_fstrim():
    assert selfheal._match_rule("Thin pool LVM CRITIQUE > 92% (pve/backup-dumps)") == "pve_fstrim"


def test_match_thin_pool_85_ne_donne_rien():
    assert selfheal._match_rule("Thin pool LVM > 85% (pve/backup-dumps)") is None


def test_match_backup_echec_ne_donne_rien():
    """Les échecs de backup ne se « réparent » pas automatiquement."""
    assert selfheal._match_rule("Backup vzdump échec") is None


def test_match_inconnu_ne_donne_rien():
    assert selfheal._match_rule("RAM LXC > 90%") is None


def test_endpoint_webhook_renvoie_statut_des_rapports(monkeypatch):
    async def fake_handle(payload):
        return {"acted": ["action ok"], "skipped": [], "reports_generated": 1, "reports_sent": 1}

    monkeypatch.setenv("VEKTOR_SELFHEAL_SECRET", "secret")
    monkeypatch.setattr("app.selfheal.handle_grafana_webhook", fake_handle)
    # Ne pas entrer dans la lifespan : ce test unitaire ne dépend pas de PostgreSQL.
    client = TestClient(main.app)
    response = client.post(
        "/api/webhook/grafana",
        headers={"X-Vektor-Selfheal": "secret"},
        json={"alerts": []},
    )
    assert response.status_code == 200
    assert response.json() == {
        "acted": ["action ok"],
        "skipped_count": 0,
        "reports_generated": 1,
        "reports_sent": 1,
    }


@pytest.mark.asyncio
async def test_webhook_warning_ne_declenche_rien(monkeypatch):
    executed = []

    async def fake_execute(action):
        executed.append(action)
        return "ok"

    async def capture_tg(text):
        return True

    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    payload = {
        "alerts": [{
            "title": "Thin pool LVM > 85% (pve/backup-dumps)",
            "status": "firing",
            "labels": {"severity": "warning"},
        }]
    }
    summary = await selfheal.handle_grafana_webhook(payload)
    assert executed == []
    assert summary["acted"] == []
    assert summary["skipped"] == ["Thin pool LVM > 85% (pve/backup-dumps)"]
    assert summary["reports_sent"] == 1


@pytest.mark.asyncio
async def test_webhook_critical_execute_quand_hors_cooldown(monkeypatch):
    executed = []

    async def fake_execute(action):
        executed.append(action)
        return "RESULT: thin 92% -> 40%"

    reports = []

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    # Cooldown vide => action autorisée (isolation totale du state module)
    monkeypatch.setattr(selfheal, "_LAST_RUN", {})
    payload = {
        "alerts": [{
            "title": "Thin pool LVM CRITIQUE > 92% (pve/backup-dumps)",
            "status": "firing",
            "labels": {"severity": "critical"},
        }]
    }
    summary = await selfheal.handle_grafana_webhook(payload)
    assert executed == ["pve_fstrim"]
    assert "pve_fstrim" in summary["acted"][0]
    assert selfheal._LAST_RUN["pve_fstrim"] > 0
    assert summary["reports_sent"] == 1
    assert "Rapport d’incident Vektor" in reports[0]
    assert "Cause probable" in reports[0]
    assert "lancer fstrim" in reports[0]
    assert "succès fonctionnel non vérifié" in reports[0]
    assert "Conseil" in reports[0]


@pytest.mark.asyncio
async def test_webhook_critical_bloque_pendant_cooldown(monkeypatch):
    """Un 2e webhook rapproché ne ré-exécute PAS (anti-tempête)."""

    async def fail_execute(action):
        raise AssertionError("le cooldown doit bloquer l'exécution")

    reports = []

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", fail_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    # Cooldown déjà actif (tout juste exécuté)
    monkeypatch.setattr(
        selfheal, "_LAST_RUN", {"pve_fstrim": __import__("time").monotonic()}
    )
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    payload = {
        "alerts": [{
            "title": "Thin pool LVM CRITIQUE > 92% (pve/backup-dumps)",
            "status": "firing",
            "labels": {"severity": "critical"},
        }]
    }
    summary = await selfheal.handle_grafana_webhook(payload)
    assert summary["acted"] == []
    assert any("cooldown" in s for s in summary["skipped"])
    assert summary["reports_sent"] == 1
    assert "cooldown anti-tempête" in reports[0]
    assert "aucune réparation automatique" in reports[0]


@pytest.mark.asyncio
async def test_webhook_resolved_ne_declenche_rien(monkeypatch):
    async def fail_execute(action):
        raise AssertionError("un resolved ne doit rien exécuter")

    monkeypatch.setattr(selfheal.actions, "_execute", fail_execute)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {"Thin pool LVM CRITIQUE > 92%": {
        "title": "Thin pool LVM CRITIQUE > 92%",
        "severity": "critical",
        "detected_at": "2026-10-02T16:00:00+00:00",
        "started_at": "2026-10-02T16:00:00+00:00",
        "action": "",
        "action_result": "",
        "action_error": "",
        "skipped_reason": "",
    }})
    payload = {
        "alerts": [{
            "title": "Thin pool LVM CRITIQUE > 92%",
            "status": "resolved",
            "labels": {"severity": "critical"},
        }]
    }
    reports = []

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    summary = await selfheal.handle_grafana_webhook(payload)
    assert summary["acted"] == []
    assert summary["reports_sent"] == 1
    assert "rétablissement détecté" in reports[0]
    assert "aucune nouvelle réparation" in reports[0]


@pytest.mark.asyncio
async def test_webhook_charge_garbage_sans_casser():
    summary = await selfheal.handle_grafana_webhook({"alerts": [None, {}, {"title": "x"}]})
    assert summary["acted"] == []
    assert summary["reports_generated"] == 0


async def _noop_tg(text: str) -> bool:
    return True


@pytest.mark.asyncio
async def test_alerte_inconnue_envoie_rapport_sans_action(monkeypatch):
    reports = []

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    payload = {"alerts": [{
        "fingerprint": "unknown-1",
        "title": "Service inconnu indisponible",
        "status": "firing",
        "startsAt": "2026-10-02T16:00:00Z",
        "labels": {"severity": "critical"},
        "annotations": {"description": "La sonde HTTP échoue."},
    }]}
    summary = await selfheal.handle_grafana_webhook(payload)
    assert summary["acted"] == []
    assert summary["reports_sent"] == 1
    assert "Cause non déterminée automatiquement" in reports[0]
    assert "La sonde HTTP échoue." in reports[0]
    assert "aucune règle d’auto-réparation sûre" in reports[0]
    assert "Vektor n’a pas modifié le serveur" in reports[0]


@pytest.mark.asyncio
async def test_alerte_repétée_ne_repete_pas_l_action_ni_le_rapport(monkeypatch):
    executed = []
    reports = []

    async def fake_execute(action):
        executed.append(action)
        return "restart exécuté"

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    monkeypatch.setattr(selfheal, "_LAST_RUN", {})
    payload = {"alerts": [{
        "fingerprint": "qbit-fp",
        "title": "Conteneur qBittorrent unhealthy",
        "status": "firing",
        "labels": {"severity": "critical"},
    }]}

    await selfheal.handle_grafana_webhook(payload)
    summary = await selfheal.handle_grafana_webhook(payload)
    assert executed == ["docker_restart_107_qbittorrent"]
    assert len(reports) == 1
    assert any("rapport déjà envoyé" in s for s in summary["skipped"])


@pytest.mark.asyncio
async def test_action_exception_est_racontee_et_alerte_passee_en_suivi(monkeypatch):
    reports = []

    async def broken_execute(action):
        raise OSError("SSH indisponible")

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", broken_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    monkeypatch.setattr(selfheal, "_LAST_RUN", {})
    payload = {"alerts": [{
        "title": "Conteneur qBittorrent unhealthy",
        "status": "firing",
        "labels": {"severity": "critical"},
    }]}
    summary = await selfheal.handle_grafana_webhook(payload)
    assert summary["acted"] == []
    assert "échec de l’action" in reports[0]
    assert "OSError" in reports[0]
    assert "vérifier l’accès SSH/actions" in reports[0]


@pytest.mark.asyncio
async def test_firing_puis_resolved_donne_duree_et_rapport_retour(monkeypatch):
    reports = []
    now = datetime.now(timezone.utc)

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    monkeypatch.setattr(selfheal, "_LAST_RUN", {})

    async def fake_execute(action):
        return "fstrim terminé"

    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    firing = {"alerts": [{
        "fingerprint": "pool-fp",
        "title": "Thin pool LVM CRITIQUE > 92%",
        "status": "firing",
        "startsAt": (now - timedelta(minutes=3)).isoformat(),
        "labels": {"severity": "critical"},
    }]}
    resolved = {"alerts": [{
        "fingerprint": "pool-fp",
        "title": "Thin pool LVM CRITIQUE > 92%",
        "status": "resolved",
        "endsAt": now.isoformat(),
        "labels": {"severity": "critical"},
    }]}

    await selfheal.handle_grafana_webhook(firing)
    await selfheal.handle_grafana_webhook(resolved)
    assert len(reports) == 2
    assert "rétablissement détecté" in reports[1]
    assert "durée" in reports[1]
    assert "3 min" in reports[1]


@pytest.mark.asyncio
async def test_webhook_charge_garbage_sans_casser():
    summary = await selfheal.handle_grafana_webhook({"alerts": [None, {}, {"title": "x"}]})
    assert summary["acted"] == []
    assert summary["reports_sent"] == 0


def test_match_qbittorrent_unhealthy():
    assert selfheal._match_rule("Conteneur qBittorrent unhealthy") == "docker_restart_107_qbittorrent"


def test_match_vektor_telegram_down():
    assert selfheal._match_rule("Healthcheck vektor-telegram down") == "bot_recreate"


@pytest.mark.asyncio
async def test_bot_recreate_declenche_le_watchdog_systemd(monkeypatch):
    import app.selfheal as sh

    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)

        class P:
            async def wait(self):
                return 0

        return P()

    monkeypatch.setattr(sh.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sh, "_tg", _noop_tg)
    monkeypatch.setattr(sh, "_LAST_RUN", {})
    payload = {
        "alerts": [{
            "title": "Healthcheck vektor-telegram down",
            "status": "firing",
            "labels": {"severity": "critical"},
        }]
    }
    summary = await sh.handle_grafana_webhook(payload)
    assert calls and calls[0][1] == "start"
    assert any("bot-watchdog" in a for a in summary["acted"])
    assert summary["reports_sent"] == 1


def test_match_media_alerts_critical_words():
    assert selfheal._match_rule("Jellyfin healthcheck DOWN") == "selfheal_media_jellyfin"
    assert selfheal._match_rule("Immich API unhealthy") == "selfheal_media_immich"
    assert selfheal._match_rule("Jellyfin healthy") is None


def test_match_authelia_alerts():
    assert selfheal._match_rule("Authelia SSO down") == "selfheal_media_authelia"
    assert selfheal._match_rule("Service Authelia unhealthy") == "selfheal_media_authelia"
    assert selfheal._match_rule("auth.mayoraz-net.ch indisponible") == "selfheal_media_authelia"
    assert selfheal._match_rule("SSO Authelia down") != "docker_restart_107_qbittorrent"
    # « Authelia sain » ne déclenche rien (pas de mot de panne).
    assert selfheal._match_rule("Authelia sain") is None
    # « auth » seul (token séparé) déclenche seulement avec un mot de panne.
    assert selfheal._match_rule("auth offline") == "selfheal_media_authelia"
    # Un titre sans mot de panne ne déclenche pas.
    assert selfheal._match_rule("Authelia latence élevée") is None


@pytest.mark.asyncio
async def test_media_selfheal_firing_retry_cooldown_attempt_limit_and_resolution(monkeypatch):
    reports = []
    actions_seen = []
    results = iter([
        "UNHEALTHY: jellyfin toujours indisponible après tentative 1/2",
        "ATTEMPT 2/2 HEALTHY: jellyfin récupéré au deuxième passage",
        "RESET: budget jellyfin réinitialisé",
    ])

    async def fake_execute(action):
        actions_seen.append(action)
        return next(results)

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    monkeypatch.setattr(selfheal, "_LAST_RUN", {})
    now = [100.0]
    monkeypatch.setattr(selfheal.time, "monotonic", lambda: now[0])
    firing = {"alerts": [{
        "fingerprint": "jellyfin-outage",
        "title": "Jellyfin healthcheck DOWN",
        "status": "firing",
        "startsAt": "2026-10-02T18:00:00+00:00",
        "labels": {"severity": "critical"},
    }]}

    await selfheal.handle_grafana_webhook(firing)
    now[0] += selfheal._COOLDOWN_S + 1
    await selfheal.handle_grafana_webhook(firing)  # cooldown expiré, seconde tentative
    await selfheal.handle_grafana_webhook(firing)  # Santé confirmée, aucun restart supplémentaire
    final = await selfheal.handle_grafana_webhook(firing)
    assert actions_seen == ["selfheal_media_jellyfin", "selfheal_media_jellyfin"]
    assert len(reports) == 2  # deux tentatives ont échoué puis réussi
    assert "échec de l’action" in reports[0]
    assert "deuxième passage" in reports[1]
    assert any("santé locale déjà confirmée" in item for item in final["skipped"])

    resolved = {"alerts": [{
        **firing["alerts"][0], "status": "resolved",
    }]}
    await selfheal.handle_grafana_webhook(resolved)
    assert actions_seen[-1] == "selfheal_media_reset_jellyfin"
    assert "rétablissement détecté" in reports[-1]
    assert "aucune restauration" in reports[-1]


@pytest.mark.asyncio
async def test_media_selfheal_escalates_after_two_failed_attempts(monkeypatch):
    reports = []
    actions_seen = []
    results = iter([
        "UNHEALTHY: Immich API still down after attempt 1/2",
        "MAX_ATTEMPTS: Immich failed after 2/2 attempts",
    ])
    now = [10.0]

    async def fake_execute(action):
        actions_seen.append(action)
        return next(results)

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    monkeypatch.setattr(selfheal.time, "monotonic", lambda: now[0])
    firing = {"alerts": [{
        "fingerprint": "immich-critical-failure",
        "title": "Immich API unhealthy",
        "status": "firing",
        "labels": {"severity": "critical"},
    }]}

    await selfheal.handle_grafana_webhook(firing)
    now[0] += selfheal._COOLDOWN_S + 1
    await selfheal.handle_grafana_webhook(firing)
    again = await selfheal.handle_grafana_webhook(firing)

    assert actions_seen == ["selfheal_media_immich", "selfheal_media_immich"]
    assert "MAX_ATTEMPTS" in reports[-1]
    assert "escalade opérateur" in reports[-1]
    assert any("rapport déjà envoyé" in item for item in again["skipped"])


@pytest.mark.asyncio
async def test_authelia_selfheal_firing_retry_and_resolution(monkeypatch):
    reports = []
    actions_seen = []
    results = iter([
        "ATTEMPT 1/2 HEALTHY: authelia redémarré et son contrôle local répond OK.",
        "RESET: budget authelia réinitialisé",
    ])

    async def fake_execute(action):
        actions_seen.append(action)
        return next(results)

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    monkeypatch.setattr(selfheal, "_LAST_RUN", {})
    firing = {"alerts": [{
        "fingerprint": "authelia-outage",
        "title": "Authelia SSO down",
        "status": "firing",
        "startsAt": "2026-10-03T12:00:00+00:00",
        "labels": {"severity": "critical"},
    }]}

    summary = await selfheal.handle_grafana_webhook(firing)
    assert actions_seen == ["selfheal_media_authelia"]
    assert len(reports) == 1
    assert summary["reports_sent"] == 1
    # Narration spécifique SSO : le redémarrage ne touche que le conteneur.
    assert "DB (CT 106)" in reports[0]
    # La mention sso-switch figure dans la cause probable.
    assert "sso-switch" in reports[0]

    resolved = {"alerts": [{**firing["alerts"][0], "status": "resolved"}]}
    await selfheal.handle_grafana_webhook(resolved)
    assert actions_seen[-1] == "selfheal_media_reset_authelia"
    assert "rétablissement détecté" in reports[-1]
    assert "DB (CT 106) et la configuration" in reports[-1]


@pytest.mark.asyncio
async def test_authelia_selfheal_escalates_after_two_failed_attempts(monkeypatch):
    reports = []
    actions_seen = []
    results = iter([
        "UNHEALTHY: authelia ne répond toujours pas après le restart (tentative 1/2)",
        "MAX_ATTEMPTS: authelia est toujours en panne après 2/2 tentatives ; escalade opérateur requise",
    ])
    now = [10.0]

    async def fake_execute(action):
        actions_seen.append(action)
        return next(results)

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    monkeypatch.setattr(selfheal.time, "monotonic", lambda: now[0])
    firing = {"alerts": [{
        "fingerprint": "authelia-hard-down",
        "title": "Authelia SSO down",
        "status": "firing",
        "labels": {"severity": "critical"},
    }]}

    await selfheal.handle_grafana_webhook(firing)
    now[0] += selfheal._COOLDOWN_S + 1
    await selfheal.handle_grafana_webhook(firing)
    again = await selfheal.handle_grafana_webhook(firing)

    assert actions_seen == ["selfheal_media_authelia", "selfheal_media_authelia"]
    assert "MAX_ATTEMPTS" in reports[-1]
    assert "escalade opérateur" in reports[-1]
    assert any("rapport déjà envoyé" in item for item in again["skipped"])


@pytest.mark.asyncio
async def test_authelia_selfheal_noncritical_does_not_execute(monkeypatch):
    reports = []

    async def fail_execute(action):
        raise AssertionError(f"unexpected authelia action: {action}")

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", fail_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    summary = await selfheal.handle_grafana_webhook({"alerts": [{
        "title": "Authelia SSO down", "status": "firing",
        "labels": {"severity": "warning"},
    }]})
    assert summary["acted"] == []
    assert "réservée aux alertes critiques" in reports[0]


@pytest.mark.asyncio
async def test_media_selfheal_noncritical_does_not_execute(monkeypatch):
    reports = []

    async def fail_execute(action):
        raise AssertionError(f"unexpected media action: {action}")

    async def capture_tg(text):
        reports.append(text)
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", fail_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    summary = await selfheal.handle_grafana_webhook({"alerts": [{
        "title": "Immich API down", "status": "firing",
        "labels": {"severity": "warning"},
    }]})
    assert summary["acted"] == []
    assert "réservée aux alertes critiques" in reports[0]


@pytest.mark.asyncio
async def test_media_selfheal_repeat_distinct_fingerprint_shares_pve_cooldown(monkeypatch):
    calls = []

    async def fake_execute(action):
        calls.append(action)
        return "COOLDOWN: immich, prochaine tentative possible dans 1000s"

    async def capture_tg(text):
        return True

    monkeypatch.setattr(selfheal.actions, "_execute", fake_execute)
    monkeypatch.setattr(selfheal, "_tg", capture_tg)
    monkeypatch.setattr(selfheal, "_ACTIVE_ALERTS", {})
    monkeypatch.setattr(selfheal, "_LAST_RUN", {})
    payload = {"alerts": [{
        "fingerprint": "immich-outage-1", "title": "Immich API down",
        "status": "firing", "labels": {"severity": "critical"},
    }]}
    await selfheal.handle_grafana_webhook(payload)
    payload["alerts"][0]["fingerprint"] = "immich-outage-2"
    summary = await selfheal.handle_grafana_webhook(payload)
    assert calls == ["selfheal_media_immich", "selfheal_media_immich"]
    assert summary["acted"] == []
    assert "cooldown" in summary["skipped"][0]
