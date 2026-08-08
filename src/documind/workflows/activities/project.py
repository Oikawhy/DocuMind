"""Temporal activity that projects one frozen snapshot through all writers."""

from __future__ import annotations

from typing import Any

from temporalio import activity

from documind.services.projection_service import ProjectionCoordinator
from documind.workflows.activities.inspect import TombstoneGuard, _assert_active, _run_stage, _with_stage_checksum
from documind.workflows.document_version import StageExecution, StageReplayStore

_coordinator: ProjectionCoordinator | None = None


def configure_project_activity(
    coordinator: ProjectionCoordinator,
    *,
    tombstone_guard: TombstoneGuard | None = None,
    stage_store: StageReplayStore,
) -> None:
    """Inject worker-owned projection dependencies."""
    global _coordinator
    _coordinator = coordinator
    from documind.workflows.activities import inspect as inspect_activity

    inspect_activity._tombstone_guard = tombstone_guard
    inspect_activity._stage_store = stage_store


@activity.defn(name="project")
async def project(stage: StageExecution) -> dict[str, Any]:
    """Project one durable snapshot; workflow history carries identity only."""
    coordinator = _coordinator
    if coordinator is None:
        raise RuntimeError("Project activity has not been configured.")
    await _assert_active(stage)
    activity.heartbeat({"stage": stage.name, "version_id": stage.version_id})

    async def execute() -> dict[str, Any]:
        snapshot = await coordinator.project_snapshot(stage.input_sha256)
        return coordinator.activity_output(snapshot, status="projected")

    output = await _run_stage(stage, execute, max_attempts=2)
    await _assert_active(stage)
    activity.heartbeat({"stage": stage.name, "version_id": stage.version_id, "complete": True})
    return _with_stage_checksum(output)
