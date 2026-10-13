"""Tenant-isolated, read-only retrieval (CC6.1, CC6.3, C1.1).

Two guarantees, both enforced in code and verified at startup:
1. The database role can only SELECT (checked via information_schema), and every
   transaction is opened READ ONLY.
2. Every query carries ``WHERE tenant_id = %s``; the tenant comes from the
   caller's verified token, never from the request body.

Retrieved chunks are returned with IDs and content hashes so the audit record
can state exactly which knowledge was used for a given draft.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    tenant_id: str
    source: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class Retriever(Protocol):
    def retrieve(self, tenant_id: str, query: str, top_k: int) -> list[Chunk]: ...


class InMemoryRetriever:
    """Dev/test retriever. Same tenant filter semantics as the pgvector one."""

    def __init__(self, chunks: list[Chunk]):
        self._chunks = chunks

    def retrieve(self, tenant_id: str, query: str, top_k: int) -> list[Chunk]:
        terms = {t.lower() for t in query.split() if len(t) > 3}
        scored = [
            (sum(1 for t in terms if t in c.text.lower()), c)
            for c in self._chunks
            if c.tenant_id == tenant_id  # tenant isolation
        ]
        scored.sort(key=lambda s: s[0], reverse=True)
        return [c for score, c in scored[:top_k] if score > 0]


class PgVectorRetriever:
    """Production retriever over Postgres + pgvector with a SELECT-only role."""

    def __init__(self, dsn: str, embed, table: str = "kb_chunks"):
        import psycopg

        self._conn = psycopg.connect(dsn, autocommit=False)
        self._embed = embed  # callable(str) -> list[float]
        self._table = table
        self._assert_read_only()

    def _assert_read_only(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT privilege_type FROM information_schema.role_table_grants
                WHERE grantee = current_user AND table_name = %s
                """,
                (self._table,),
            )
            privs = {row[0] for row in cur.fetchall()}
        self._conn.rollback()
        forbidden = privs - {"SELECT"}
        if forbidden:
            raise RuntimeError(
                f"RAG role {self._conn.info.user!r} has {sorted(forbidden)} on {self._table}; must be SELECT only"
            )

    def retrieve(self, tenant_id: str, query: str, top_k: int) -> list[Chunk]:
        vec = self._embed(query)
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                f"""
                    SELECT chunk_id, tenant_id, source, content
                    FROM {self._table}
                    WHERE tenant_id = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                (tenant_id, vec, top_k),
            )
            rows = cur.fetchall()
        chunks = [Chunk(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]
        # Defence in depth: never trust the DB alone.
        leaked = [c for c in chunks if c.tenant_id != tenant_id]
        if leaked:
            raise RuntimeError("tenant isolation violation detected in retrieval results")
        return chunks
