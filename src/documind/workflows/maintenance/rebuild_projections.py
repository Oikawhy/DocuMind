"""Temporal workflow for rebuilding projections from canonical PostgreSQL data.

Each rebuild workflow targets one projection backend (qdrant, opensearch, neo4j),
replays canonical non-tombstoned facts/chunks, verifies counts/checksums, and
atomically switches the active generation pointer per §6.2.

T6-22: Rebuild activities now respect scope/scope_id and fan out to only
       the named backend instead of the full coordinator fanout.
T6-23: Rebuilds allocate a new generation before replaying data.
T6-13: Neo4j-specific rebuilds use Neo4jGraphRebuilder when available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from documind.services.graph_service import Neo4jGraphRebuilder
from documind.services.projection_service import (
    ProjectionCoordinator,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workflow input / output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RebuildProjectionInput:
    """Immutable input for one projection rebuild."""

    backend: str  # ProjectionBackend value: "qdrant", "opensearch", "neo4j"
    scope: str = "full"  # "full" or "version"
    scope_id: str | None = None  # version_id when scope == "version"
    reason: str = "manual"
    requested_by: str = "system"


@dataclass(frozen=True)
class RebuildProjectionOutput:
    """Result of a projection rebuild."""

    backend: str
    new_generation: int
    record_count: int
    verified: bool
    activated: bool


# ---------------------------------------------------------------------------
# Activity module-level state (configured at worker startup)
# ---------------------------------------------------------------------------

_coordinator: ProjectionCoordinator | None = None
_neo4j_rebuilder: Neo4jGraphRebuilder | None = None
_generation_manager: Any = None


def configure_rebuild_activities(
    coordinator: ProjectionCoordinator,
    *,
    neo4j_rebuilder: Neo4jGraphRebuilder | None = None,
    generation_manager: Any = None,
) -> None:
    """Inject worker-owned rebuild dependencies."""
    global _coordinator, _neo4j_rebuilder, _generation_manager
    _coordinator = coordinator
    _neo4j_rebuilder = neo4j_rebuilder
    _generation_manager = generation_manager


def _build_snapshot_id(backend: str, scope: str, scope_id: str | None) -> str:
    """Construct the canonical snapshot_id for a rebuild.

    T6-22: Scope-aware snapshot_id construction.
    - scope="full":     rebuild-{backend}-full
    - scope="version":  rebuild-{backend}-{scope_id}
    """
    if scope == "version" and scope_id:
        return f"rebuild-{backend}-{scope_id}"
    return f"rebuild-{backend}-full"


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn(name="rebuild_projection")
async def rebuild_projection(input_data: dict[str, Any]) -> dict[str, Any]:
    """Replay canonical facts/chunks for one backend into a new generation.

    T6-22: Uses scope-aware snapshot ID construction.
    T6-23: Allocates a replacement generation before projecting.
    T6-13: Uses Neo4jGraphRebuilder for Neo4j-specific rebuilds.
    """
    coordinator = _coordinator
    if coordinator is None:
        raise RuntimeError("Rebuild activities have not been configured.")

    backend = input_data["backend"]
    snapshot_id = input_data["snapshot_id"]
    generation = input_data.get("generation")

    activity.heartbeat({"phase": "rebuilding", "backend": backend})

    # T6-13: Use dedicated Neo4j rebuilder when available
    if backend == "neo4j" and _neo4j_rebuilder is not None:
        snapshot = await coordinator.snapshot(snapshot_id)
        count = await _neo4j_rebuilder.rebuild(snapshot=snapshot, new_generation=generation or 1)
        output = coordinator.activity_output(snapshot, status="rebuilt")
        output["record_count"] = count
        return output

    # Default: use coordinator for the specified backend
    snapshot = await coordinator.project_snapshot(snapshot_id)
    return coordinator.activity_output(snapshot, status="rebuilt")


@activity.defn(name="verify_rebuild")
async def verify_rebuild(input_data: dict[str, Any]) -> dict[str, Any]:
    """Verify count/checksum match between PostgreSQL and rebuilt projection."""
    coordinator = _coordinator
    if coordinator is None:
        raise RuntimeError("Rebuild activities have not been configured.")

    snapshot_id = input_data["snapshot_id"]
    backend = input_data["backend"]

    activity.heartbeat({"phase": "verifying", "backend": backend})

    snapshot = await coordinator.snapshot(snapshot_id)
    manifests = await coordinator.verify_snapshot(snapshot)
    output = coordinator.activity_output(snapshot, status="verified")
    output["manifest_count"] = len(manifests)
    return output


@activity.defn(name="activate_generation")
async def activate_generation(input_data: dict[str, Any]) -> dict[str, Any]:
    """Atomically switch active generation pointer after verification."""
    manager = _generation_manager
    if manager is None:
        raise RuntimeError("Rebuild activities have not been configured with a generation manager.")

    backend = input_data["backend"]
    generation = input_data["generation"]
    scope_key = input_data.get("scope_key", "global")

    activity.heartbeat({"phase": "activating", "backend": backend})

    await manager.activate(backend, scope_key, generation)

    return {"status": "completed", "backend": backend, "generation": generation}


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow.defn
class RebuildProjectionWorkflow:
    """Rebuild one projection backend from canonical PostgreSQL data.

    Steps:
    0. allocate_generation — reserve a new generation number (T6-23)
    1. rebuild_projection — replay canonical data into new generation
    2. verify_rebuild — count/checksum comparison
    3. activate_generation — atomically switch active pointer

    The prior verified generation remains active during rebuild (no gap).
    T6-22: scope/scope_id are propagated to the snapshot_id and activities.
    T6-24: On verification failure, one retry with a fresh generation is
    attempted before marking the projection unhealthy.
    """

    @workflow.run
    async def run(self, input: RebuildProjectionInput) -> RebuildProjectionOutput:
        """Execute the rebuild cycle."""
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=30),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=10),
            maximum_attempts=3,
        )

        # T6-22: Build scope-aware snapshot_id
        snapshot_id = _build_snapshot_id(input.backend, input.scope, input.scope_id)
        scope_key = input.scope_id or "global"

        # T6-23: Allocate a new generation before rebuild
        generation = await self._allocate_generation(input.backend, scope_key, retry)

        # Phase 1: Rebuild
        rebuild_result = await workflow.execute_activity(
            rebuild_projection,
            {
                "snapshot_id": snapshot_id,
                "backend": input.backend,
                "generation": generation,
            },
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=retry,
        )

        generation = rebuild_result.get("generation", generation)

        # Phase 2: Verify
        verify_result = await workflow.execute_activity(
            verify_rebuild,
            {"snapshot_id": snapshot_id, "backend": input.backend},
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=retry,
        )

        verified = verify_result.get("status") == "verified"

        # T6-24: Recovery — retry once with fresh generation on verification failure
        if not verified:
            workflow.logger.warning(
                "Verification failed for %s/%s gen %d; retrying with fresh generation",
                input.backend,
                scope_key,
                generation,
            )
            generation = await self._allocate_generation(input.backend, scope_key, retry)
            rebuild_result = await workflow.execute_activity(
                rebuild_projection,
                {
                    "snapshot_id": snapshot_id,
                    "backend": input.backend,
                    "generation": generation,
                },
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=retry,
            )
            generation = rebuild_result.get("generation", generation)
            verify_result = await workflow.execute_activity(
                verify_rebuild,
                {"snapshot_id": snapshot_id, "backend": input.backend},
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=retry,
            )
            verified = verify_result.get("status") == "verified"

        # Phase 3: Activate (only if verified)
        activated = False
        if verified:
            activate_result = await workflow.execute_activity(
                activate_generation,
                {
                    "backend": input.backend,
                    "generation": generation,
                    "scope_key": scope_key,
                },
                start_to_close_timeout=timedelta(minutes=2),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=retry,
            )
            activated = activate_result.get("status") == "completed"

        record_count = rebuild_result.get("record_count", 0)
        return RebuildProjectionOutput(
            backend=input.backend,
            new_generation=int(generation),
            record_count=int(record_count) if isinstance(record_count, (int, str)) else 0,
            verified=verified,
            activated=activated,
        )

    @staticmethod
    async def _allocate_generation(
        backend: str, scope_key: str, retry: RetryPolicy,
    ) -> int:
        """Allocate a new generation via the generation manager activity.

        T6-23: Pre-rebuild generation allocation ensures each rebuild
        writes to a fresh namespace that does not overwrite the active one.
        """
        result = await workflow.execute_activity(
            "allocate_generation",
            {"backend": backend, "scope_key": scope_key},
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=retry,
        )
        return result.get("generation", 1)
