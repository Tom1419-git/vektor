"""RAG enrichi.

Améliorations V1 -> V1.2 (chantier #4 de la roadmap) :
1. Chunking par sections markdown (titre hérité) avec fallback paragraphe :
   chaque extrait garde son contexte au lieu d'être coupé à l'aveugle.
2. Re-ranking pondéré : le score croise la couverture des termes de la
   requête (pondérée par IDF — les termes rares pèsent plus) et la présence
   dans le titre de la section.
3. Détection « la doc ne répond pas » : si aucun extrait ne dépasse un score
   minimal, retrieve_context le dit explicitement au LLM au lieu d'injecter
   des extraits hors sujet (limite l'hallucination documentaire).

La table SQL (source, content, terms) est inchangée : le contenu des chunks
et le scoring évoluent, pas le schéma ni l'API publique.
"""

from pathlib import Path
import math
import re

from .memory import Memory

# Titres markdown (# à ####) : délimitent les sections.
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
# Longueurs de chunk : une section plus longue est re-découpée par paragraphe.
_MAX_CHUNK_CHARS = 1600
_MIN_SCORE = 1.0  # seuil de « la doc répond » (somme d'IDF, empirique)


def _terms(text: str) -> list[str]:
    return sorted(set(re.findall(r"[a-z0-9][a-z0-9._:-]{2,}", text.lower())))


def _chunks_from_text(text: str) -> list[str]:
    """Découpe par sections markdown ; fallback paragraphe si hors limite."""
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []  # (titre, corps)
    title = ""
    body: list[str] = []
    for line in lines:
        match = _HEADING.match(line)
        if match:
            if title or any(part.strip() for part in body):
                sections.append((title, body))
            title = match.group(2).strip()
            body = []
        else:
            body.append(line)
    sections.append((title, body))

    chunks: list[str] = []
    for section_title, section_body in sections:
        text_section = "\n".join(section_body).strip()
        if not text_section:
            # Un titre sans corps : le titre seul reste cherchable.
            if section_title:
                chunks.append(section_title)
            continue
        if len(text_section) <= _MAX_CHUNK_CHARS:
            chunks.append(f"{section_title}\n{text_section}" if section_title else text_section)
            continue
        # Section trop longue : re-découpe paragraphe, titre préfixé.
        for paragraph in re.split(r"\n\s*\n", text_section):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            prefix = f"{section_title}\n" if section_title else ""
            # Paragraphe démesuré : coupure dure aux frontières de mots.
            for start in range(0, len(paragraph), _MAX_CHUNK_CHARS - len(prefix)):
                window = paragraph[start : start + _MAX_CHUNK_CHARS - len(prefix)].strip()
                if window:
                    chunks.append(prefix + window)
    return chunks


def load_chunks(root: str = "knowledge") -> list[tuple[str, str, list[str]]]:
    chunks: list[tuple[str, str, list[str]]] = []
    for path in Path(root).rglob("*.md"):
        # Fichiers AppleDouble macOS (._doc.md) : métadonnées binaires qui
        # contiennent des octets NUL — refusés par PostgreSQL. Jamais de la doc.
        if path.name.startswith("._"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Ceinture et bretelles : NUL est UTF-8 valide mais interdit en base.
        text = text.replace("\x00", "")
        for chunk in _chunks_from_text(text):
            chunks.append((str(path), chunk, _terms(chunk)))
    return chunks


async def index_knowledge(memory: Memory) -> int:
    chunks = load_chunks()
    await memory.replace_knowledge(chunks)
    return len(chunks)


def _idf(chunks: list[tuple[str, str, list[str]]]) -> dict[str, float]:
    """IDF simple : un terme présent dans peu de chunks pèse plus lourd."""
    total = max(len(chunks), 1)
    document_frequency: dict[str, int] = {}
    for _, _, terms in chunks:
        for term in terms:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    return {
        term: math.log((total + 1) / (count + 0.5))
        for term, count in document_frequency.items()
    }


def rank_chunks(
    query: str,
    chunks: list[tuple[str, str, list[str]]],
    limit: int = 5,
) -> list[tuple[float, dict[str, str]]]:
    """Score = couverture des termes pondérée IDF + bonus titre de section.

    Renvoie [(score, {source, content})] triée par score décroissant. Un
    chunk ne score que s'il couvre au moins un terme de la requête.
    """
    query_terms = {part.lower() for part in re.split(r"\s+", query.strip()) if len(part) > 2}
    if not query_terms or not chunks:
        return []
    idf = _idf(chunks)

    ranked: list[tuple[float, dict[str, str]]] = []
    for source, content, terms in chunks:
        first_line = content.split("\n", 1)[0].lower()
        score = 0.0
        title_hits = 0
        for term in query_terms:
            if term not in terms:
                continue
            weight = idf.get(term, 0.0) or 0.5
            score += weight
            if term in first_line:  # le terme est dans le titre de section
                score += weight * 0.5
                title_hits += 1
        # Bonus « bon chunk » : la section porte le nom du service demandé
        # (ex. « Healthchecks » dans le titre d'une question sur le port HC)
        # — évite qu'un chunk voisin mentionnant le terme en passant gagne.
        if title_hits >= 2:
            score += 0.25 * score
        if score > 0:
            ranked.append((score, {"source": source, "content": content}))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[:limit]


async def retrieve_context(memory: Memory, query: str) -> str:
    chunks = await memory.search_knowledge(query, limit=0)  # 0 = tout, re-ranking ici
    ranked = rank_chunks(query, chunks)
    if not ranked or ranked[0][0] < _MIN_SCORE:
        return (
            "Aucun extrait de documentation ne correspond clairement à la question. "
            "Ne devine pas : dis que la documentation ne couvre pas ce sujet."
        )
    return "\n\n".join(f"[{item['source']}]\n{item['content']}" for _, item in ranked)
