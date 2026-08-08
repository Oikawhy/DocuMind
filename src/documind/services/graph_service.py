"""Neo4j batch writer and rebuilder per §6.2.

The writer satisfies ``ProjectionWriter`` from :mod:`projection_service`
and materializes knowledge-graph facts as labeled nodes and relationships.

Write order per spec: source → entity → fact → relationships.
Batch limit: 500 facts or 5 MiB per transaction.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from documind.services.projection_service import (
    ProjectionBackend,
    ProjectionManifest,
    ProjectionSnapshot,
    ProjectionTransientError,
    manifest_checksum,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_FACTS_PER_TX = 500
_MAX_BYTES_PER_TX = 5 * 1024 * 1024  # 5 MiB

# Neo4j constraints and indexes per §6.2
_CONSTRAINTS = [
    "CREATE CONSTRAINT entity_identity IF NOT EXISTS"
    " FOR (e:Entity) REQUIRE (e.entity_type, e.normalized_key) IS UNIQUE",
    "CREATE CONSTRAINT chunk_identity IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
    "CREATE CONSTRAINT version_identity IF NOT EXISTS FOR (v:DocumentVersion) REQUIRE v.version_id IS UNIQUE",
    "CREATE CONSTRAINT fact_identity IF NOT EXISTS FOR (f:Fact) REQUIRE (f.fact_id, f.generation) IS UNIQUE",
    "CREATE CONSTRAINT document_identity IF NOT EXISTS FOR (d:Document) REQUIRE d.document_id IS UNIQUE",
    "CREATE CONSTRAINT label_identity IF NOT EXISTS FOR (l:Label) REQUIRE l.label_id IS UNIQUE",
]

_INDEXES = [
    "CREATE INDEX fact_source_version IF NOT EXISTS FOR (f:Fact) ON (f.generation, f.source_version_id)",
    "CREATE INDEX fact_source_chunk IF NOT EXISTS FOR (f:Fact) ON (f.generation, f.source_chunk_id)",
]

# ---------------------------------------------------------------------------
# Resolved fact payload for Neo4j materialization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphFactPayload:
    """Full resolved graph fact for Neo4j projection."""

    fact_id: str
    subject_entity_type: str
    subject_normalized_key: str
    subject_display_value: str
    predicate_key: str
    # Entity-object branch
    object_entity_type: str | None = None
    object_normalized_key: str | None = None
    object_display_value: str | None = None
    # Literal-object branch
    object_literal: dict[str, Any] | None = None
    # Source provenance
    source_chunk_id: str = ""
    source_version_id: str = ""
    source_document_id: str = ""
    confidence: float = 0.0
    corroboration_count: int = 1
    extraction_revision: str = ""
    tombstone_generation: int = 0
    generation: int = 0
    # Source chunk metadata for Chunk nodes
    chunk_page_start: int | None = None
    chunk_page_end: int | None = None
    chunk_section_path: list[str] | None = None
    chunk_content_hash: str = ""
    chunk_lifecycle: str = ""
    # Label IDs for Document label materialization
    label_ids: list[str] | None = None


class GraphFactPayloadResolver(Protocol):
    """Resolve full graph-fact payloads from a projection snapshot."""

    async def resolve_graph_payloads(self, snapshot: ProjectionSnapshot) -> list[GraphFactPayload]: ...


# ---------------------------------------------------------------------------
# Neo4j driver protocol
# ---------------------------------------------------------------------------


class Neo4jAsyncDriver(Protocol):
    """Subset of neo4j async driver API used by the writer."""

    def session(self, **kwargs: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Neo4j projection writer
# ---------------------------------------------------------------------------


class Neo4jProjectionWriter:
    """Batch Neo4j writer satisfying ``ProjectionWriter``.

    Labels: Entity, Fact, Chunk, DocumentVersion, Document.
    Entity subtype is a property, not a dynamic label.
    Fact is a reified node with ``predicate_key`` property.
    Relationships: ``(:Entity)-[:SUBJECT_OF]->(:Fact)``,
    ``(:Fact)-[:OBJECT_ENTITY]->(:Entity)``,
    ``(:Fact)-[:SUPPORTED_BY]->(:Chunk)``.
    """

    def __init__(
        self,
        *,
        driver: Neo4jAsyncDriver,
        database: str = "neo4j",
        payload_resolver: GraphFactPayloadResolver,
    ) -> None:
        self._driver = driver
        self._database = database
        self._resolver = payload_resolver

    async def ensure_constraints(self) -> None:
        """Create the 6 constraints and 2 indexes from §6.2."""
        async with self._driver.session(database=self._database) as session:
            for statement in _CONSTRAINTS + _INDEXES:
                await session.run(statement)
        logger.info(
            "Neo4j constraints and indexes ensured (%d constraints, %d indexes)",
            len(_CONSTRAINTS),
            len(_INDEXES),
        )

    async def project(self, snapshot: ProjectionSnapshot) -> ProjectionManifest:
        """Materialize graph facts as Neo4j nodes and relationships."""
        payloads = await self._resolver.resolve_graph_payloads(snapshot)

        # Batch payloads
        batches = self._split_batches(payloads)

        for batch_idx, batch in enumerate(batches):
            try:
                async with self._driver.session(database=self._database) as session:
                    await session.execute_write(self._write_batch, batch, snapshot.generation)
            except (ConnectionError, OSError, TimeoutError) as exc:
                raise ProjectionTransientError(f"Neo4j batch {batch_idx} failed: {exc}") from exc

        return ProjectionManifest(
            backend=ProjectionBackend.NEO4J,
            snapshot_id=snapshot.snapshot_id,
            generation=snapshot.generation,
            tombstone_generation=snapshot.tombstone_generation,
            record_count=len(snapshot.records),
            checksum=manifest_checksum(snapshot.records),
        )

    @staticmethod
    def _split_batches(payloads: list[GraphFactPayload]) -> list[list[GraphFactPayload]]:
        """Split payloads into batches of max 500 facts or 5 MiB."""
        if not payloads:
            return []

        batches: list[list[GraphFactPayload]] = []
        current_batch: list[GraphFactPayload] = []
        current_bytes = 0

        for payload in payloads:
            payload_size = sys.getsizeof(payload)
            if (
                len(current_batch) >= _MAX_FACTS_PER_TX or (current_bytes + payload_size) > _MAX_BYTES_PER_TX
            ) and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_bytes = 0
            current_batch.append(payload)
            current_bytes += payload_size

        if current_batch:
            batches.append(current_batch)

        return batches

    @staticmethod
    async def _write_batch(tx: Any, batch: list[GraphFactPayload], generation: int) -> None:
        """Write one batch in spec order: source → entity → fact → relationships.

        Uses MERGE for idempotent upserts on deterministic IDs.
        """
        # Collect unique source chunks, entities, and version/document references
        seen_chunks: set[str] = set()
        seen_entities: set[str] = set()  # (type, normalized_key)
        seen_versions: set[str] = set()
        seen_documents: set[str] = set()
        seen_labels: set[str] = set()

        # --- 1. Source nodes (Chunk, DocumentVersion) ---
        for payload in batch:
            if payload.source_chunk_id and payload.source_chunk_id not in seen_chunks:
                seen_chunks.add(payload.source_chunk_id)
                await tx.run(
                    """
                    MERGE (c:Chunk {chunk_id: $chunk_id})
                    SET c.page_start = $page_start,
                        c.page_end = $page_end,
                        c.section_path = $section_path,
                        c.content_hash = $content_hash,
                        c.lifecycle = $lifecycle
                    """,
                    chunk_id=payload.source_chunk_id,
                    page_start=payload.chunk_page_start,
                    page_end=payload.chunk_page_end,
                    section_path=payload.chunk_section_path or [],
                    content_hash=payload.chunk_content_hash,
                    lifecycle=payload.chunk_lifecycle,
                )

            if payload.source_version_id and payload.source_version_id not in seen_versions:
                seen_versions.add(payload.source_version_id)
                await tx.run(
                    """
                    MERGE (v:DocumentVersion {version_id: $version_id})
                    SET v.document_id = $document_id
                    """,
                    version_id=payload.source_version_id,
                    document_id=payload.source_document_id,
                )

            # Document nodes and VERSION_OF relationships
            if payload.source_document_id and payload.source_document_id not in seen_documents:
                seen_documents.add(payload.source_document_id)
                await tx.run(
                    "MERGE (d:Document {document_id: $document_id})",
                    document_id=payload.source_document_id,
                )

            if payload.source_version_id and payload.source_document_id:
                await tx.run(
                    """
                    MATCH (v:DocumentVersion {version_id: $version_id})
                    MATCH (d:Document {document_id: $document_id})
                    MERGE (v)-[:VERSION_OF]->(d)
                    """,
                    version_id=payload.source_version_id,
                    document_id=payload.source_document_id,
                )

            # Label nodes and HAS_LABEL relationships
            for label_id in payload.label_ids or []:
                if label_id not in seen_labels:
                    seen_labels.add(label_id)
                    await tx.run(
                        "MERGE (l:Label {label_id: $label_id})",
                        label_id=label_id,
                    )
                if payload.source_document_id:
                    await tx.run(
                        """
                        MATCH (d:Document {document_id: $document_id})
                        MATCH (l:Label {label_id: $label_id})
                        MERGE (d)-[:HAS_LABEL]->(l)
                        """,
                        document_id=payload.source_document_id,
                        label_id=label_id,
                    )

        # --- 2. Entity nodes ---
        for payload in batch:
            subj_key = f"{payload.subject_entity_type}:{payload.subject_normalized_key}"
            if subj_key not in seen_entities:
                seen_entities.add(subj_key)
                await tx.run(
                    """
                    MERGE (e:Entity {entity_type: $entity_type, normalized_key: $normalized_key})
                    SET e.display_value = $display_value
                    """,
                    entity_type=payload.subject_entity_type,
                    normalized_key=payload.subject_normalized_key,
                    display_value=payload.subject_display_value,
                )

            if payload.object_entity_type and payload.object_normalized_key:
                obj_key = f"{payload.object_entity_type}:{payload.object_normalized_key}"
                if obj_key not in seen_entities:
                    seen_entities.add(obj_key)
                    await tx.run(
                        """
                        MERGE (e:Entity {entity_type: $entity_type, normalized_key: $normalized_key})
                        SET e.display_value = $display_value
                        """,
                        entity_type=payload.object_entity_type,
                        normalized_key=payload.object_normalized_key,
                        display_value=payload.object_display_value or "",
                    )

        # --- 3. Fact nodes (reified with predicate_key property) ---
        for payload in batch:
            object_literal_json = json.dumps(payload.object_literal, sort_keys=True) if payload.object_literal else None
            await tx.run(
                """
                MERGE (f:Fact {fact_id: $fact_id, generation: $generation})
                SET f.predicate_key = $predicate_key,
                    f.source_chunk_id = $source_chunk_id,
                    f.source_version_id = $source_version_id,
                    f.source_document_id = $source_document_id,
                    f.confidence = $confidence,
                    f.corroboration_count = $corroboration_count,
                    f.extraction_revision = $extraction_revision,
                    f.tombstone_generation = $tombstone_generation,
                    f.object_literal = $object_literal,
                    f.object_normalized_key = $object_normalized_key
                """,
                fact_id=payload.fact_id,
                generation=generation,
                predicate_key=payload.predicate_key,
                source_chunk_id=payload.source_chunk_id,
                source_version_id=payload.source_version_id,
                source_document_id=payload.source_document_id,
                confidence=payload.confidence,
                corroboration_count=payload.corroboration_count,
                extraction_revision=payload.extraction_revision,
                tombstone_generation=payload.tombstone_generation,
                object_literal=object_literal_json,
                object_normalized_key=(payload.object_normalized_key if payload.object_entity_type else None),
            )

        # --- 4. Relationships ---
        for payload in batch:
            # (:Entity)-[:SUBJECT_OF]->(:Fact)
            await tx.run(
                """
                MATCH (e:Entity {entity_type: $entity_type, normalized_key: $normalized_key})
                MATCH (f:Fact {fact_id: $fact_id, generation: $generation})
                MERGE (e)-[:SUBJECT_OF]->(f)
                """,
                entity_type=payload.subject_entity_type,
                normalized_key=payload.subject_normalized_key,
                fact_id=payload.fact_id,
                generation=generation,
            )

            # (:Fact)-[:OBJECT_ENTITY]->(:Entity) for entity objects
            if payload.object_entity_type and payload.object_normalized_key:
                await tx.run(
                    """
                    MATCH (f:Fact {fact_id: $fact_id, generation: $generation})
                    MATCH (e:Entity {entity_type: $entity_type, normalized_key: $normalized_key})
                    MERGE (f)-[:OBJECT_ENTITY]->(e)
                    """,
                    fact_id=payload.fact_id,
                    generation=generation,
                    entity_type=payload.object_entity_type,
                    normalized_key=payload.object_normalized_key,
                )

            # (:Fact)-[:SUPPORTED_BY]->(:Chunk)
            if payload.source_chunk_id:
                await tx.run(
                    """
                    MATCH (f:Fact {fact_id: $fact_id, generation: $generation})
                    MATCH (c:Chunk {chunk_id: $chunk_id})
                    MERGE (f)-[:SUPPORTED_BY]->(c)
                    """,
                    fact_id=payload.fact_id,
                    generation=generation,
                    chunk_id=payload.source_chunk_id,
                )


# ---------------------------------------------------------------------------
# Neo4j graph rebuilder
# ---------------------------------------------------------------------------


class Neo4jGraphRebuilder:
    """Full Neo4j rebuild from canonical PostgreSQL facts per §6.2.

    Selects canonical non-tombstoned graph facts from PostgreSQL in
    immutable version order, materializes a new Fact-node generation,
    compares counts, and atomically switches the active generation.
    """

    def __init__(
        self,
        *,
        driver: Neo4jAsyncDriver,
        payload_resolver: GraphFactPayloadResolver,
        database: str = "neo4j",
    ) -> None:
        self._driver = driver
        self._resolver = payload_resolver
        self._database = database

    async def rebuild(self, *, snapshot: ProjectionSnapshot, new_generation: int) -> int:
        """Replay canonical facts and materialize a new generation.

        Returns the number of facts materialized.
        """
        payloads = await self._resolver.resolve_graph_payloads(snapshot)

        # Update generation on all payloads
        writer = Neo4jProjectionWriter(
            driver=self._driver,
            database=self._database,
            payload_resolver=self._resolver,
        )

        # Write with the new generation
        batches = writer._split_batches(payloads)
        total = 0

        for batch in batches:
            async with self._driver.session(database=self._database) as session:
                await session.execute_write(writer._write_batch, batch, new_generation)
                total += len(batch)

        logger.info(
            "Neo4j rebuild complete: generation=%d facts=%d",
            new_generation,
            total,
        )
        return total

    async def verify_generation(self, *, generation: int, expected_count: int) -> bool:
        """Verify that the rebuilt generation has the expected fact count."""
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (f:Fact {generation: $generation}) RETURN count(f) AS cnt",
                generation=generation,
            )
            record = await result.single()
            actual_count = record["cnt"] if record else 0

        if actual_count != expected_count:
            logger.error(
                "Neo4j generation %d count mismatch: expected=%d actual=%d",
                generation,
                expected_count,
                actual_count,
            )
            return False

        logger.info("Neo4j generation %d verified: count=%d", generation, actual_count)
        return True

    async def delete_erased_facts(self, *, chunk_id: str, generation: int) -> bool:
        """Delete facts and relationships for an erased chunk in a generation.

        Returns True only after verifying zero Fact nodes remain for that chunk.
        """
        async with self._driver.session(database=self._database) as session:
            # Delete relationships first, then fact nodes
            await session.run(
                """
                MATCH (f:Fact {generation: $generation, source_chunk_id: $chunk_id})
                DETACH DELETE f
                """,
                generation=generation,
                chunk_id=chunk_id,
            )

            # Verify deletion
            result = await session.run(
                """
                MATCH (f:Fact {generation: $generation, source_chunk_id: $chunk_id})
                RETURN count(f) AS remaining
                """,
                generation=generation,
                chunk_id=chunk_id,
            )
            record = await result.single()
            remaining = record["remaining"] if record else 0

        return remaining == 0
