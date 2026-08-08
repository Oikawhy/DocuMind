"""Temporal activity that makes tombstone-gated lifecycle completion reachable."""

from __future__ import annotations

from typing import Any

from temporalio import activity

from documind.services.projection_service import ProjectionCoordinator
from documind.workflows.activities.inspect import TombstoneGuard, _assert_active, _run_stage, _with_stage_checksum
from documind.workflows.document_version import StageExecution, StageReplayStore

_coordinator: ProjectionCoordinator | None = None


def configure_complete_activity(
    coordinator: ProjectionCoordinator,
    *,
    tombstone_guard: TombstoneGuard | None = None,
    stage_store: StageReplayStore,
) -> None:
    """Inject worker-owned lifecycle dependencies."""
    global _coordinator
    _coordinator = coordinator
    from documind.workflows.activities import inspect as inspect_activity

    inspect_activity._tombstone_guard = tombstone_guard
    inspect_activity._stage_store = stage_store


@activity.defn(name="complete")
async def complete(stage: StageExecution) -> dict[str, Any]:
    """Complete only after verified evidence and a final authoritative guard."""
    coordinator = _coordinator
    if coordinator is None:
        raise RuntimeError("Complete activity has not been configured.")
    await _assert_active(stage)
    activity.heartbeat({"stage": stage.name, "version_id": stage.version_id})

    async def execute() -> dict[str, Any]:
        snapshot = await coordinator.snapshot(stage.input_sha256)
        return await coordinator.complete_snapshot(snapshot)

    output = await _run_stage(stage, execute, max_attempts=1)
    await _assert_active(stage)
    activity.heartbeat({"stage": stage.name, "version_id": stage.version_id, "complete": True})
    return _with_stage_checksum(output)
