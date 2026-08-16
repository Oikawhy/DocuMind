"""Response Formatter node per §7.4 — deterministic final response.

Emits a stable response object: answer or abstention, citations,
confidence, trace ID, agent path, route, policy revisions,
model-route revisions, safe limitation code.

Cannot add new claims, citations, confidence changes, or tool calls.
"""

from __future__ import annotations

from typing import Any

import structlog

from documind.rag.nodes.confidence import calculate_confidence
from documind.rag.state import AgentState

logger = structlog.get_logger(__name__)


async def response_formatter_node(state: AgentState) -> dict[str, Any]:
    """Format the final response from the graph state.

    This is a deterministic node — it cannot modify claims, citations,
    or confidence.  It only structures the output.
    """
    confidence = calculate_confidence(state)
    draft = state.get("draft_answer")
    citation_verification = state.get("citation_verification")
    abstention_reason = state.get("abstention_reason")

    # Build the stable response object.
    response: dict[str, Any] = {
        "trace_id": state.get("trace_id", ""),
        "request_id": state.get("request_id", ""),
        "route": state.get("route_type", "simple_qa"),
        "confidence": confidence,
        "agent_path": state.get("agent_path", []),
        "policy_revisions": {
            "authorization": state.get("authorization_revision", 0),
            "retrieval_policy": state.get("retrieval_policy_revision", 0),
        },
        "model_route_revisions": state.get("model_route_revisions", {}),
    }

    if abstention_reason:
        response["abstained"] = True
        response["abstention_reason"] = abstention_reason
        response["answer"] = None
        response["citations"] = []
        response["limitation_code"] = _derive_limitation_code(abstention_reason)
    elif draft is not None:
        response["abstained"] = False
        response["answer"] = draft.text
        response["citations"] = _format_citations(citation_verification)
        response["limitation_code"] = None
    else:
        response["abstained"] = True
        response["abstention_reason"] = "No answer generated"
        response["answer"] = None
        response["citations"] = []
        response["limitation_code"] = "NO_ANSWER"

    return {
        "confidence": confidence,
        "final_response": response,
        "agent_path": [
            *state.get("agent_path", []),
            f"formatter:confidence={confidence},abstained={response.get('abstained', True)}",
        ],
    }


def _format_citations(citation_verification: Any | None) -> list[dict[str, Any]]:
    """Format verified citations for the response."""
    if citation_verification is None:
        return []

    return [
        {
            "citation_id": c.citation_id,
            "claim_id": c.claim_id,
            "document_id": c.document_id,
            "version_id": c.version_id,
            "version_number": c.version_number,
            "chunk_id": c.chunk_id,
            "page_start": c.page_start,
            "page_end": c.page_end,
            "section_path": c.section_path,
            "excerpt": c.excerpt,
        }
        for c in citation_verification.verified_citations
    ]


def _derive_limitation_code(reason: str) -> str:
    """Derive a safe limitation code from the abstention reason."""
    reason_lower = reason.lower()
    if "insufficient" in reason_lower or "no evidence" in reason_lower:
        return "INSUFFICIENT_EVIDENCE"
    if "maximum" in reason_lower or "retry" in reason_lower or "loop" in reason_lower:
        return "LOOP_LIMIT"
    if "citation" in reason_lower or "tombstone" in reason_lower:
        return "CITATION_INVALID"
    if "route" in reason_lower or "unavailable" in reason_lower:
        return "ROUTE_UNAVAILABLE"
    if "ambig" in reason_lower or "clarif" in reason_lower:
        return "AMBIGUOUS"
    if "auth" in reason_lower:
        return "AUTH_CHANGE"
    return "GENERAL_ABSTENTION"
