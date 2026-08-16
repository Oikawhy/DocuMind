"""rerank_evidence — typed reranker wrapper tool per §7.6.

Wraps ``RerankerService`` — operates only on permitted evidence IDs.
Pinned model inference (deterministic from the pipeline's perspective).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Versioned input / output schemas
# ---------------------------------------------------------------------------


class RerankEvidenceInput(BaseModel):
    """Input schema for rerank_evidence tool."""

    query: str
    allowed_evidence_ids: list[str]
    schema_version: str = SCHEMA_VERSION


class ScoredEvidenceId(BaseModel):
    """Evidence ID with its reranker score."""

    evidence_id: str
    score: float


class RerankEvidenceOutput(BaseModel):
    """Output schema for rerank_evidence tool."""

    ranked_evidence_ids: list[ScoredEvidenceId] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def rerank_evidence(
    input_data: RerankEvidenceInput,
    reranker_service: Any,
    evidence_cache: Any,
) -> RerankEvidenceOutput:
    """Rerank only permitted evidence IDs using the BGE cross-encoder.

    Retrieves content from the evidence cache for scoring, then returns
    ordered scored evidence IDs (not full content).
    """
    from documind.schemas.retrieval import ScoredChunk

    # Build ScoredChunk objects from the evidence cache for reranking.
    chunks: list[ScoredChunk] = []
    id_to_chunk: dict[str, ScoredChunk] = {}

    for eid in input_data.allowed_evidence_ids:
        content = evidence_cache.get(eid) if evidence_cache else None
        if content is None:
            # Skip evidence not in cache — it shouldn't be scored.
            continue

        # Create a minimal ScoredChunk for the reranker.
        import uuid

        chunk = ScoredChunk(
            chunk_id=uuid.UUID(eid),
            version_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            document_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            content=content,
            content_sha256="",
            score=0.0,
            source_branch="rag",
        )
        chunks.append(chunk)
        id_to_chunk[eid] = chunk

    if not chunks:
        return RerankEvidenceOutput(ranked_evidence_ids=[])

    try:
        reranked = await reranker_service.rerank(input_data.query, chunks)
    except Exception:
        # Reranker unavailable — return unranked in original order.
        return RerankEvidenceOutput(
            ranked_evidence_ids=[
                ScoredEvidenceId(evidence_id=eid, score=0.0) for eid in input_data.allowed_evidence_ids
            ]
        )

    ranked = [
        ScoredEvidenceId(evidence_id=str(chunk.chunk_id), score=chunk.score) for chunk in reranked
    ]

    return RerankEvidenceOutput(ranked_evidence_ids=ranked)
