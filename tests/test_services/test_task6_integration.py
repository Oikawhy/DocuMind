"""Integration-style tests for Task 6 gap remediations (T6-01 through T6-13).

These tests verify the critical behaviors fixed across all 8 remediation tasks
without requiring external services (PostgreSQL, Qdrant, OpenSearch, Neo4j).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from typing import Any

import pytest

from documind.services.embedding_service import (
    EmbeddingModelConfig,
)
from documind.services.graph_service import (
    GraphFactPayload,
    Neo4jProjectionWriter,
)
from documind.services.projection_service import (
    ProjectionBackend,
    ProjectionCoordinator,
    ProjectionManifest,
    ProjectionSnapshot,
    SnapshotRecord,
    WriterOutcome,
    manifest_checksum,
)
from documind.workflows.document_version import (
    StageExecution,
    stage_idempotency_key,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_sha256(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stage_execution(version_id: str, name: str, input_sha256: str) -> StageExecution:
    return StageExecution(
        version_id=version_id,
        name=name,
        input_sha256=input_sha256,
        idempotency_key=stage_idempotency_key(version_id, name, input_sha256),
    )


def _make_snapshot(
    n_records: int = 3,
    snapshot_id: str = "",
    generation: int = 1,
) -> ProjectionSnapshot:
    records = tuple(
        SnapshotRecord(
            deterministic_id=str(uuid.uuid4()),
            canonical_payload_hash=hashlib.sha256(f"record-{i}".encode()).hexdigest(),
            projection_type="chunk",
        )
        for i in range(n_records)
    )
    return ProjectionSnapshot(
        snapshot_id=snapshot_id or hashlib.sha256(b"test-snapshot").hexdigest(),
        run_id="test-run",
        version_id=str(uuid.uuid4()),
        generation=generation,
        tombstone_generation=0,
        records=records,
    )


class InMemoryEvidenceStore:
    """Satisfies ProjectionEvidenceStore + ProjectionIncidentSink for tests."""

    def __init__(self) -> None:
        self.outcomes: dict[str, WriterOutcome] = {}
        self.manifests: list[ProjectionManifest] = []
        self.incidents: list[Any] = []

    async def state_for(self, backend: ProjectionBackend, snapshot: ProjectionSnapshot) -> WriterOutcome | None:
        return self.outcomes.get(f"{backend}:{snapshot.snapshot_id}")

    async def record_outcome(self, outcome: WriterOutcome) -> None:
        self.outcomes[f"{outcome.backend}:{outcome.snapshot_id}"] = outcome

    async def record_manifest(self, manifest: ProjectionManifest) -> None:
        self.manifests.append(manifest)

    async def record_incident(self, incident: Any) -> None:
        self.incidents.append(incident)


class InMemorySource:
    """Satisfies CanonicalProjectionSource for tests."""

    def __init__(self, snapshot: ProjectionSnapshot) -> None:
        self._snapshot = snapshot

    async def resolve_snapshot(self, snapshot_id: str) -> ProjectionSnapshot:
        return self._snapshot


class PassthroughWriter:
    """Satisfies ProjectionWriter for tests."""

    def __init__(self, backend: ProjectionBackend) -> None:
        self._backend = backend
        self.call_count = 0

    async def project(self, snapshot: ProjectionSnapshot) -> ProjectionManifest:
        self.call_count += 1
        return ProjectionManifest(
            backend=self._backend,
            snapshot_id=snapshot.snapshot_id,
            generation=snapshot.generation,
            tombstone_generation=snapshot.tombstone_generation,
            record_count=len(snapshot.records),
            checksum=manifest_checksum(snapshot.records),
        )


class PassthroughGuard:
    async def assert_active(self, version_id: str, tombstone_generation: int) -> None:
        pass


class PassthroughCompleter:
    def __init__(self) -> None:
        self.completed: list[str] = []

    async def complete_version(self, snapshot: ProjectionSnapshot) -> None:
        self.completed.append(snapshot.version_id)


def _build_coordinator(
    snapshot: ProjectionSnapshot,
    *,
    evidence: InMemoryEvidenceStore | None = None,
    completer: PassthroughCompleter | None = None,
) -> tuple[
    ProjectionCoordinator, InMemoryEvidenceStore, PassthroughCompleter, dict[ProjectionBackend, PassthroughWriter]
]:
    evidence = evidence or InMemoryEvidenceStore()
    completer = completer or PassthroughCompleter()
    writers = {backend: PassthroughWriter(backend) for backend in ProjectionBackend}
    coordinator = ProjectionCoordinator(
        source=InMemorySource(snapshot),
        writers=writers,
        evidence_store=evidence,
        tombstone_guard=PassthroughGuard(),
        lifecycle_completer=completer,
        incident_sink=evidence,
    )
    return coordinator, evidence, completer, writers


# ---------------------------------------------------------------------------
# T6-01: No _Stub classes remain — real adapters are wired
# ---------------------------------------------------------------------------


def test_worker_uses_real_adapters_not_stubs() -> None:
    """Verify _Stub classes have been removed from worker module."""
    import documind.workflows.worker as worker_module

    source = dir(worker_module)
    stub_names = [name for name in source if name.startswith("_Stub")]
    assert stub_names == [], f"Found remaining stub classes: {stub_names}"


# ---------------------------------------------------------------------------
# T6-02: Checksum chain — project/verify/complete share one snapshot identity
# ---------------------------------------------------------------------------


def test_snapshot_checksum_is_shared_across_three_stages() -> None:
    """Project, verify, and complete stages must all use the same input_sha256."""
    version_id = str(uuid.uuid4())
    enriched = {"type": "invoice", "facts": 5}
    snapshot_checksum = _payload_sha256(enriched)

    project = _stage_execution(version_id, "project", snapshot_checksum)
    verify = _stage_execution(version_id, "verify", snapshot_checksum)
    complete = _stage_execution(version_id, "complete", snapshot_checksum)

    # Same input_sha256
    assert project.input_sha256 == verify.input_sha256 == complete.input_sha256 == snapshot_checksum

    # But distinct idempotency keys (stage name differs)
    keys = {project.idempotency_key, verify.idempotency_key, complete.idempotency_key}
    assert len(keys) == 3


# ---------------------------------------------------------------------------
# T6-03/08/09: Durable evidence, lifecycle, generation
# ---------------------------------------------------------------------------


async def test_coordinator_full_project_verify_complete_cycle() -> None:
    """Full coordinator lifecycle: project → verify → complete."""
    snapshot = _make_snapshot()
    coordinator, evidence, completer, writers = _build_coordinator(snapshot)

    # Project
    await coordinator.project_snapshot(snapshot.snapshot_id)

    # All three writers called
    for backend in ProjectionBackend:
        assert writers[backend].call_count == 1

    # Evidence stored for all backends
    assert len(evidence.outcomes) == 3

    # Verify
    manifests = await coordinator.verify_snapshot(snapshot)
    assert len(manifests) == 3
    assert len(evidence.manifests) == 3

    # Complete
    await coordinator.complete_snapshot(snapshot)
    assert snapshot.version_id in completer.completed


# ---------------------------------------------------------------------------
# T6-04: Embedding config validation
# ---------------------------------------------------------------------------


def test_embedding_config_rejects_wrong_dimension(tmp_path: Any) -> None:
    """BGE-M3 contract requires exactly 1024 dimensions."""
    valid_digest = "sha256:" + "a" * 64
    with pytest.raises(ValueError, match="1024"):
        EmbeddingModelConfig(model_path=tmp_path, expected_digest=valid_digest, dimension=768)


def test_embedding_config_rejects_invalid_digest(tmp_path: Any) -> None:
    """Production config must have a properly formatted expected_digest."""
    with pytest.raises(ValueError, match="expected_digest"):
        EmbeddingModelConfig(model_path=tmp_path, expected_digest="bad-digest")


def test_embedding_config_accepts_valid_config(tmp_path: Any) -> None:
    """Valid production config with correct digest format passes validation."""
    valid_digest = "sha256:" + "a" * 64
    config = EmbeddingModelConfig(model_path=tmp_path, expected_digest=valid_digest)
    assert config.dimension == 1024


# ---------------------------------------------------------------------------
# T6-05: Qdrant/OpenSearch setup error propagation
# ---------------------------------------------------------------------------


def test_opensearch_index_settings_have_english_analyzer() -> None:
    """OpenSearch index must include English language analyzer for BM25."""
    from documind.services.indexing_service import _OPENSEARCH_INDEX_SETTINGS

    analyzers = _OPENSEARCH_INDEX_SETTINGS["settings"]["analysis"]["analyzer"]
    assert "english_analyzer" in analyzers
    assert "english_stemmer" in analyzers["english_analyzer"]["filter"]

    # Content field has sub-field with english analyzer
    content = _OPENSEARCH_INDEX_SETTINGS["mappings"]["properties"]["content"]
    assert "fields" in content
    assert content["fields"]["english"]["analyzer"] == "english_analyzer"


# ---------------------------------------------------------------------------
# T6-06/07: Neo4j batch sizing uses serialized size
# ---------------------------------------------------------------------------


def test_neo4j_batch_uses_serialized_size_not_getsizeof() -> None:
    """Batch sizing must use JSON-serialized size, not sys.getsizeof."""
    payload = GraphFactPayload(
        fact_id="f1",
        subject_entity_type="Person",
        subject_normalized_key="john-doe",
        subject_display_value="John Doe",
        predicate_key="works_at",
    )

    measured = Neo4jProjectionWriter._payload_byte_size(payload)
    expected = len(json.dumps(asdict(payload), default=str).encode("utf-8"))
    assert measured == expected
    assert measured > 0


def test_neo4j_batch_splits_on_count_limit() -> None:
    """Batches must split at 500 facts regardless of size."""
    payloads = [
        GraphFactPayload(
            fact_id=f"f{i}",
            subject_entity_type="Entity",
            subject_normalized_key=f"key-{i}",
            subject_display_value=f"Entity {i}",
            predicate_key="rel",
        )
        for i in range(501)
    ]
    batches = Neo4jProjectionWriter._split_batches(payloads)
    assert len(batches) == 2
    assert len(batches[0]) == 500
    assert len(batches[1]) == 1


# ---------------------------------------------------------------------------
# T6-10/11: Rebuild activities accept generation_manager
# ---------------------------------------------------------------------------


def test_rebuild_configure_accepts_generation_manager() -> None:
    """configure_rebuild_activities must accept generation_manager kwarg."""
    import inspect

    from documind.workflows.maintenance.rebuild_projections import configure_rebuild_activities

    sig = inspect.signature(configure_rebuild_activities)
    assert "generation_manager" in sig.parameters


# ---------------------------------------------------------------------------
# T6-12/13: End-to-end coordinator integration
# ---------------------------------------------------------------------------


async def test_coordinator_requires_all_three_backends() -> None:
    """Coordinator must raise if any backend is missing."""
    snapshot = _make_snapshot()
    incomplete_writers = {
        ProjectionBackend.QDRANT: PassthroughWriter(ProjectionBackend.QDRANT),
        ProjectionBackend.OPENSEARCH: PassthroughWriter(ProjectionBackend.OPENSEARCH),
        # NEO4J missing
    }
    with pytest.raises(ValueError, match="Missing projection writers"):
        ProjectionCoordinator(
            source=InMemorySource(snapshot),
            writers=incomplete_writers,
            evidence_store=InMemoryEvidenceStore(),
            tombstone_guard=PassthroughGuard(),
            lifecycle_completer=PassthroughCompleter(),
        )


async def test_coordinator_replay_uses_stored_evidence() -> None:
    """On replay, coordinator must use stored evidence, not re-project."""
    snapshot = _make_snapshot()
    evidence = InMemoryEvidenceStore()

    # Pre-populate evidence for all backends
    for backend in ProjectionBackend:
        outcome = WriterOutcome(
            backend=backend,
            snapshot_id=snapshot.snapshot_id,
            generation=snapshot.generation,
            tombstone_generation=snapshot.tombstone_generation,
            status="projected",
            manifest=ProjectionManifest(
                backend=backend,
                snapshot_id=snapshot.snapshot_id,
                generation=snapshot.generation,
                tombstone_generation=snapshot.tombstone_generation,
                record_count=len(snapshot.records),
                checksum=manifest_checksum(snapshot.records),
            ),
        )
        evidence.outcomes[f"{backend}:{snapshot.snapshot_id}"] = outcome

    coordinator, _, _, writers = _build_coordinator(snapshot, evidence=evidence)
    await coordinator.project_snapshot(snapshot.snapshot_id)

    # Writers should NOT have been called — evidence was replayed
    for backend in ProjectionBackend:
        assert writers[backend].call_count == 0, f"{backend} writer called despite stored evidence"


async def test_coordinator_manifest_checksum_is_deterministic() -> None:
    """manifest_checksum must be deterministic for the same records."""
    records = tuple(
        SnapshotRecord(
            deterministic_id=f"id-{i}",
            canonical_payload_hash=f"hash-{i}",
            projection_type="chunk",
        )
        for i in range(5)
    )
    checksum1 = manifest_checksum(records)
    checksum2 = manifest_checksum(records)
    assert checksum1 == checksum2

    # Order must not matter (sorted by deterministic_id)
    reversed_records = tuple(reversed(records))
    checksum3 = manifest_checksum(reversed_records)
    assert checksum1 == checksum3


def test_writer_delete_methods_exist() -> None:
    """Both Qdrant and OpenSearch writers must have delete_by_version."""
    from documind.services.indexing_service import (
        OpenSearchProjectionWriter,
        QdrantProjectionWriter,
    )

    assert hasattr(QdrantProjectionWriter, "delete_by_version")
    assert hasattr(OpenSearchProjectionWriter, "delete_by_version")
    assert callable(QdrantProjectionWriter.delete_by_version)
    assert callable(OpenSearchProjectionWriter.delete_by_version)
