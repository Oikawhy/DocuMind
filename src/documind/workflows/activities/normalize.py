"""Temporal normalization activity for reproducible document representation."""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity

from documind.services.processing_service import ProcessingService
from documind.workflows.activities.inspect import (
    TombstoneGuard,
    _assert_active,
    _heartbeat_loop,
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
async def normalize(stage: StageExecution) -> dict[str, Any]:
    """Normalize a successful parse; the parse result is loaded from PostgreSQL.

    Returns only checksums and metadata — NEVER full text content.
    The actual normalized artifact is persisted to MinIO by the service;
    downstream activities load it from storage, not workflow history.
    """
    service = _processing_service
    if service is None:
        raise RuntimeError("Normalize activity has not been configured.")
    await _assert_active(stage)

    async def execute() -> dict[str, Any]:
        result = await service.normalize(uuid.UUID(stage.version_id))
        # Return only checksums and metadata for compact Temporal history.
        return {
            "version_id": result.version_id,
            "normalization_revision": result.normalization_revision,
            "content_sha256": result.content_sha256,
            "normalized_object_key": result.normalized_object_key,
            "block_count": len(result.blocks),
            "page_count": len(result.pages),
            "parser_attempts": result.parser_attempts,
        }

    async with _heartbeat_loop(stage):
        output = await _run_stage(stage, execute, max_attempts=3)
    await _assert_active(stage)
    return _with_stage_checksum(output)
