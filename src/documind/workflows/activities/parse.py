"""Temporal Docling/RapidOCR parse activity with recorded fallback provenance."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from temporalio import activity

from documind.services.ocr_service import OCRService
from documind.workflows.activities.inspect import (
    TombstoneGuard,
    _assert_active,
    _heartbeat_loop,
    _run_stage,
    _with_stage_checksum,
)
from documind.workflows.document_version import StageExecution, StageReplayStore

_ocr_service: OCRService | None = None


def configure_parse_activity(
    ocr_service: OCRService,
    *,
    tombstone_guard: TombstoneGuard | None = None,
    stage_store: StageReplayStore,
) -> None:
    """Inject worker-owned parser services and durability guards."""
    global _ocr_service
    _ocr_service = ocr_service
    # Share the same guarded stage adapter with the other ingest activities.
    from documind.workflows.activities import inspect as inspect_activity

    inspect_activity._tombstone_guard = tombstone_guard
    inspect_activity._stage_store = stage_store


@activity.defn(name="parse")
async def parse(stage: StageExecution) -> dict[str, Any]:
    """Run the explicit parser chain after inspection has passed."""
    service = _ocr_service
    if service is None:
        raise RuntimeError("Parse activity has not been configured.")
    await _assert_active(stage)

    async def execute() -> dict[str, Any]:
        # T5.6-04: Return only checksums and metadata, not document text.
        result = await service.parse(uuid.UUID(stage.version_id))
        content_sha256 = hashlib.sha256(result.text.encode("utf-8")).hexdigest()
        return {
            "success": True,
            "content_sha256": content_sha256,
            "page_count": len(result.pages),
            "parser_used": getattr(result, "parser_used", None),
        }

    async with _heartbeat_loop(stage):
        output = await _run_stage(stage, execute, max_attempts=2)
    await _assert_active(stage)
    return _with_stage_checksum(output)
