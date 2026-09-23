"""Tests du RAG enrichi : chunking sections, re-ranking IDF, détection hors sujet."""

from app.rag import _chunks_from_text, load_chunks, rank_chunks, retrieve_context


# ── Chunking par sections ────────────────────────────────────────────────

def test_chunking_conserve_le_titre_de_section():
    text = "# Réseau\n\nLe DNS local filtre les publicités.\n\n## VLAN\n\nVLAN 10 = invités.\n"
    chunks = _chunks_from_text(text)
    assert any(c.startswith("Réseau\nLe DNS local") for c in chunks)
    assert any(c.startswith("VLAN\nVLAN 10") for c in chunks)


def test_chunking_section_trop_longue_re_decoupee_par_paragraphe():
    paragraph = "Un paragraphe de test avec des termes techniques jellyfin. " * 40
    text = f"# Média\n\n{paragraph}\n\n{paragraph}\n"
    chunks = _chunks_from_text(text)
    assert len(chunks) >= 2  # re-découpé, pas un seul blob
    assert all(len(c) <= 1700 for c in chunks)


def test_chunking_titre_sans_corps_restera_cherchable():
    chunks = _chunks_from_text("# Table des matières\n\n# Réseau\n\nDu contenu.\n")
    assert "Table des matières" in chunks


def test_load_chunks_ignore_les_appledouble(tmp_path):
    # ._doc.md = métadonnées macOS binaires, à ne jamais indexer
    (tmp_path / "._doc.md").write_bytes(b"\x00\x05\x16\x07macOS binary")
    (tmp_path / "doc.md").write_text("# Sujet\n\nContenu utile.\n", encoding="utf-8")
    chunks = load_chunks(str(tmp_path))
    sources = [source for source, _, _ in chunks]
    assert all(not s.endswith("._doc.md") for s in sources)
    assert any(s.endswith("doc.md") for s in sources)
    assert all("\x00" not in content for _, content, _ in chunks)


# ── Re-ranking ────────────────────────────────────────────────────────────

def _fake_chunks():
    return [
        ("knowledge/a.md", "Jellyfin\nLe serveur média transcode avec la GPU.", ["jellyfin", "serveur", "média", "transcode", "gpu"]),
        ("knowledge/b.md", "Un terme jellyfin au milieu d'un paragraphe sur les sauvegardes nightly.", ["terme", "jellyfin", "milieu", "paragraphe", "sauvegardes", "nightly"]),
        ("knowledge/c.md", "qBittorrent gère les torrents en pause.", ["qbittorrent", "gère", "torrents", "pause"]),
    ]


def test_ranking_terme_rare_pese_plus_que_terme_commun():
    chunks = [
        ("k/x.md", "A", ["le", "un"]),           # termes très fréquents
        ("k/y.md", "B", ["wireguard"]),           # terme rare
    ]
    ranked = rank_chunks("comment marche wireguard", chunks)
    assert ranked and ranked[0][1]["source"] == "k/y.md"


def test_ranking_bonus_titre_de_section():
    ranked = rank_chunks("jellyfin transcode", _fake_chunks())
    assert ranked[0][1]["source"] == "knowledge/a.md"  # titre + corps
    assert ranked[1][1]["source"] == "knowledge/b.md"  # corps seulement


def test_ranking_aucun_terme_communaux():
    assert rank_chunks("recette de cuisine", _fake_chunks()) == []


# ── Détection « la doc ne répond pas » ───────────────────────────────────

class FakeMemory:
    def __init__(self, chunks):
        self._chunks = chunks

    async def search_knowledge(self, query, limit=5):
        return self._chunks


async def test_hors_sujet_dit_explicitement_que_la_doc_ne_couvre_pas():
    memory = FakeMemory(_fake_chunks())
    context = await retrieve_context(memory, "question totalement hors infra")
    assert "ne couvre pas" in context


async def test_sujet_couvra_injecte_les_extraits():
    memory = FakeMemory(_fake_chunks())
    context = await retrieve_context(memory, "jellyfin transcode comment")
    assert "knowledge/a.md" in context
    assert "ne couvre pas" not in context


async def test_doc_vide_dit_ne_couvre_pas():
    memory = FakeMemory([])
    context = await retrieve_context(memory, "état de proxmox")
    assert "ne couvre pas" in context


# ── Régression v1.3.1 : contrat memory <-> rag ────────────────────────────
# search_knowledge (prod) renvoie des tuples (source, content, terms) comme
# load_chunks — pas des dicts. Un mock qui ment sur ce contrat a déjà cassé
# le chat LLM en prod (ValueError unpack). Ce test fige le contrat.

async def test_contrat_search_knowledge_renvoie_des_tuples_3_elements():
    chunks = _fake_chunks()
    assert all(isinstance(c, tuple) and len(c) == 3 for c in chunks), (
        "load_chunks doit renvoyer (source, content, terms)"
    )
    memory = FakeMemory(chunks)
    # ne doit PAS lever ValueError : c'est la régression v1.3.0
    context = await retrieve_context(memory, "jellyfin transcode")
    assert "knowledge/a.md" in context


async def test_retrieve_context_avec_chunks_memorie_reels():
    """Forme EXACTE renvoyée par memory.search_knowledge en prod (asyncpg)."""
    prod_shape = [
        ("knowledge/homelab.md", "## Wireguard\nWireguard est le VPN du mesh.", ["wireguard", "vpn"]),
        ("knowledge/homelab.md", "## DNS\nPi-hole filtre les pubs.", ["dns", "pihole"]),
    ]
    context = await retrieve_context(FakeMemory(prod_shape), "vpn wireguard config")
    assert "Wireguard" in context
