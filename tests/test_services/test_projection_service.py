"""Negative-path contracts for projection coordination."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from documind.services.projection_service import (
    ProjectionBackend,
    ProjectionCoordinator,
    ProjectionIncident,
    ProjectionIntegrityError,
    ProjectionManifest,
    ProjectionSnapshot,
    ProjectionTransientError,
    SnapshotRecord,
    WriterOutcome,
    manifest_checksum,
)


@dataclass
class Source:
    snapshot_value: ProjectionSnapshot

    async def resolve_snapshot(self, snapshot_id: str) -> ProjectionSnapshot:
        assert snapshot_id == self.snapshot_value.snapshot_id
        return self.snapshot_value


@dataclass
class Writer:
    backend: ProjectionBackend
    transient_failures: int = 0
    mismatched_manifest: bool = False
    calls: int = 0

    async def project(self, snapshot: ProjectionSnapshot) -> ProjectionManifest:
        self.calls += 1
        if self.calls <= self.transient_failures:
            raise ProjectionTransientError(f"{self.backend.value} unavailable")
        # T6-08: Backend-specific record counting
        if self.backend == ProjectionBackend.NEO4J:
            relevant = tuple(r for r in snapshot.records if r.projection_type == "fact")
        else:
            relevant = tuple(r for r in snapshot.records if r.projection_type == "chunk")
        checksum = manifest_checksum(relevant)
        return ProjectionManifest(
            backend=self.backend,
            snapshot_id=snapshot.snapshot_id,
            generation=snapshot.generation,
            tombstone_generation=snapshot.tombstone_generation,
            record_count=len(relevant),
            checksum="different" if self.mismatched_manifest else checksum,
        )


@dataclass
class Evidence:
    states: dict[ProjectionBackend, WriterOutcome] = field(default_factory=dict)
    outcomes: list[WriterOutcome] = field(default_factory=list)
    manifests: list[ProjectionManifest] = field(default_factory=list)

    async def state_for(self, backend: ProjectionBackend, snapshot: ProjectionSnapshot) -> WriterOutcome | None:
        return self.states.get(backend)

    async def record_outcome(self, outcome: WriterOutcome, *, snapshot: ProjectionSnapshot | None = None) -> None:
        self.outcomes.append(outcome)
        self.states[outcome.backend] = outcome

    async def record_manifest(self, manifest: ProjectionManifest, *, snapshot: ProjectionSnapshot | None = None) -> None:
        self.manifests.append(manifest)


@dataclass
class Guard:
    async def assert_active(self, version_id: str, tombstone_generation: int) -> None:
        return None


@dataclass
class Completion:
    calls: int = 0

    async def complete_version(self, snapshot: ProjectionSnapshot) -> None:
        self.calls += 1


@dataclass
class Incidents:
    entries: list[ProjectionIncident] = field(default_factory=list)

    async def record_incident(self, incident: ProjectionIncident) -> None:
        self.entries.append(incident)


@pytest.fixture
def snapshot() -> ProjectionSnapshot:
    return ProjectionSnapshot(
        snapshot_id="snapshot-1",
        run_id="run-1",
        version_id="version-1",
        generation=7,
        tombstone_generation=3,
        records=(
            SnapshotRecord(
                deterministic_id="chunk-1",
                canonical_payload_hash="a" * 64,
                projection_type="chunk",
            ),
        ),
    )


def coordinator_for(
    snapshot: ProjectionSnapshot,
    writers: dict[ProjectionBackend, Writer],
    evidence: Evidence,
    completion: Completion | None = None,
    incidents: Incidents | None = None,
) -> ProjectionCoordinator:
    return ProjectionCoordinator(
        source=Source(snapshot),
        writers=writers,
        evidence_store=evidence,
        tombstone_guard=Guard(),
        lifecycle_completer=completion or Completion(),
        incident_sink=incidents,
    )


@pytest.mark.asyncio
async def test_transient_failure_retries_only_failed_backend_and_reuses_verified_evidence(
    snapshot: ProjectionSnapshot,
) -> None:
    writers = {
        ProjectionBackend.QDRANT: Writer(ProjectionBackend.QDRANT, transient_failures=1),
        ProjectionBackend.OPENSEARCH: Writer(ProjectionBackend.OPENSEARCH),
        ProjectionBackend.NEO4J: Writer(ProjectionBackend.NEO4J),
    }
    coordinator = coordinator_for(snapshot, writers, Evidence())

    await coordinator.project_snapshot(snapshot.snapshot_id)
    await coordinator.project_snapshot(snapshot.snapshot_id)

    assert {backend: writer.calls for backend, writer in writers.items()} == {
        ProjectionBackend.QDRANT: 2,
        ProjectionBackend.OPENSEARCH: 1,
        ProjectionBackend.NEO4J: 1,
    }


@pytest.mark.asyncio
async def test_integrity_failure_records_all_outcomes_opens_safe_incident_and_blocks_completion(
    snapshot: ProjectionSnapshot,
) -> None:
    writers = {
        ProjectionBackend.QDRANT: Writer(ProjectionBackend.QDRANT, mismatched_manifest=True),
        ProjectionBackend.OPENSEARCH: Writer(ProjectionBackend.OPENSEARCH),
        ProjectionBackend.NEO4J: Writer(ProjectionBackend.NEO4J),
    }
    evidence = Evidence()
    completion = Completion()
    incidents = Incidents()
    coordinator = coordinator_for(snapshot, writers, evidence, completion, incidents)

    with pytest.raises(ProjectionIntegrityError):
        await coordinator.project_snapshot(snapshot.snapshot_id)

    assert {outcome.backend for outcome in evidence.outcomes} == set(ProjectionBackend)
    assert len(incidents.entries) == 1
    incident = incidents.entries[0]
    assert incident.safe_error_class == "integrity"
    assert "chunk-1" not in repr(incident)
    assert "a" * 64 not in repr(incident)
    with pytest.raises(ProjectionIntegrityError):
        await coordinator.complete_snapshot(snapshot)
    assert completion.calls == 0


@pytest.mark.asyncio
async def test_divergent_durable_replay_records_failure_after_siblings_settle(
    snapshot: ProjectionSnapshot,
) -> None:
    writers = {backend: Writer(backend) for backend in ProjectionBackend}
    evidence = Evidence(
        states={
            ProjectionBackend.QDRANT: WriterOutcome(
                backend=ProjectionBackend.QDRANT,
                snapshot_id=snapshot.snapshot_id,
                generation=snapshot.generation,
                tombstone_generation=snapshot.tombstone_generation,
                status="verified",
                manifest=ProjectionManifest(
                    backend=ProjectionBackend.QDRANT,
                    snapshot_id=snapshot.snapshot_id,
                    generation=snapshot.generation,
                    tombstone_generation=snapshot.tombstone_generation,
                    record_count=len(snapshot.records),
                    checksum="divergent-checksum",
                ),
            )
        }
    )
    incidents = Incidents()
    coordinator = coordinator_for(snapshot, writers, evidence, incidents=incidents)

    with pytest.raises(ProjectionIntegrityError):
        await coordinator.project_snapshot(snapshot.snapshot_id)

    assert writers[ProjectionBackend.QDRANT].calls == 0
    assert writers[ProjectionBackend.OPENSEARCH].calls == 1
    assert writers[ProjectionBackend.NEO4J].calls == 1
    assert {outcome.backend for outcome in evidence.outcomes} == set(ProjectionBackend)
    assert len(incidents.entries) == 1


@pytest.mark.asyncio
async def test_tombstone_prevents_completion(
    snapshot: ProjectionSnapshot,
) -> None:
    """A tombstoned version must not reach completed state."""

    @dataclass
    class TombstoneGuard:
        async def assert_active(self, version_id: str, tombstone_generation: int) -> None:
            raise RuntimeError("Version has been tombstoned")

    writers = {backend: Writer(backend) for backend in ProjectionBackend}
    evidence = Evidence()
    completion = Completion()
    coordinator = ProjectionCoordinator(
        source=Source(snapshot),
        writers=writers,
        evidence_store=evidence,
        tombstone_guard=TombstoneGuard(),
        lifecycle_completer=completion,
    )

    # project_snapshot should fail due to tombstone
    with pytest.raises(RuntimeError, match="tombstoned"):
        await coordinator.project_snapshot(snapshot.snapshot_id)

    # complete_snapshot should also fail
    with pytest.raises((RuntimeError, ProjectionIntegrityError)):
        await coordinator.complete_snapshot(snapshot)

    # Completion must never have been called
    assert completion.calls == 0


@pytest.mark.asyncio
async def test_happy_path_completion_after_all_backends_verified(
    snapshot: ProjectionSnapshot,
) -> None:
    """Completion is reachable after all backends produce matching manifests."""
    writers = {backend: Writer(backend) for backend in ProjectionBackend}
    evidence = Evidence()
    completion = Completion()
    coordinator = coordinator_for(snapshot, writers, evidence, completion)

    # Project successfully
    await coordinator.project_snapshot(snapshot.snapshot_id)

    # All 3 backends should have recorded outcomes with manifests
    assert len(evidence.outcomes) == 3
    assert all(o.manifest is not None for o in evidence.outcomes)

    # Verify the snapshot (required before complete)
    manifests = await coordinator.verify_snapshot(snapshot)
    assert len(manifests) == 3

    # Complete should work after successful projection + verification
    result = await coordinator.complete_snapshot(snapshot)
    assert completion.calls == 1
    assert isinstance(result, dict)
