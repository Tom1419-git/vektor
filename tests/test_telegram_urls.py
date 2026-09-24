"""Régression : les URLs par défaut du bot pointent vers le service API.

Bug v1.3.5 : les défauts `http://127.0.0.1:8000/...` laissaient /dns,
/seeds, /backups, /model, /monitoring et /reload muets en production —
dans le conteneur telegram, 127.0.0.1 est SON propre loopback, où aucune
API n'écoute (« Impossible de sonder les résolveurs »). Toute nouvelle
constante d'URL du bot doit suivre le même contrat.
"""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.telegram as tg  # noqa: E402

# constante -> (endpoint attendu, variable d'environnement)
EXPECTED = {
    "API_URL": ("/api/chat", "VEKTOR_API_URL"),
    "STATUS_URL": ("/api/status", "VEKTOR_STATUS_URL"),
    "MODEL_URL": ("/api/model", "VEKTOR_MODEL_URL"),
    "SEEDS_URL": ("/api/seeds", "VEKTOR_SEEDS_URL"),
    "BACKUPS_URL": ("/api/backups", "VEKTOR_BACKUPS_URL"),
    "DNS_URL": ("/api/dns", "VEKTOR_DNS_URL"),
    "MONITORING_URL": ("/api/monitoring", "VEKTOR_MONITORING_URL"),
    "PING_URL": ("/api/ping", "VEKTOR_PING_URL"),
    "RELOAD_URL": ("/api/reload-doc", "VEKTOR_RELOAD_URL"),
    "FORGET_URL": ("/api/forget", "VEKTOR_FORGET_URL"),
}


@pytest.mark.parametrize("attr", sorted(EXPECTED))
def test_default_urls_target_the_api_service(attr):
    endpoint, _env = EXPECTED[attr]
    url = getattr(tg, attr)
    assert url.endswith(endpoint), f"{attr}={url} : endpoint inattendu"
    host = url[: -len(endpoint)].rstrip("/")
    assert host.endswith("vektor-api:8000"), (
        f"{attr}={url} : l'hôte par défaut doit être le service Docker "
        "vektor-api (127.0.0.1 est le loopback du conteneur telegram, "
        "muet en production)"
    )
    assert "127.0.0.1" not in url and "localhost" not in url


def test_env_override_beats_default(monkeypatch):
    """L'environnement compose garde la priorité sur les défauts."""
    for _attr, (endpoint, env) in EXPECTED.items():
        monkeypatch.setenv(env, f"http://override:1234{endpoint}")
    try:
        importlib.reload(tg)
        for attr, (endpoint, _env) in EXPECTED.items():
            assert getattr(tg, attr) == f"http://override:1234{endpoint}"
    finally:
        for _attr, (_endpoint, env) in EXPECTED.items():
            monkeypatch.delenv(env, raising=False)
        importlib.reload(tg)
