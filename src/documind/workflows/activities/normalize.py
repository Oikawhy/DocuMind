"""Temporal normalization activity for reproducible document representation."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from temporalio import activity

from documind.services.processing_service import ProcessingService
from documind.workflows.activities.inspect import (
    TombstoneGuard,
    _assert_active,
    _run_stage,
    _with_stage_checksum,
)
from documind.workflows.document_version import StageExecution, StageReplayStore

_processing_service: ProcessingService | None = None


def configure_normalize_activity(
    processing_service: ProcessingService,
    *,
    tombstone_guard: TombstoneGuard | None = None,
    stage_store: StageReplayStore,
) -> None:
    """Inject worker-owned normalizer and mandatory lifecycle guard."""
    global _processing_service
    _processing_service = processing_service
    from documind.workflows.activities import inspect as inspect_activity

    inspect_activity._tombstone_guard = tombstone_guard
    inspect_activity._stage_store = stage_store


@activity.defn(name="normalize")
async def normalize(stage: StageExecution, parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize a successful parse; the parse payload preserves workflow evidence."""
    service = _processing_service
    if service is None:
        raise RuntimeError("Normalize activity has not been configured.")
    if not parsed.get("success", False):
        raise ValueError("Normalization cannot run without a successful parse.")
    await _assert_active(stage)
    activity.heartbeat({"stage": stage.name, "version_id": stage.version_id})

    async def execute() -> dict[str, Any]:
        return asdict(await service.normalize(uuid.UUID(stage.version_id)))

    output = await _run_stage(stage, execute, max_attempts=3)
    await _assert_active(stage)
    return _with_stage_checksum(output)
