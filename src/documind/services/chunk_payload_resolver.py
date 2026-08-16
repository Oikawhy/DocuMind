"""Production chunk payload resolver for Qdrant and OpenSearch projections.

Resolves ``ChunkProjectionPayload`` values from PostgreSQL canonical
data by joining chunks with document versions, documents, and label
associations.  Optionally attaches BGE-M3 embeddings when an embedder
is provided.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.chunk import DocumentChunk
from documind.services.indexing_service import ChunkProjectionPayload
from documind.services.projection_service import ProjectionSnapshot, SnapshotRecord

logger = logging.getLogger(__name__)


class DenseEmbedder(Protocol):
    """Produce dense vectors for a batch of texts."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def dimension(self) -> int: ...

    @property
    def model_digest(self) -> str: ...


class PostgresChunkPayloadResolver:
    """Production ``ChunkPayloadResolver`` backed by PostgreSQL.

    Resolves full chunk payloads including document metadata, label IDs,
    lifecycle state, and optionally attaches embedding vectors.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        embedder: DenseEmbedder | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder

    async def resolve_chunk_payloads(
        self, snapshot: ProjectionSnapshot,
    ) -> list[ChunkProjectionPayload]:
        """Resolve full payloads for all chunk-type records in the snapshot."""
        chunk_ids = [
            record.deterministic_id
            for record in snapshot.records
            if record.projection_type == "chunk"
        ]
        if not chunk_ids:
            return []

        async with self._session_factory() as session:
            chunks = await self._load_chunks(session, chunk_ids)

        payloads = [self._to_payload(chunk) for chunk in chunks]

        # Attach embeddings if an embedder is available
        if self._embedder is not None and payloads:
            texts = [p.content for p in payloads]
            vectors = await self._embedder.embed(texts)
            payloads = [
                ChunkProjectionPayload(
                    chunk_id=p.chunk_id,
                    version_id=p.version_id,
                    document_id=p.document_id,
                    content=p.content,
                    content_hash=p.content_hash,
                    profile_revision=p.profile_revision,
                    label_ids=p.label_ids,
                    lifecycle=p.lifecycle,
                    tombstone_generation=p.tombstone_generation,
                    locale=p.locale,
                    declared_type=p.declared_type,
                    page_start=p.page_start,
                    page_end=p.page_end,
                    section_path=p.section_path,
                    embedding=vectors[i],
                )
                for i, p in enumerate(payloads)
            ]
            logger.info(
                "Embedded %d chunks (%d-dim, digest=%s)",
                len(payloads),
                self._embedder.dimension,
                self._embedder.model_digest[:16],
            )

        return payloads

    @staticmethod
    async def _load_chunks(
        session: AsyncSession, chunk_ids: list[str],
    ) -> list[Any]:
        """Load chunks from PostgreSQL by their deterministic IDs."""
        import uuid as _uuid

        parsed_ids = []
        for cid in chunk_ids:
            try:
                parsed_ids.append(_uuid.UUID(cid))
            except ValueError:
                logger.warning("Skipping invalid chunk ID: %s", cid)

        if not parsed_ids:
            return []

        result = await session.execute(
            select(DocumentChunk)
            .where(DocumentChunk.id.in_(parsed_ids))
            .order_by(DocumentChunk.chunk_index)
        )
        return list(result.scalars().all())

    @staticmethod
    def _to_payload(chunk: Any) -> ChunkProjectionPayload:
        """Map a DocumentChunk ORM row to a ChunkProjectionPayload."""
        # Extract label IDs from relationship or attribute
        label_ids: list[str] = []
        raw_labels = getattr(chunk, "labels", None)
        if raw_labels is not None and not callable(raw_labels):
            label_ids = [str(getattr(lbl, "id", lbl)) for lbl in raw_labels]

        return ChunkProjectionPayload(
            chunk_id=str(chunk.id),
            version_id=str(getattr(chunk, "version_id", "")),
            document_id=str(getattr(chunk, "document_id", "")),
            content=getattr(chunk, "content", "") or "",
            content_hash=getattr(chunk, "content_sha256", "") or "",
            profile_revision=str(getattr(chunk, "profile_revision", "") or ""),
            label_ids=label_ids,
            lifecycle=getattr(chunk, "lifecycle", "active") or "active",
            tombstone_generation=getattr(chunk, "tombstone_generation", 0) or 0,
            locale=getattr(chunk, "locale", None),
            declared_type=getattr(chunk, "declared_type", None),
            page_start=getattr(chunk, "page_start", None),
            page_end=getattr(chunk, "page_end", None),
            section_path=getattr(chunk, "section_path", None),
            embedding=None,  # Set later if embedder is available
        )
