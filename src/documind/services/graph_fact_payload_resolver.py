"""Production graph-fact payload resolver for Neo4j projections.

Resolves ``GraphFactPayload`` values from PostgreSQL canonical data
by joining facts with entities, chunks, document versions, and label
associations.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from documind.models.graph import GraphEntity, GraphFact
from documind.services.graph_service import GraphFactPayload
from documind.services.projection_service import ProjectionSnapshot

logger = logging.getLogger(__name__)


class PostgresGraphFactPayloadResolver:
    """Production ``GraphFactPayloadResolver`` backed by PostgreSQL.

    Resolves full graph-fact payloads including entity identities,
    source provenance, chunk metadata, and label associations.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def resolve_graph_payloads(
        self, snapshot: ProjectionSnapshot,
    ) -> list[GraphFactPayload]:
        """Resolve full payloads for all fact-type records in the snapshot."""
        fact_ids = [
            record.deterministic_id
            for record in snapshot.records
            if record.projection_type == "fact"
        ]
        if not fact_ids:
            return []

        async with self._session_factory() as session:
            facts = await self._load_facts(session, fact_ids)
            # Pre-load entity data for subjects and objects
            entity_ids = set()
            for fact in facts:
                entity_ids.add(fact.subject_entity_id)
                if fact.object_entity_id is not None:
                    entity_ids.add(fact.object_entity_id)

            entities = await self._load_entities(session, list(entity_ids))
            entity_map = {e.id: e for e in entities}

            # Load chunk metadata for source provenance
            chunk_ids = {fact.source_chunk_id for fact in facts}
            chunk_map = await self._load_chunk_metadata(session, list(chunk_ids))

        payloads = []
        for fact in facts:
            subject = entity_map.get(fact.subject_entity_id)
            obj = entity_map.get(fact.object_entity_id) if fact.object_entity_id else None
            chunk_meta = chunk_map.get(fact.source_chunk_id, {})

            payloads.append(
                GraphFactPayload(
                    fact_id=str(fact.id),
                    subject_entity_type=subject.entity_type if subject else "",
                    subject_normalized_key=subject.normalized_key if subject else "",
                    subject_display_value=subject.display_value if subject else "",
                    predicate_key=fact.predicate_key,
                    object_entity_type=obj.entity_type if obj else None,
                    object_normalized_key=obj.normalized_key if obj else None,
                    object_display_value=obj.display_value if obj else None,
                    object_literal=fact.object_literal if fact.object_literal else None,
                    source_chunk_id=str(fact.source_chunk_id),
                    source_version_id=str(fact.source_version_id),
                    source_document_id=chunk_meta.get("document_id", ""),
                    confidence=float(fact.confidence),
                    corroboration_count=fact.corroboration_count,
                    extraction_revision=str(fact.extraction_route_revision_id),
                    tombstone_generation=fact.tombstone_generation,
                    generation=snapshot.generation,
                    chunk_page_start=chunk_meta.get("page_start"),
                    chunk_page_end=chunk_meta.get("page_end"),
                    chunk_section_path=chunk_meta.get("section_path"),
                    chunk_content_hash=chunk_meta.get("content_sha256", ""),
                    chunk_lifecycle=chunk_meta.get("lifecycle", ""),
                    label_ids=chunk_meta.get("label_ids"),
                )
            )

        logger.info("Resolved %d graph-fact payloads for snapshot %s", len(payloads), snapshot.snapshot_id[:12])
        return payloads

    @staticmethod
    async def _load_facts(session: AsyncSession, fact_ids: list[str]) -> list[Any]:
        """Load facts by deterministic IDs."""
        import uuid as _uuid

        parsed_ids = []
        for fid in fact_ids:
            try:
                parsed_ids.append(_uuid.UUID(fid))
            except ValueError:
                logger.warning("Skipping invalid fact ID: %s", fid)

        if not parsed_ids:
            return []

        result = await session.execute(
            select(GraphFact).where(GraphFact.id.in_(parsed_ids))
        )
        return list(result.scalars().all())

    @staticmethod
    async def _load_entities(session: AsyncSession, entity_ids: list[Any]) -> list[Any]:
        """Load entities by IDs."""
        if not entity_ids:
            return []
        result = await session.execute(
            select(GraphEntity).where(GraphEntity.id.in_(entity_ids))
        )
        return list(result.scalars().all())

    @staticmethod
    async def _load_chunk_metadata(
        session: AsyncSession, chunk_ids: list[Any],
    ) -> dict[Any, dict[str, Any]]:
        """Load chunk metadata needed for graph provenance."""
        from documind.models.chunk import DocumentChunk

        if not chunk_ids:
            return {}

        result = await session.execute(
            select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))
        )
        chunks = result.scalars().all()

        meta: dict[Any, dict[str, Any]] = {}
        for chunk in chunks:
            label_ids: list[str] = []
            raw_labels = getattr(chunk, "labels", None)
            if raw_labels is not None and not callable(raw_labels):
                label_ids = [str(getattr(lbl, "id", lbl)) for lbl in raw_labels]

            meta[chunk.id] = {
                "document_id": str(getattr(chunk, "document_id", "")),
                "page_start": getattr(chunk, "page_start", None),
                "page_end": getattr(chunk, "page_end", None),
                "section_path": getattr(chunk, "section_path", None),
                "content_sha256": getattr(chunk, "content_sha256", ""),
                "lifecycle": getattr(chunk, "lifecycle", ""),
                "label_ids": label_ids,
            }
        return meta
