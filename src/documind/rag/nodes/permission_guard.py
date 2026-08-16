"""Permission Guard node per §7.4 — deterministic canonical recheck.

Distinct from the Task 7 ``PermissionGuard`` helper — this is the
graph node that receives candidate IDs, rereads canonical metadata
from PostgreSQL, and returns allowed evidence IDs with audit.
"""

from __future__ import annotations

from typing import Any

import structlog

from documind.rag.state import AgentState

logger = structlog.get_logger(__name__)


async def permission_guard_node(
    state: AgentState,
    *,
    session_factory: Any,
    allowed_document_ids: set[str],
    audit_service: Any | None = None,
) -> dict[str, Any]:
    """Canonical PostgreSQL recheck on every candidate.

    Never places inaccessible titles, excerpts, IDs, or labels in AgentState.
    """
    from documind.rag.tools.permission_guard import PermissionGuardInput, permission_guard

    candidate_ids = state.get("candidate_ids", [])

    if not candidate_ids:
        return {
            "filtered_candidate_ids": [],
            "filtered_out_count": 0,
            "agent_path": [*state.get("agent_path", []), "permission_guard:no_candidates"],
        }

    input_data = PermissionGuardInput(
        candidate_ids=candidate_ids,
        principal_subject=state["principal_subject"],
    )

    async with session_factory() as session:
        result = await permission_guard(
            input_data,
            session,
            allowed_document_ids,
            audit_service=audit_service,
            trace_id=state.get("trace_id"),
        )

    return {
        "filtered_candidate_ids": result.allowed_ids,
        "filtered_out_count": result.filtered_count,
        "agent_path": [
            *state.get("agent_path", []),
            f"permission_guard:allowed={len(result.allowed_ids)},filtered={result.filtered_count}",
        ],
    }
