"""v1.5.6 : bonus titre ≥2 termes — le chunk dont le TITRE porte le service
demandé doit gagner sur un chunk voisin qui ne mentionne le terme qu'en
passant (régression : le port Healthchecks ressortait depuis le mauvais chunk)."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.rag import load_chunks, rank_chunks  # noqa: E402

DOC = """## Healthchecks :8010

Le service Healthchecks écoute sur le port 8010 sur le CT tools.

## Panneau de bord

Un lien vers healthchecks est affiché sur le portail.
"""


def test_bonus_titre_porte_le_service_demande():
    """2 termes de la requête dans le titre = chunk « bon » (bonus +25 %)."""
    d = tempfile.mkdtemp()
    open(os.path.join(d, "doc.md"), "w", encoding="utf-8").write(DOC)
    chunks = load_chunks(d)
    ranked = rank_chunks("healthchecks port", chunks)
    assert ranked, "aucun chunk scoré"
    top = ranked[0][1]["content"]
    assert top.startswith("Healthchecks :8010"), (
        f"le chunk « titre porte le service » doit gagner, obtenu : {top[:60]!r}"
    )
    # et le chunk « passing » reste scoré mais derrière
    assert len(ranked) == 2


def test_bonus_ne_sapplique_pas_aux_voisins():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "doc.md"), "w", encoding="utf-8").write(DOC)
    chunks = load_chunks(d)
    ranked = rank_chunks("healthchecks port", chunks)
    scores = [s for s, _ in ranked]
    # écart net : le bonus creuse la différence (pas de quasi-égalité)
    assert scores[0] > scores[1] * 2
