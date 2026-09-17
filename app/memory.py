from uuid import UUID, uuid4
import asyncpg
from .config import get_settings


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_conversation_idx
    ON messages (conversation_id, created_at);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    terms TEXT[] NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_source_content_idx
    ON knowledge_chunks (source, content);
"""


class Memory:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(get_settings().database_url, min_size=1, max_size=5)
        async with self.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def ensure_conversation(self, conversation_id: str | None, user_id: str, channel: str) -> UUID:
        assert self.pool
        cid = UUID(conversation_id) if conversation_id else uuid4()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO conversations (id,user_id,channel) VALUES ($1,$2,$3) ON CONFLICT (id) DO NOTHING",
                cid, user_id, channel,
            )
        return cid

    async def add_message(self, conversation_id: UUID, role: str, content: str) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages (conversation_id,role,content) VALUES ($1,$2,$3)",
                conversation_id, role, content,
            )

    async def history(self, conversation_id: UUID, limit: int = 12) -> list[dict[str, str]]:
        assert self.pool
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM messages WHERE conversation_id=$1 ORDER BY created_at DESC LIMIT $2",
                conversation_id, limit,
            )
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    async def forget_user(self, user_id: str) -> int:
        assert self.pool
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM conversations WHERE user_id=$1",
                user_id,
            )
            ids = [row["id"] for row in rows]
            if ids:
                await conn.execute(
                    "DELETE FROM messages WHERE conversation_id = ANY($1)",
                    ids,
                )
                await conn.execute(
                    "DELETE FROM conversations WHERE user_id=$1",
                    user_id,
                )
            return len(ids)

    async def replace_knowledge(self, chunks: list[tuple[str, str, list[str]]]) -> None:
        assert self.pool
        async with self.pool.acquire() as conn:
            await conn.execute("TRUNCATE knowledge_chunks")
            await conn.executemany(
                "INSERT INTO knowledge_chunks (source,content,terms) VALUES ($1,$2,$3)",
                chunks,
            )

    async def search_knowledge(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        assert self.pool
        terms = {part.lower() for part in query.split() if len(part) > 2}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT source, content, terms FROM knowledge_chunks")
        ranked = []
        for row in rows:
            score = len(terms.intersection(set(row["terms"])))
            if score:
                ranked.append((score, {"source": row["source"], "content": row["content"]}))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in ranked[:limit]]
