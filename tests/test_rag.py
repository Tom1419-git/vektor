"""Tests du RAG V1 : extraction de termes, chargement des chunks."""

from app.rag import _terms, load_chunks


def test_terms_ignore_les_mots_trops_courts():
    terms = _terms("Le CT de jellyfin tourne sur pve.lan:8096")
    assert "ct" not in terms  # 2 caractères : exclu
    assert any(t.startswith("pve.lan") for t in terms)


def test_terms_sont_uniques_et_minuscules():
    terms = _terms("Jellyfin Jellyfin JELLYFIN")
    assert terms == ["jellyfin"]


def test_load_chunks_trouve_le_template():
    chunks = load_chunks()
    sources = {source for source, _, _ in chunks}
    assert any("homelab.md" in source for source in sources)
    # Chaque chunk porte ses termes
    assert all(chunks_terms for _, _, chunks_terms in chunks)
