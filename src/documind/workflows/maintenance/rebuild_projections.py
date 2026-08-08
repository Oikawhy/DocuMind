"""Temporal workflow for rebuilding projections from canonical PostgreSQL data.

Each rebuild workflow targets one projection backend (qdrant, opensearch, neo4j),
replays canonical non-tombstoned facts/chunks, verifies counts/checksums, and
atomically switches the active generation pointer per §6.2.
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


def configure_rebuild_activities(
    coordinator: ProjectionCoordinator,
    *,
    neo4j_rebuilder: Neo4jGraphRebuilder | None = None,
) -> None:
    """Inject worker-owned rebuild dependencies."""
    global _coordinator, _neo4j_rebuilder
    _coordinator = coordinator
    _neo4j_rebuilder = neo4j_rebuilder


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn(name="rebuild_projection")
async def rebuild_projection(input_data: dict[str, Any]) -> dict[str, Any]:
    """Replay canonical facts/chunks for one backend into a new generation.

    The activity receives a snapshot_id that identifies the frozen canonical
    data to replay. The coordinator resolves and projects it.
    """
    coordinator = _coordinator
    if coordinator is None:
        raise RuntimeError("Rebuild activities have not been configured.")

    snapshot_id = input_data["snapshot_id"]
    backend = input_data["backend"]

    activity.heartbeat({"phase": "rebuilding", "backend": backend})

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
    coordinator = _coordinator
    if coordinator is None:
        raise RuntimeError("Rebuild activities have not been configured.")

    snapshot_id = input_data["snapshot_id"]
    backend = input_data["backend"]

    activity.heartbeat({"phase": "activating", "backend": backend})

    snapshot = await coordinator.snapshot(snapshot_id)
    result = await coordinator.complete_snapshot(snapshot)
    return dict(result)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow.defn
class RebuildProjectionWorkflow:
    """Rebuild one projection backend from canonical PostgreSQL data.

    Steps:
    1. rebuild_projection — replay canonical data into new generation
    2. verify_rebuild — count/checksum comparison
    3. activate_generation — atomically switch active pointer

    The prior verified generation remains active during rebuild (no gap).
    """

    @workflow.run
    async def run(self, input: RebuildProjectionInput) -> RebuildProjectionOutput:
        """Execute the three-phase rebuild cycle."""
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=30),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=10),
            maximum_attempts=3,
        )

        # Phase 1: Rebuild
        rebuild_result = await workflow.execute_activity(
            rebuild_projection,
            {
                "snapshot_id": f"rebuild-{input.backend}-{input.scope}",
                "backend": input.backend,
            },
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=retry,
        )

        snapshot_id = rebuild_result.get("snapshot_id", "")
        generation = rebuild_result.get("generation", 0)

        # Phase 2: Verify
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
                {"snapshot_id": snapshot_id, "backend": input.backend},
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
