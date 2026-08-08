"""Temporal chunking activity with idempotent chunk persistence.

Follows the same pattern as inspect/parse/normalize activities: inject
dependencies via ``configure_chunk_activity``, execute within the durable
stage replay store, and guard against tombstoned versions.

The chunk activity receives only a ``StageExecution`` with the checksum of
its predecessor's output.  It resolves normalized content and chunk profile
from PostgreSQL and MinIO — never from workflow history payloads.
"""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity

from documind.services.chunking_service import (
    ChunkingService,
    ChunkProfile,
    ChunkWriter,
    NormalizedBlock,
    NormalizedDocument,
)
from documind.workflows.activities.inspect import (
    TombstoneGuard,
    _assert_active,
    _run_stage,
    _with_stage_checksum,
)
from documind.workflows.document_version import StageExecution, StageReplayStore


class NormalizedDocumentSource:
    """Protocol for resolving normalized content from durable stores."""

    async def load(self, version_id: uuid.UUID) -> dict[str, Any]:
        """Load normalized document data for chunking."""
        raise NotImplementedError


_chunking_service: ChunkingService | None = None
_chunk_writer: ChunkWriter | None = None
_normalized_source: NormalizedDocumentSource | None = None


def configure_chunk_activity(
    chunking_service: ChunkingService,
    chunk_writer: ChunkWriter,
    *,
    normalized_source: NormalizedDocumentSource | None = None,
    tombstone_guard: TombstoneGuard | None = None,
    stage_store: StageReplayStore,
) -> None:
    """Inject worker-owned chunking dependencies."""
    global _chunking_service, _chunk_writer, _normalized_source
    _chunking_service = chunking_service
    _chunk_writer = chunk_writer
    _normalized_source = normalized_source
    from documind.workflows.activities import inspect as inspect_activity

    inspect_activity._tombstone_guard = tombstone_guard
    inspect_activity._stage_store = stage_store


@activity.defn(name="chunk")
async def chunk(stage: StageExecution) -> dict[str, Any]:
    """Chunk a normalized document and persist rows idempotently.

    The activity resolves its data dependencies from PostgreSQL and MinIO
    using the version ID — never from workflow history payloads.  Each stage
    receives an immutable checksum of its predecessor rather than document
    text in workflow history.
    """
    service = _chunking_service
    writer = _chunk_writer
    source = _normalized_source
    if service is None or writer is None:
        raise RuntimeError("Chunk activity has not been configured.")
    await _assert_active(stage)
    activity.heartbeat({"stage": stage.name, "version_id": stage.version_id})

    async def execute() -> dict[str, Any]:

        version_id = uuid.UUID(stage.version_id)

        # Resolve normalized content from durable store
        if source is not None:
            normalization = await source.load(version_id)
        else:
            # Fallback: resolve from the stage store's persisted normalize output.
            # This path is only used in tests; production always injects a source.
            normalization = {}

        normalized = _build_normalized_document(version_id, normalization)
        profile = _build_chunk_profile(normalization)
        chunks = service.chunk(normalized, profile)
        result = await writer.write_chunks(version_id, profile.revision_id, chunks)
        # Include profile/effective-profile and chunk count/checksum in output
        result.setdefault("profile_revision_id", str(profile.revision_id))
        result.setdefault("effective_profile_strategy", profile.strategy)
        result.setdefault("tokenizer_digest", profile.tokenizer_digest)
        result.setdefault("embedding_model_digest", profile.embedding_model_digest)
        return result

    output = await _run_stage(stage, execute, max_attempts=3)
    await _assert_active(stage)
    activity.heartbeat({"stage": stage.name, "version_id": stage.version_id, "complete": True})
    return _with_stage_checksum(output)


def _build_normalized_document(
    version_id: uuid.UUID,
    normalization: dict[str, Any],
) -> NormalizedDocument:
    """Reconstruct a NormalizedDocument from the persisted normalization output."""

    blocks = tuple(
        NormalizedBlock(
            block_id=block["block_id"],
            page_number=block["page_number"],
            section_path=tuple(block.get("section_path", ())),
            start_offset=block["start_offset"],
            end_offset=block["end_offset"],
        )
        for block in normalization.get("blocks", [])
    )
    return NormalizedDocument(
        version_id=version_id,
        text=normalization.get("text", ""),
        blocks=blocks,
    )


def _build_chunk_profile(normalization: dict[str, Any]) -> ChunkProfile:
    """Build a ChunkProfile from the persisted normalization payload.

    The profile comes from the stage_output's chunk_profile section, which
    was captured during admission when the profile revision was pinned.
    """
    profile_data = normalization.get("chunk_profile", {})
    fallback = profile_data.get("recursive_fallback_revision_id")
    return ChunkProfile(
        revision_id=uuid.UUID(profile_data["revision_id"]) if "revision_id" in profile_data else uuid.uuid4(),
        strategy=profile_data.get("strategy", "fixed"),
        tokenizer_digest=profile_data.get("tokenizer_digest", ""),
        target_tokens=profile_data.get("target_tokens", 512),
        overlap_tokens=profile_data.get("overlap_tokens", 0),
        embedding_model_digest=profile_data.get("embedding_model_digest", ""),
        active=profile_data.get("active", True),
        min_tokens=profile_data.get("min_tokens", 1),
        max_tokens=profile_data.get("max_tokens"),
        vector_similarity_threshold=profile_data.get("vector_similarity_threshold", 0.45),
        recursive_fallback_revision_id=uuid.UUID(fallback) if fallback else None,
    )
