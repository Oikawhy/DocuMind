"""End-to-end contract for the immutable verified-projection tracer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from documind.services.projection_service import (
    ProjectionBackend,
    ProjectionCoordinator,
    ProjectionManifest,
    ProjectionSnapshot,
    SnapshotRecord,
    manifest_checksum,
)


@dataclass
class FrozenSource:
    snapshots: list[ProjectionSnapshot]

    async def resolve_snapshot(self, snapshot_id: str) -> ProjectionSnapshot:
        snapshot = ProjectionSnapshot(
            snapshot_id=snapshot_id,
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
        self.snapshots.append(snapshot)
        return snapshot


@dataclass
class ConcurrencyTracker:
    active: int = 0
    maximum_active: int = 0


@dataclass
class ConcurrentWriter:
    backend: ProjectionBackend
    tracker: ConcurrencyTracker
    received: list[ProjectionSnapshot] = field(default_factory=list)

    async def project(self, snapshot: ProjectionSnapshot) -> ProjectionManifest:
        self.received.append(snapshot)
        self.tracker.active += 1
        self.tracker.maximum_active = max(self.tracker.maximum_active, self.tracker.active)
        await asyncio.sleep(0)
        self.tracker.active -= 1
        return ProjectionManifest(
            backend=self.backend,
            snapshot_id=snapshot.snapshot_id,
            generation=snapshot.generation,
            tombstone_generation=snapshot.tombstone_generation,
            record_count=len(snapshot.records),
            checksum=manifest_checksum(snapshot.records),
        )


@dataclass
class Evidence:
    outcomes: list[object] = field(default_factory=list)
    manifests: list[ProjectionManifest] = field(default_factory=list)

    async def state_for(self, backend: ProjectionBackend, snapshot: ProjectionSnapshot) -> object | None:
        return None

    async def record_outcome(self, outcome: object) -> None:
        self.outcomes.append(outcome)

    async def record_manifest(self, manifest: ProjectionManifest) -> None:
        self.manifests.append(manifest)


@dataclass
class Guard:
    calls: list[str] = field(default_factory=list)

    async def assert_active(self, version_id: str, tombstone_generation: int) -> None:
        self.calls.append(f"{version_id}:{tombstone_generation}")


@dataclass
class Completion:
    completed: list[ProjectionSnapshot] = field(default_factory=list)

    async def complete_version(self, snapshot: ProjectionSnapshot) -> None:
        self.completed.append(snapshot)


@pytest.mark.asyncio
async def test_projection_tracer_freezes_fans_out_verifies_and_completes_without_payload_leakage() -> None:
    source = FrozenSource([])
    tracker = ConcurrencyTracker()
    writers = {backend: ConcurrentWriter(backend, tracker) for backend in ProjectionBackend}
    evidence = Evidence()
    guard = Guard()
    completion = Completion()
    coordinator = ProjectionCoordinator(
        source=source,
        writers=writers,
        evidence_store=evidence,
        tombstone_guard=guard,
        lifecycle_completer=completion,
    )

    snapshot = await coordinator.project_snapshot("snapshot-1")
    await coordinator.verify_snapshot(snapshot)
    output = await coordinator.complete_snapshot(snapshot)

    assert len(source.snapshots) == 1
    assert {writer.received[0].snapshot_id for writer in writers.values()} == {"snapshot-1"}
    assert {writer.received[0].generation for writer in writers.values()} == {7}
    assert {writer.received[0].tombstone_generation for writer in writers.values()} == {3}
    assert tracker.maximum_active == 3
    assert len(evidence.outcomes) == 3
    assert len(evidence.manifests) == 3
    assert guard.calls == ["version-1:3", "version-1:3"]
    assert completion.completed == [snapshot]
    assert output == {
        "snapshot_id": "snapshot-1",
        "run_id": "run-1",
        "generation": 7,
        "tombstone_generation": 3,
        "status": "completed",
    }
    serialized = repr(output)
    for forbidden in ("chunk-1", "a" * 64, "vector", "credential", "fact"):
        assert forbidden not in serialized
