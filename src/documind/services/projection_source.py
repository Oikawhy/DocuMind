"""PostgreSQL-backed canonical projection source.

Implements the ``CanonicalProjectionSource`` protocol from
:mod:`projection_service` by resolving frozen snapshots from the
``document_chunk`` and ``graph_fact`` tables.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.services.projection_service import (
    ProjectionSnapshot,
    SnapshotRecord,
)

logger = logging.getLogger(__name__)


class PostgresCanonicalSource:
    """Resolve a canonical projection snapshot from PostgreSQL tables.

    Satisfies the ``CanonicalProjectionSource`` protocol.  Builds
    ``SnapshotRecord`` tuples from chunk and fact rows for the version
    identified by the snapshot ID.

    Snapshot ID conventions:
    - Ingestion: the version UUID string (resolves that version's data)
    - Rebuild:   ``rebuild-{backend}-full`` or ``rebuild-{backend}-{version_id}``
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        generation: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._generation = generation

    async def resolve_snapshot(self, snapshot_id: str) -> ProjectionSnapshot:
        """Build a frozen snapshot from canonical chunk and fact data.

        The ``snapshot_id`` is parsed to determine scope:
        - A valid UUID string → version-scoped ingestion snapshot
        - ``rebuild-{backend}-full`` → full-corpus rebuild snapshot
        - ``rebuild-{backend}-{version_id}`` → version-scoped rebuild

        Returns a ``ProjectionSnapshot`` with records derived from
        chunks (for Qdrant/OpenSearch projections) and facts (for Neo4j).
        """
        from documind.models.chunk import DocumentChunk
        from documind.models.graph import GraphFact

        version_id, is_rebuild = self._parse_snapshot_id(snapshot_id)

        records: list[SnapshotRecord] = []

        async with self._session_factory() as session:
            # Resolve chunk records for vector/search projections
            chunk_query = select(DocumentChunk).where(
                DocumentChunk.tombstone_generation == 0,
            ).order_by(DocumentChunk.chunk_index)

            if version_id is not None:
                chunk_query = chunk_query.where(
                    DocumentChunk.version_id == version_id,
                )

            chunk_rows = (await session.execute(chunk_query)).scalars().all()

            for chunk in chunk_rows:
                payload_hash = _chunk_payload_hash(chunk)
                records.append(
                    SnapshotRecord(
                        deterministic_id=str(chunk.id),
                        canonical_payload_hash=payload_hash,
                        projection_type="chunk",
                    )
                )

            # Resolve fact records for graph projections
            fact_query = select(GraphFact).where(
                GraphFact.tombstone_generation == 0,
            )

            if version_id is not None:
                fact_query = fact_query.where(
                    GraphFact.source_version_id == version_id,
                )

            fact_rows = (await session.execute(fact_query)).scalars().all()

            for fact in fact_rows:
                payload_hash = _fact_payload_hash(fact)
                records.append(
                    SnapshotRecord(
                        deterministic_id=str(fact.id),
                        canonical_payload_hash=payload_hash,
                        projection_type="fact",
                    )
                )

        generation = self._generation if self._generation is not None else 1

        return ProjectionSnapshot(
            snapshot_id=snapshot_id,
            run_id=f"snapshot-{snapshot_id[:12]}",
            version_id=str(version_id) if version_id is not None else "",
            generation=generation,
            tombstone_generation=0,
            records=tuple(records),
        )

    @staticmethod
    def _parse_snapshot_id(snapshot_id: str) -> tuple[uuid.UUID | None, bool]:
        """Extract version scope and rebuild flag from a snapshot ID.

        Returns ``(version_uuid_or_none, is_rebuild)``.
        """
        # Direct version UUID (ingestion path)
        try:
            return uuid.UUID(snapshot_id), False
        except ValueError:
            pass

        # Rebuild format: rebuild-{backend}-{scope}
        if snapshot_id.startswith("rebuild-"):
            parts = snapshot_id.split("-", 2)
            if len(parts) >= 3:
                scope = parts[2]
                if scope == "full":
                    return None, True
                try:
                    return uuid.UUID(scope), True
                except ValueError:
                    pass
            return None, True

        # Fallback: treat as opaque ID, full-corpus scope
        return None, False


def _chunk_payload_hash(chunk: object) -> str:
    """Compute a deterministic hash covering all projection-defining chunk fields."""
    data = {
        "id": str(getattr(chunk, "id", "")),
        "content_sha256": getattr(chunk, "content_sha256", ""),
        "version_id": str(getattr(chunk, "version_id", "")),
        "document_id": str(getattr(chunk, "document_id", "")),
        "lifecycle": getattr(chunk, "lifecycle", ""),
        "tombstone_generation": getattr(chunk, "tombstone_generation", 0),
        "profile_revision": str(getattr(chunk, "profile_revision", "")),
    }
    # Include label_ids if available (may be a relationship)
    label_ids = getattr(chunk, "label_ids", None)
    if label_ids is not None:
        if callable(label_ids):
            label_ids = None
        else:
            data["label_ids"] = sorted(str(lid) for lid in label_ids) if label_ids else []
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


def _fact_payload_hash(fact: object) -> str:
    """Compute a deterministic hash covering all projection-defining fact fields."""
    data = {
        "id": str(getattr(fact, "id", "")),
        "predicate_key": getattr(fact, "predicate_key", ""),
        "subject_entity_id": str(getattr(fact, "subject_entity_id", "")),
        "object_entity_id": str(getattr(fact, "object_entity_id", "")),
        "object_literal": getattr(fact, "object_literal", None),
        "object_normalized_key": getattr(fact, "object_normalized_key", ""),
        "source_chunk_id": str(getattr(fact, "source_chunk_id", "")),
        "source_version_id": str(getattr(fact, "source_version_id", "")),
        "confidence": str(getattr(fact, "confidence", 0)),
        "corroboration_count": getattr(fact, "corroboration_count", 1),
        "extraction_route_revision_id": str(getattr(fact, "extraction_route_revision_id", "")),
        "tombstone_generation": getattr(fact, "tombstone_generation", 0),
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()
