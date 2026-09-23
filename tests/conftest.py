"""Environnement de test : sys.path vers la racine et variables minimales.

Les variables définies ici sont des valeurs de test sans rapport avec le
déploiement réel — aucun secret du homelab n'apparaît dans ce dépôt.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Les modules app.* lisent l'environnement à l'import : fixer les valeurs
# avant toute importation de test (aucune valeur réelle ici).
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("VEKTOR_API_TOKEN", "test-token")
os.environ.setdefault("VEKTOR_SERVICES", "jellyfin=http://svc.test:8096,sonarr=http://svc.test:8989")
os.environ.setdefault("OLLAMA_BASE_URL", "http://ollama.test:11434")
os.environ.setdefault("OLLAMA_MODEL", "test-model:latest")


class FakeLLM:
    """Double de ChatOllama : renvoie une réponse fixe, sans inférence."""

    def __init__(self, content: str = "réponse du modèle") -> None:
        self.content = content
        self.calls: list[list] = []
        self.bound: list = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return _FakeResult(self.content)

    def bind_tools(self, tools):
        self.bound.append(list(tools))
        return self


class _FakeResult:
    def __init__(self, content: str) -> None:
        self.content = content


@pytest.fixture
def fake_llm():
    return FakeLLM()
