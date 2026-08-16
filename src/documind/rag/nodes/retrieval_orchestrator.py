"""Retrieval Orchestrator node per §7.4 — deterministic retrieval dispatch.

Calls the retrieval service per §6, saves branch result IDs and timings.
Only uses administrator-enabled modes and server-built filters.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from documind.rag.state import MAX_RETRIEVAL_ATTEMPTS, AgentState

logger = structlog.get_logger(__name__)


async def retrieval_orchestrator_node(
    state: AgentState,
    *,
    retrieval_service: Any,
    principal: Any,
) -> dict[str, Any]:
    """Execute retrieval using the deterministic retrieval service.

    Increments ``retrieval_attempts`` and records branch timings.
    Respects ``MAX_RETRIEVAL_ATTEMPTS`` — returns abstention if exceeded.
    """
    current_attempts = state.get("retrieval_attempts", 0)

    if current_attempts >= MAX_RETRIEVAL_ATTEMPTS:
        return {
            "abstention_reason": f"Maximum retrieval attempts ({MAX_RETRIEVAL_ATTEMPTS}) exceeded",
            "agent_path": [*state.get("agent_path", []), "retrieval:max_attempts"],
        }

    queries = state.get("rewritten_queries", [state["original_question"]])
    if not queries:
        queries = [state["original_question"]]

    from documind.rag.tools.retrieve_evidence import (
        RetrieveEvidenceInput,
        retrieve_evidence,
    )

    input_data = RetrieveEvidenceInput(
        queries=queries[:3],
        principal_subject=state["principal_subject"],
        locale=state.get("locale", "en"),
    )

    start = time.monotonic()
    try:
        result = await retrieve_evidence(input_data, retrieval_service, principal)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        return {
            "candidate_ids": result.candidate_ids,
            "retrieval_attempts": current_attempts + 1,
            "degraded_branches": [
                *state.get("degraded_branches", []),
                *result.degraded_branches,
            ],
            "agent_path": [
                *state.get("agent_path", []),
                f"retrieval:attempt_{current_attempts + 1}:{elapsed_ms}ms",
            ],
        }
    except Exception:
        logger.exception("retrieval_orchestrator_error")
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "candidate_ids": [],
            "retrieval_attempts": current_attempts + 1,
            "degraded_branches": [*state.get("degraded_branches", []), "retrieval_error"],
            "agent_path": [
                *state.get("agent_path", []),
                f"retrieval:error:{elapsed_ms}ms",
            ],
        }
