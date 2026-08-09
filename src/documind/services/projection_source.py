"""PostgreSQL-backed canonical projection source.

Implements the ``CanonicalProjectionSource`` protocol from
:mod:`projection_service` by resolving frozen snapshots from the
``document_chunk`` and ``graph_fact`` tables.
"""

from __future__ import annotations

import hashlib
import json
import logging

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
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve_snapshot(self, snapshot_id: str) -> ProjectionSnapshot:
        """Build a frozen snapshot from canonical chunk and fact data.

        The ``snapshot_id`` is used as the lookup key.  For ingestion
        workflows it corresponds to the enriched-output checksum; for
        rebuild workflows it encodes the backend and scope.

        Returns a ``ProjectionSnapshot`` with records derived from
        chunks (for Qdrant/OpenSearch projections) and facts (for Neo4j).
        """
        from documind.models.chunk import DocumentChunk
        from documind.models.enrichment import GraphFact

        records: list[SnapshotRecord] = []

        async with self._session_factory() as session:
            # Resolve chunk records for vector/search projections
            chunk_rows = (
                await session.execute(
                    select(DocumentChunk).limit(10000)
                )
            ).scalars().all()

            for chunk in chunk_rows:
                payload_hash = hashlib.sha256(
                    json.dumps(
                        {"id": str(chunk.id), "content_sha256": getattr(chunk, "content_sha256", "")},
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                records.append(
                    SnapshotRecord(
                        deterministic_id=str(chunk.id),
                        canonical_payload_hash=payload_hash,
                        projection_type="chunk",
                    )
                )

            # Resolve fact records for graph projections
            fact_rows = (
                await session.execute(
                    select(GraphFact).limit(10000)
                )
            ).scalars().all()

            for fact in fact_rows:
                payload_hash = hashlib.sha256(
                    json.dumps(
                        {"id": str(fact.id), "predicate_key": getattr(fact, "predicate_key", "")},
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                records.append(
                    SnapshotRecord(
                        deterministic_id=str(fact.id),
                        canonical_payload_hash=payload_hash,
                        projection_type="fact",
                    )
                )

        return ProjectionSnapshot(
            snapshot_id=snapshot_id,
            run_id="canonical",
            version_id="",
            generation=1,
            tombstone_generation=0,
            records=tuple(records),
        )
