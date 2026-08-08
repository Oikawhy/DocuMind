"""Temporal enrichment activity: type suggestion, template extraction, and graph facts.

Follows the same pattern as chunk.py: inject dependencies via
``configure_enrich_activity``, execute within the durable stage replay
store, and guard against tombstoned versions.

The enrich activity receives only a ``StageExecution`` with the checksum of
the chunk stage's output.  It resolves version metadata, chunks, and template
revisions from PostgreSQL — never from workflow history payloads.
"""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity

from documind.services.enrichment_service import EnrichmentService
from documind.workflows.activities.inspect import (
    TombstoneGuard,
    _assert_active,
    _run_stage,
    _with_stage_checksum,
)
from documind.workflows.document_version import StageExecution, StageReplayStore

_enrichment_service: EnrichmentService | None = None


def configure_enrich_activity(
    enrichment_service: EnrichmentService,
    *,
    tombstone_guard: TombstoneGuard | None = None,
    stage_store: StageReplayStore,
) -> None:
    """Inject worker-owned enrichment dependencies."""
    global _enrichment_service
    _enrichment_service = enrichment_service
    from documind.workflows.activities import inspect as inspect_activity

    inspect_activity._tombstone_guard = tombstone_guard
    inspect_activity._stage_store = stage_store


@activity.defn(name="enrich")
async def enrich(stage: StageExecution) -> dict[str, Any]:
    """Enrich a chunked document version with type suggestions, extractions, and facts.

    The activity resolves version metadata, chunks, and the pinned template
    from PostgreSQL using the version ID — never from workflow history payloads.
    Each stage receives an immutable checksum of its predecessor rather than
    document data in workflow history.
    """
    service = _enrichment_service
    if service is None:
        raise RuntimeError("Enrich activity has not been configured.")
    await _assert_active(stage)
    activity.heartbeat({"stage": stage.name, "version_id": stage.version_id})

    async def execute() -> dict[str, Any]:
        result = await service.enrich(
            version_id=uuid.UUID(stage.version_id),
        )
        return {
            "type_suggestion": result.type_suggestion,
            "extraction_status": result.extraction_status,
            "extraction_id": (str(result.extraction_id) if result.extraction_id else None),
            "proposal_id": (str(result.proposal_id) if result.proposal_id else None),
            "fact_result": {
                "entities_created": (result.fact_result.entities_created if result.fact_result else 0),
                "facts_created": (result.fact_result.facts_created if result.fact_result else 0),
                "facts_corroborated": (result.fact_result.facts_corroborated if result.fact_result else 0),
            },
            "errors": result.errors,
            "route_revision_ids": [str(rid) for rid in (getattr(result, "route_revision_ids", None) or [])],
        }

    output = await _run_stage(stage, execute, max_attempts=2)
    await _assert_active(stage)
    activity.heartbeat({"stage": stage.name, "version_id": stage.version_id, "complete": True})
    return _with_stage_checksum(output)
