"""Immutable, verified coordination for rebuildable search projections.

The coordinator deliberately has no Qdrant, OpenSearch, Neo4j, or database
client knowledge.  Worker composition injects durable authority and derived
store adapters through the narrow ports below.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ProjectionBackend(StrEnum):
    """The complete, fixed set of rebuildable projection writers."""

    QDRANT = "qdrant"
    OPENSEARCH = "opensearch"
    NEO4J = "neo4j"


@dataclass(frozen=True)
class SnapshotRecord:
    """One canonical record represented only by deterministic integrity data."""

    deterministic_id: str
    canonical_payload_hash: str
    projection_type: str


@dataclass(frozen=True)
class ProjectionSnapshot:
    """Frozen canonical identity shared by every backend writer."""

    snapshot_id: str
    run_id: str
    version_id: str
    generation: int
    tombstone_generation: int
    records: tuple[SnapshotRecord, ...]


@dataclass(frozen=True)
class ProjectionManifest:
    """Content-free observed or expected projection evidence."""

    backend: ProjectionBackend
    snapshot_id: str
    generation: int
    tombstone_generation: int
    record_count: int
    checksum: str


@dataclass(frozen=True)
class WriterOutcome:
    """Durable writer state with a safe error class when applicable."""

    backend: ProjectionBackend
    snapshot_id: str
    generation: int
    tombstone_generation: int
    status: str
    manifest: ProjectionManifest | None = None
    safe_error_class: str | None = None


@dataclass(frozen=True)
class ProjectionIncident:
    """Content-free integrity incident suitable for audit storage."""

    run_id: str
    snapshot_id: str
    projection_type: ProjectionBackend
    generation: int
    expected_count: int
    expected_checksum: str
    observed_count: int | None
    observed_checksum: str | None
    tombstone_generation: int
    safe_error_class: str


class ProjectionTransientError(RuntimeError):
    """A backend-local dependency failure that may be retried once."""


class ProjectionIntegrityError(RuntimeError):
    """Immutable snapshot or manifest evidence no longer agrees."""


class CanonicalProjectionSource(Protocol):
    """Resolve a persisted, immutable canonical snapshot by identity."""

    async def resolve_snapshot(self, snapshot_id: str) -> ProjectionSnapshot: ...


class ProjectionWriter(Protocol):
    """Write exactly one backend's records for a frozen snapshot."""

    async def project(self, snapshot: ProjectionSnapshot) -> ProjectionManifest: ...


class ProjectionEvidenceStore(Protocol):
    """Persist and retrieve only content-free writer evidence."""

    async def state_for(self, backend: ProjectionBackend, snapshot: ProjectionSnapshot) -> WriterOutcome | None: ...

    async def record_outcome(self, outcome: WriterOutcome) -> None: ...

    async def record_manifest(self, manifest: ProjectionManifest) -> None: ...


class ProjectionIncidentSink(Protocol):
    """Persist an integrity incident without derived payload content."""

    async def record_incident(self, incident: ProjectionIncident) -> None: ...


class TombstoneGuard(Protocol):
    """Check authoritative lifecycle state before a side effect or completion."""

    async def assert_active(self, version_id: str, tombstone_generation: int) -> None: ...


class LifecycleCompleter(Protocol):
    """Perform the authoritative lifecycle transition after verification."""

    async def complete_version(self, snapshot: ProjectionSnapshot) -> None: ...


def manifest_checksum(records: tuple[SnapshotRecord, ...] | list[SnapshotRecord]) -> str:
    """Return a deterministic SHA-256 for canonical projection records."""
    canonical = [
        {
            "canonical_payload_hash": record.canonical_payload_hash,
            "deterministic_id": record.deterministic_id,
            "projection_type": record.projection_type,
        }
        for record in sorted(records, key=lambda item: item.deterministic_id)
    ]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProjectionCoordinator:
    """Freeze, fan out, verify, and complete one projection snapshot safely."""

    def __init__(
        self,
        *,
        source: CanonicalProjectionSource,
        writers: Mapping[ProjectionBackend, ProjectionWriter],
        evidence_store: ProjectionEvidenceStore,
        tombstone_guard: TombstoneGuard,
        lifecycle_completer: LifecycleCompleter,
        incident_sink: ProjectionIncidentSink | None = None,
        max_backend_attempts: int = 2,
    ) -> None:
        missing = set(ProjectionBackend).difference(writers)
        if missing:
            raise ValueError(f"Missing projection writers: {sorted(item.value for item in missing)}")
        if max_backend_attempts < 1:
            raise ValueError("max_backend_attempts must be at least one")
        self._source = source
        self._writers = dict(writers)
        self._evidence_store = evidence_store
        self._tombstone_guard = tombstone_guard
        self._lifecycle_completer = lifecycle_completer
        self._incident_sink = incident_sink
        self._max_backend_attempts = max_backend_attempts
        self._snapshots: dict[str, ProjectionSnapshot] = {}
        self._outcomes: dict[str, dict[ProjectionBackend, WriterOutcome]] = {}
        self._verified: set[str] = set()

    async def snapshot(self, snapshot_id: str) -> ProjectionSnapshot:
        """Load a persisted frozen snapshot once per coordinator lifetime."""
        existing = self._snapshots.get(snapshot_id)
        if existing is not None:
            return existing
        snapshot = await self._source.resolve_snapshot(snapshot_id)
        if snapshot.snapshot_id != snapshot_id:
            raise ProjectionIntegrityError("Canonical source returned a divergent snapshot identity.")
        self._snapshots[snapshot_id] = snapshot
        return snapshot

    async def project_snapshot(self, snapshot_id: str) -> ProjectionSnapshot:
        """Fan all writers out concurrently from one frozen snapshot."""
        snapshot = await self.snapshot(snapshot_id)
        await self._tombstone_guard.assert_active(snapshot.version_id, snapshot.tombstone_generation)
        outcomes: dict[ProjectionBackend, WriterOutcome] = {}

        async def project_backend(backend: ProjectionBackend) -> None:
            existing = await self._evidence_store.state_for(backend, snapshot)
            if existing is not None:
                try:
                    self._assert_replay_matches(snapshot, backend, existing)
                except ProjectionIntegrityError:
                    # A durable mismatch is integrity evidence, not a task-group
                    # exception: siblings must finish and every backend needs a
                    # recorded state before the run stops for operator review.
                    outcome = WriterOutcome(
                        backend=backend,
                        snapshot_id=snapshot.snapshot_id,
                        generation=snapshot.generation,
                        tombstone_generation=snapshot.tombstone_generation,
                        status="failed",
                        manifest=existing.manifest,
                        safe_error_class="integrity",
                    )
                    outcomes[backend] = outcome
                    await self._evidence_store.record_outcome(outcome)
                    return
                if existing.status in {"projected", "verified"} and existing.manifest is not None:
                    outcomes[backend] = existing
                    return
            outcome = await self._write_backend(backend, snapshot)
            outcomes[backend] = outcome
            await self._evidence_store.record_outcome(outcome)

        # Every task handles its own failure and records an outcome.  That lets
        # siblings settle instead of TaskGroup cancelling useful evidence.
        async with asyncio.TaskGroup() as task_group:
            for backend in ProjectionBackend:
                task_group.create_task(project_backend(backend))

        self._outcomes[snapshot.snapshot_id] = outcomes
        failures = [outcome for outcome in outcomes.values() if outcome.status != "projected"]
        if failures:
            if any(outcome.safe_error_class == "integrity" for outcome in failures):
                await self._record_integrity_incident(snapshot, failures[0])
                raise ProjectionIntegrityError("A projection writer produced an integrity failure.")
            raise ProjectionTransientError("A projection backend did not complete its bounded retry.")
        return snapshot

    async def verify_snapshot(self, snapshot: ProjectionSnapshot) -> tuple[ProjectionManifest, ...]:
        """Require three matching full manifests before completion is reachable."""
        outcomes = self._outcomes.get(snapshot.snapshot_id)
        if outcomes is None:
            raise ProjectionIntegrityError("Projection verification has no recorded writer outcomes.")
        manifests: list[ProjectionManifest] = []
        for backend in ProjectionBackend:
            outcome = outcomes.get(backend)
            if outcome is None or outcome.manifest is None or outcome.status != "projected":
                raise ProjectionIntegrityError("Projection verification requires all writer outcomes.")
            expected = self._expected_manifest(snapshot, backend)
            if outcome.manifest != expected:
                await self._record_integrity_incident(snapshot, outcome)
                raise ProjectionIntegrityError(f"{backend.value} manifest does not match the frozen snapshot.")
            manifests.append(outcome.manifest)
        for manifest in manifests:
            await self._evidence_store.record_manifest(manifest)
        self._verified.add(snapshot.snapshot_id)
        return tuple(manifests)

    async def complete_snapshot(self, snapshot: ProjectionSnapshot) -> dict[str, int | str]:
        """Make the one authoritative completion call after the final guard."""
        if snapshot.snapshot_id not in self._verified:
            raise ProjectionIntegrityError("Completion requires three verified projection manifests.")
        await self._tombstone_guard.assert_active(snapshot.version_id, snapshot.tombstone_generation)
        await self._lifecycle_completer.complete_version(snapshot)
        return self.activity_output(snapshot, status="completed")

    @staticmethod
    def activity_output(snapshot: ProjectionSnapshot, *, status: str) -> dict[str, int | str]:
        """Return compact Temporal-safe metadata, never canonical projection data."""
        return {
            "snapshot_id": snapshot.snapshot_id,
            "run_id": snapshot.run_id,
            "generation": snapshot.generation,
            "tombstone_generation": snapshot.tombstone_generation,
            "status": status,
        }

    async def _write_backend(self, backend: ProjectionBackend, snapshot: ProjectionSnapshot) -> WriterOutcome:
        writer = self._writers[backend]
        for attempt in range(1, self._max_backend_attempts + 1):
            try:
                manifest = await writer.project(snapshot)
            except ProjectionTransientError:
                if attempt < self._max_backend_attempts:
                    continue
                return WriterOutcome(
                    backend=backend,
                    snapshot_id=snapshot.snapshot_id,
                    generation=snapshot.generation,
                    tombstone_generation=snapshot.tombstone_generation,
                    status="failed",
                    safe_error_class="transient_dependency",
                )
            except ProjectionIntegrityError:
                return WriterOutcome(
                    backend=backend,
                    snapshot_id=snapshot.snapshot_id,
                    generation=snapshot.generation,
                    tombstone_generation=snapshot.tombstone_generation,
                    status="failed",
                    safe_error_class="integrity",
                )
            expected = self._expected_manifest(snapshot, backend)
            if manifest != expected:
                return WriterOutcome(
                    backend=backend,
                    snapshot_id=snapshot.snapshot_id,
                    generation=snapshot.generation,
                    tombstone_generation=snapshot.tombstone_generation,
                    status="failed",
                    manifest=manifest,
                    safe_error_class="integrity",
                )
            return WriterOutcome(
                backend=backend,
                snapshot_id=snapshot.snapshot_id,
                generation=snapshot.generation,
                tombstone_generation=snapshot.tombstone_generation,
                status="projected",
                manifest=manifest,
            )
        raise AssertionError("bounded backend retry loop exited unexpectedly")

    def _expected_manifest(self, snapshot: ProjectionSnapshot, backend: ProjectionBackend) -> ProjectionManifest:
        return ProjectionManifest(
            backend=backend,
            snapshot_id=snapshot.snapshot_id,
            generation=snapshot.generation,
            tombstone_generation=snapshot.tombstone_generation,
            record_count=len(snapshot.records),
            checksum=manifest_checksum(snapshot.records),
        )

    def _assert_replay_matches(
        self, snapshot: ProjectionSnapshot, backend: ProjectionBackend, outcome: WriterOutcome
    ) -> None:
        if (
            outcome.backend != backend
            or outcome.snapshot_id != snapshot.snapshot_id
            or outcome.generation != snapshot.generation
            or outcome.tombstone_generation != snapshot.tombstone_generation
        ):
            raise ProjectionIntegrityError("Durable projection replay identity diverged from the frozen snapshot.")
        if outcome.manifest is not None and outcome.manifest != self._expected_manifest(snapshot, backend):
            raise ProjectionIntegrityError("Durable projection replay checksum diverged from expected evidence.")

    async def _record_integrity_incident(self, snapshot: ProjectionSnapshot, outcome: WriterOutcome) -> None:
        if self._incident_sink is None:
            return
        expected = self._expected_manifest(snapshot, outcome.backend)
        observed = outcome.manifest
        await self._incident_sink.record_incident(
            ProjectionIncident(
                run_id=snapshot.run_id,
                snapshot_id=snapshot.snapshot_id,
                projection_type=outcome.backend,
                generation=snapshot.generation,
                expected_count=expected.record_count,
                expected_checksum=expected.checksum,
                observed_count=observed.record_count if observed else None,
                observed_checksum=observed.checksum if observed else None,
                tombstone_generation=snapshot.tombstone_generation,
                safe_error_class=outcome.safe_error_class or "integrity",
            )
        )
