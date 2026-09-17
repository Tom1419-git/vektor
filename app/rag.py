from pathlib import Path
import re
from .memory import Memory


def _terms(text: str) -> list[str]:
    return sorted(set(re.findall(r"[a-z0-9][a-z0-9._:-]{2,}", text.lower())))


def load_chunks(root: str = "knowledge") -> list[tuple[str, str, list[str]]]:
    chunks = []
    for path in Path(root).rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        for paragraph in paragraphs:
            chunks.append((str(path), paragraph, _terms(paragraph)))
    return chunks


async def index_knowledge(memory: Memory) -> int:
    chunks = load_chunks()
    await memory.replace_knowledge(chunks)
    return len(chunks)


async def retrieve_context(memory: Memory, query: str) -> str:
    results = await memory.search_knowledge(query)
    if not results:
        return "Aucun document pertinent trouvé dans la documentation."
    return "\n\n".join(f"[{item['source']}]\n{item['content']}" for item in results)
