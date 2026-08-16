"""Reranker node per §7.4 — deterministic (pinned model) reranking.

Operates only on permitted evidence IDs using the BGE cross-encoder
via ``RerankerService``.
"""

from __future__ import annotations

from typing import Any

import structlog

from documind.rag.state import AgentState, EvidenceCache

logger = structlog.get_logger(__name__)


async def reranker_node(
    state: AgentState,
    *,
    reranker_service: Any,
    evidence_cache: EvidenceCache,
    session_factory: Any,
) -> dict[str, Any]:
    """Rerank authorized evidence and load into the evidence cache.

    Only operates on filtered (permitted) candidate IDs.
    """
    filtered_ids = state.get("filtered_candidate_ids", [])

    if not filtered_ids:
        return {
            "reranked_evidence_ids": [],
            "agent_path": [*state.get("agent_path", []), "reranker:no_evidence"],
        }

    # First, load evidence into the cache.
    from documind.rag.tools.load_evidence import LoadEvidenceInput, load_evidence

    load_input = LoadEvidenceInput(allowed_evidence_ids=filtered_ids)
    async with session_factory() as session:
        await load_evidence(load_input, session, evidence_cache)

    # Then rerank using the cache content.
    from documind.rag.tools.rerank_evidence import RerankEvidenceInput, rerank_evidence

    rerank_input = RerankEvidenceInput(
        query=state["original_question"],
        allowed_evidence_ids=filtered_ids,
    )

    try:
        result = await rerank_evidence(rerank_input, reranker_service, evidence_cache)
        reranked_ids = [item.evidence_id for item in result.ranked_evidence_ids]

        return {
            "reranked_evidence_ids": reranked_ids,
            "agent_path": [
                *state.get("agent_path", []),
                f"reranker:{len(reranked_ids)}_ranked",
            ],
        }
    except Exception:
        logger.exception("reranker_node_error")
        # On reranker failure, use unranked filtered IDs.
        return {
            "reranked_evidence_ids": filtered_ids,
            "degraded_branches": [*state.get("degraded_branches", []), "reranker_unavailable"],
            "agent_path": [*state.get("agent_path", []), "reranker:fallback_unranked"],
        }
