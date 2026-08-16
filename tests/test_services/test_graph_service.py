"""Graph service (Neo4j writer + rebuilder) unit tests using protocol-level mocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from documind.services.graph_service import (
    _CONSTRAINTS,
    _INDEXES,
    _MAX_FACTS_PER_TX,
    GraphFactPayload,
    Neo4jGraphRebuilder,
    Neo4jProjectionWriter,
)
from documind.services.projection_service import (
    ProjectionBackend,
    ProjectionSnapshot,
    SnapshotRecord,
    manifest_checksum,
)

# ---------------------------------------------------------------------------
# Fake Neo4j driver / session / transaction
# ---------------------------------------------------------------------------


@dataclass
class FakeResult:
    _record: dict[str, Any] | None = None

    async def single(self) -> dict[str, Any] | None:
        return self._record


@dataclass
class FakeTransaction:
    """Records all Cypher statements for assertion."""

    statements: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def run(self, query: str, **params: Any) -> FakeResult:
        self.statements.append((query.strip(), params))
        return FakeResult()


@dataclass
class FakeSession:
    """Fake async Neo4j session."""

    transactions: list[FakeTransaction] = field(default_factory=list)
    run_statements: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _verify_count: int = 0

    async def run(self, query: str, **params: Any) -> FakeResult:
        self.run_statements.append((query.strip(), params))
        if "count(f) AS cnt" in query:
            return FakeResult({"cnt": self._verify_count})
        if "count(f) AS remaining" in query:
            return FakeResult({"remaining": self._verify_count})
        return FakeResult()

    async def execute_write(self, fn: Any, *args: Any) -> None:
        tx = FakeTransaction()
        self.transactions.append(tx)
        await fn(tx, *args)

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


@dataclass
class FakeDriver:
    """Fake async Neo4j driver."""

    sessions: list[FakeSession] = field(default_factory=list)
    verify_count: int = 0

    def session(self, **kwargs: Any) -> FakeSession:
        s = FakeSession(_verify_count=self.verify_count)
        self.sessions.append(s)
        return s


# ---------------------------------------------------------------------------
# Fake payload resolver
# ---------------------------------------------------------------------------


class FakeGraphPayloadResolver:
    def __init__(self, payloads: list[GraphFactPayload] | None = None) -> None:
        self._payloads = payloads or []

    async def resolve_graph_payloads(self, snapshot: ProjectionSnapshot) -> list[GraphFactPayload]:
        return self._payloads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(n_records: int = 1, generation: int = 1) -> ProjectionSnapshot:
    records = tuple(
        SnapshotRecord(
            deterministic_id=f"fact-{i}",
            canonical_payload_hash=f"{'a' * 63}{i}",
            projection_type="fact",
        )
        for i in range(n_records)
    )
    return ProjectionSnapshot(
        snapshot_id="snap-1",
        run_id="run-1",
        version_id="version-1",
        generation=generation,
        tombstone_generation=0,
        records=records,
    )


def _make_payload(
    fact_id: str = "fact-1",
    entity_object: bool = True,
    literal_object: bool = False,
) -> GraphFactPayload:
    return GraphFactPayload(
        fact_id=fact_id,
        subject_entity_type="Organization",
        subject_normalized_key="organization:acme corp",
        subject_display_value="Acme Corp",
        predicate_key="has_subsidiary",
        object_entity_type="Organization" if entity_object else None,
        object_normalized_key="organization:widgets inc" if entity_object else None,
        object_display_value="Widgets Inc" if entity_object else None,
        object_literal={"type": "currency", "unit": "USD", "value": "1000000"} if literal_object else None,
        source_chunk_id="chunk-1",
        source_version_id="version-1",
        source_document_id="doc-1",
        confidence=0.95,
        corroboration_count=1,
        extraction_revision="rev-1",
        tombstone_generation=0,
        generation=1,
        chunk_page_start=1,
        chunk_page_end=2,
        chunk_section_path=["Section 1"],
        chunk_content_hash="a" * 64,
        chunk_lifecycle="completed",
    )


# ---------------------------------------------------------------------------
# Tests: ensure_constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_constraints_creates_all_eight() -> None:
    """Verifies 6 constraints + 2 indexes are created."""
    driver = FakeDriver()
    resolver = FakeGraphPayloadResolver()
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )

    await writer.ensure_constraints()

    assert len(driver.sessions) == 1
    session = driver.sessions[0]
    all_statements = [stmt.strip() for stmt, _ in session.run_statements]

    for constraint in _CONSTRAINTS:
        assert constraint in all_statements, f"Missing constraint: {constraint}"
    for index in _INDEXES:
        assert index in all_statements, f"Missing index: {index}"


# ---------------------------------------------------------------------------
# Tests: batch splitting
# ---------------------------------------------------------------------------


def test_batch_respects_500_fact_limit() -> None:
    """More than 500 facts are split into multiple batches."""
    payloads = [_make_payload(fact_id=f"fact-{i}") for i in range(600)]
    batches = Neo4jProjectionWriter._split_batches(payloads)

    assert len(batches) >= 2
    assert len(batches[0]) == _MAX_FACTS_PER_TX
    assert sum(len(b) for b in batches) == 600


def test_batch_single_batch_under_limit() -> None:
    """Under 500 facts stays in one batch."""
    payloads = [_make_payload(fact_id=f"fact-{i}") for i in range(10)]
    batches = Neo4jProjectionWriter._split_batches(payloads)

    assert len(batches) == 1
    assert len(batches[0]) == 10


def test_batch_empty_payloads() -> None:
    """Empty payloads produce no batches."""
    batches = Neo4jProjectionWriter._split_batches([])
    assert batches == []


# ---------------------------------------------------------------------------
# Tests: write order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_order_source_entity_fact_relationships() -> None:
    """Write order matches spec: source → entity → fact → relationships."""
    driver = FakeDriver()
    payload = _make_payload()
    resolver = FakeGraphPayloadResolver([payload])
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    await writer.project(snapshot)

    assert len(driver.sessions) == 1
    tx = driver.sessions[0].transactions[0]
    queries = [stmt for stmt, _ in tx.statements]

    # Find positions of each phase
    chunk_merge = next(i for i, q in enumerate(queries) if "MERGE (c:Chunk" in q)
    version_merge = next(i for i, q in enumerate(queries) if "MERGE (v:DocumentVersion" in q)
    entity_merge = next(i for i, q in enumerate(queries) if "MERGE (e:Entity" in q)
    fact_merge = next(i for i, q in enumerate(queries) if "MERGE (f:Fact" in q)
    subject_of = next(i for i, q in enumerate(queries) if "ABOUT" in q)

    # Source before entities before facts before relationships
    assert chunk_merge < entity_merge, "Source (Chunk) must come before Entity"
    assert version_merge < entity_merge, "Source (Version) must come before Entity"
    assert entity_merge < fact_merge, "Entity must come before Fact"
    assert fact_merge < subject_of, "Fact must come before relationships"


# ---------------------------------------------------------------------------
# Tests: idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_idempotent_on_retry() -> None:
    """Same snapshot projected twice produces the same manifest."""
    driver = FakeDriver()
    payload = _make_payload()
    resolver = FakeGraphPayloadResolver([payload])
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    manifest1 = await writer.project(snapshot)
    manifest2 = await writer.project(snapshot)

    assert manifest1 == manifest2
    assert manifest1.checksum == manifest2.checksum


# ---------------------------------------------------------------------------
# Tests: entity subtype as property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_subtype_is_property_not_label() -> None:
    """Entity type is stored as entity_type property per §6.2, not a dynamic label."""
    driver = FakeDriver()
    payload = _make_payload()
    resolver = FakeGraphPayloadResolver([payload])
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    await writer.project(snapshot)

    tx = driver.sessions[0].transactions[0]
    entity_stmts = [(q, p) for q, p in tx.statements if "MERGE (e:Entity" in q]

    # Entity uses :Entity label (not :Organization or any dynamic label)
    for query, params in entity_stmts:
        assert ":Entity" in query
        assert "entity_type" in params
        # Ensure no dynamic label like :Organization in the MERGE pattern
        assert f":{params['entity_type']}" not in query.split("MERGE")[1].split(")")[0]


# ---------------------------------------------------------------------------
# Tests: reified fact nodes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fact_is_reified_node_with_predicate_key() -> None:
    """Fact is a reified :Fact node with predicate_key property per §6.2."""
    driver = FakeDriver()
    payload = _make_payload()
    resolver = FakeGraphPayloadResolver([payload])
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    await writer.project(snapshot)

    tx = driver.sessions[0].transactions[0]
    fact_stmts = [(q, p) for q, p in tx.statements if "MERGE (f:Fact" in q and "predicate_key" in q]
    assert len(fact_stmts) >= 1

    query, params = fact_stmts[0]
    assert params["predicate_key"] == "has_subsidiary"
    assert ":Fact" in query


# ---------------------------------------------------------------------------
# Tests: literal objects as fact properties
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_literal_object_remains_fact_property() -> None:
    """Literal objects stay as properties on the Fact node, not separate nodes."""
    driver = FakeDriver()
    payload = _make_payload(entity_object=False, literal_object=True)
    resolver = FakeGraphPayloadResolver([payload])
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    await writer.project(snapshot)

    tx = driver.sessions[0].transactions[0]
    fact_stmts = [(q, p) for q, p in tx.statements if "MERGE (f:Fact" in q]
    assert len(fact_stmts) >= 1

    _, params = fact_stmts[0]
    assert params["object_literal"] is not None
    assert "currency" in params["object_literal"]

    # No MENTIONS relationship should exist for literal objects
    obj_rel_stmts = [q for q, _ in tx.statements if "MENTIONS" in q]
    assert len(obj_rel_stmts) == 0


# ---------------------------------------------------------------------------
# Tests: SOURCED_FROM relationship
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_fact_has_sourced_from_chunk() -> None:
    """Every Fact connects through (:Fact)-[:SOURCED_FROM]->(:Chunk)."""
    driver = FakeDriver()
    payloads = [_make_payload(fact_id=f"fact-{i}") for i in range(3)]
    resolver = FakeGraphPayloadResolver(payloads)
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot(n_records=3)

    await writer.project(snapshot)

    tx = driver.sessions[0].transactions[0]
    sourced_from = [(q, p) for q, p in tx.statements if "SOURCED_FROM" in q]
    assert len(sourced_from) == 3  # One per fact


# ---------------------------------------------------------------------------
# Tests: rebuild
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_from_canonical_excludes_tombstoned() -> None:
    """Rebuilder uses the resolver which should exclude tombstoned facts.

    The resolver is responsible for filtering; the rebuilder materializes
    whatever the resolver provides.
    """
    driver = FakeDriver()
    # Only non-tombstoned payloads from resolver
    payloads = [_make_payload(fact_id="fact-alive")]
    resolver = FakeGraphPayloadResolver(payloads)
    rebuilder = Neo4jGraphRebuilder(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    count = await rebuilder.rebuild(snapshot=snapshot, new_generation=42)
    assert count == 1

    # Check that generation 42 was used in the write
    tx = driver.sessions[0].transactions[0]
    fact_stmts = [(q, p) for q, p in tx.statements if "MERGE (f:Fact" in q]
    assert fact_stmts[0][1]["generation"] == 42


# ---------------------------------------------------------------------------
# Tests: generation verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_generation_matching_count() -> None:
    """Verification passes when Neo4j count matches expected."""
    driver = FakeDriver(verify_count=10)
    resolver = FakeGraphPayloadResolver()
    rebuilder = Neo4jGraphRebuilder(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )

    result = await rebuilder.verify_generation(generation=1, expected_count=10)
    assert result is True


@pytest.mark.asyncio
async def test_verify_generation_mismatching_count() -> None:
    """Verification fails when Neo4j count doesn't match expected."""
    driver = FakeDriver(verify_count=5)
    resolver = FakeGraphPayloadResolver()
    rebuilder = Neo4jGraphRebuilder(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )

    result = await rebuilder.verify_generation(generation=1, expected_count=10)
    assert result is False


# ---------------------------------------------------------------------------
# Tests: erasure propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_erased_facts_returns_true_on_clean() -> None:
    """delete_erased_facts returns True when no facts remain."""
    driver = FakeDriver(verify_count=0)
    resolver = FakeGraphPayloadResolver()
    rebuilder = Neo4jGraphRebuilder(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )

    result = await rebuilder.delete_erased_facts(chunk_id="chunk-erased", generation=1)
    assert result is True


# ---------------------------------------------------------------------------
# Tests: manifest correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_returns_correct_manifest() -> None:
    """Manifest matches snapshot identity and deterministic checksum."""
    driver = FakeDriver()
    payload = _make_payload()
    resolver = FakeGraphPayloadResolver([payload])
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    manifest = await writer.project(snapshot)

    assert manifest.backend == ProjectionBackend.NEO4J
    assert manifest.snapshot_id == "snap-1"
    assert manifest.generation == 1
    assert manifest.tombstone_generation == 0
    assert manifest.record_count == 1
    assert manifest.checksum == manifest_checksum(snapshot.records)


# ---------------------------------------------------------------------------
# Tests: Document and Label materialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_node_is_materialized() -> None:
    """Document nodes are created with document_id per §6.2."""
    driver = FakeDriver()
    payload = _make_payload()
    resolver = FakeGraphPayloadResolver([payload])
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    await writer.project(snapshot)

    tx = driver.sessions[0].transactions[0]
    doc_stmts = [(q, p) for q, p in tx.statements if "MERGE (d:Document" in q]
    assert len(doc_stmts) >= 1
    assert doc_stmts[0][1]["document_id"] == "doc-1"


@pytest.mark.asyncio
async def test_version_of_relationship_is_created() -> None:
    """(:DocumentVersion)-[:VERSION_OF]->(:Document) is materialized."""
    driver = FakeDriver()
    payload = _make_payload()
    resolver = FakeGraphPayloadResolver([payload])
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    await writer.project(snapshot)

    tx = driver.sessions[0].transactions[0]
    version_of_stmts = [q for q, _ in tx.statements if "VERSION_OF" in q]
    assert len(version_of_stmts) >= 1


@pytest.mark.asyncio
async def test_label_nodes_and_has_label_relationships() -> None:
    """Label nodes and (:Document)-[:HAS_LABEL]->(:Label) are materialized."""
    driver = FakeDriver()
    payload = _make_payload()
    # Add label_ids via replacement
    import dataclasses

    payload_with_labels = dataclasses.replace(payload, label_ids=["label-a", "label-b"])
    resolver = FakeGraphPayloadResolver([payload_with_labels])
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    await writer.project(snapshot)

    tx = driver.sessions[0].transactions[0]
    label_merge_stmts = [(q, p) for q, p in tx.statements if "MERGE (l:Label" in q]
    assert len(label_merge_stmts) == 2  # Two unique labels

    has_label_stmts = [q for q, _ in tx.statements if "HAS_LABEL" in q]
    assert len(has_label_stmts) == 2  # One per label


@pytest.mark.asyncio
async def test_no_labels_when_label_ids_is_none() -> None:
    """When label_ids is None, no Label nodes or HAS_LABEL relationships are created."""
    driver = FakeDriver()
    payload = _make_payload()  # label_ids defaults to None
    resolver = FakeGraphPayloadResolver([payload])
    writer = Neo4jProjectionWriter(
        driver=driver,  # type: ignore[arg-type]
        payload_resolver=resolver,
    )
    snapshot = _make_snapshot()

    await writer.project(snapshot)

    tx = driver.sessions[0].transactions[0]
    label_stmts = [q for q, _ in tx.statements if "Label" in q]
    assert len(label_stmts) == 0
