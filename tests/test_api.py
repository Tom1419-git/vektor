"""Tests de l'API FastAPI : santé, authentification par token, validation."""

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


async def test_health_sans_auth(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_chat_refuse_sans_token(client):
    response = await client.post("/api/chat", json={"user_id": "u", "channel": "telegram", "text": "salut"})
    assert response.status_code == 401


async def test_status_refuse_sans_token(client):
    response = await client.get("/api/status")
    assert response.status_code == 401


async def test_chat_valide_le_canal(client):
    response = await client.post(
        "/api/chat",
        json={"user_id": "u", "channel": "carrier-pigeon", "text": "salut"},
        headers={"X-Vektor-Token": "test-token"},
    )
    assert response.status_code == 422
