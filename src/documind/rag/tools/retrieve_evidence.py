"""retrieve_evidence — typed retrieval wrapper tool per §7.6.

Wraps ``RetrievalService.search()`` with typed input/output schemas.
Returns candidate/evidence IDs and timings only — not full content.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Versioned input / output schemas
# ---------------------------------------------------------------------------


class RetrieveEvidenceInput(BaseModel):
    """Input schema for retrieve_evidence tool."""

    queries: list[str] = Field(..., max_length=3)
    document_ids: list[str] = Field(default_factory=list, max_length=20)
    mode: str | None = None
    locale: str = "en"
    principal_subject: str
    policy_context_id: str | None = None
    max_candidates: int = 100
    schema_version: str = SCHEMA_VERSION


class BranchTiming(BaseModel):
    """Timing information for a single retrieval branch."""

    branch: str
    elapsed_ms: int
    candidate_count: int


class RetrieveEvidenceOutput(BaseModel):
    """Output schema for retrieve_evidence tool."""

    candidate_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    branch_timings: list[BranchTiming] = Field(default_factory=list)
    total_elapsed_ms: int = 0
    degraded_branches: list[str] = Field(default_factory=list)
    mode_used: str = "hybrid"
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def retrieve_evidence(
    input_data: RetrieveEvidenceInput,
    retrieval_service: Any,
    principal: Any,
) -> RetrieveEvidenceOutput:
    """Execute retrieval via the deterministic retrieval service (§6).

    Saves branch result IDs and timings only.  Full content is loaded
    separately by the ``load_evidence`` tool after permission checks.
    """
    from documind.schemas.retrieval import RetrievalRequest

    start = time.monotonic()
    all_candidate_ids: list[str] = []
    all_evidence_ids: list[str] = []
    branch_timings: list[BranchTiming] = []
    degraded: list[str] = []
    mode_used = "hybrid"

    # Execute retrieval for each query variant.
    for query in input_data.queries:
        doc_uuids = [uuid.UUID(d) for d in input_data.document_ids] if input_data.document_ids else []
        request = RetrievalRequest(
            query=query,
            locale=input_data.locale,
            document_ids=doc_uuids,
            mode=input_data.mode,
        )

        try:
            response = await retrieval_service.search(
                request=request,
                principal=principal,
            )

            # Collect IDs from evidence items.
            for item in response.evidence:
                eid = str(item.chunk_id)
                if eid not in all_evidence_ids:
                    all_evidence_ids.append(eid)

            # Metadata.
            meta = response.retrieval_metadata
            mode_used = meta.mode
            for branch_name, elapsed in meta.backend_timings.items():
                branch_timings.append(
                    BranchTiming(
                        branch=branch_name,
                        elapsed_ms=elapsed,
                        candidate_count=meta.candidate_count_before_auth,
                    )
                )
            degraded.extend(response.degraded_branches)

            # Candidate IDs come from metadata counts — we track evidence IDs as candidates
            # at this stage since the full candidate set is only available internally.
            all_candidate_ids.extend(all_evidence_ids)

        except Exception:
            # Record the branch as degraded but don't fail the whole retrieval.
            degraded.append(f"query_variant_{input_data.queries.index(query)}")

    total_ms = int((time.monotonic() - start) * 1000)

    # Deduplicate.
    seen_candidates: list[str] = []
    for cid in all_candidate_ids:
        if cid not in seen_candidates:
            seen_candidates.append(cid)

    return RetrieveEvidenceOutput(
        candidate_ids=seen_candidates,
        evidence_ids=list(dict.fromkeys(all_evidence_ids)),
        branch_timings=branch_timings,
        total_elapsed_ms=total_ms,
        degraded_branches=list(set(degraded)),
        mode_used=mode_used,
    )
