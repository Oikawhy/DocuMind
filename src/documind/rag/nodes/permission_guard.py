"""Permission Guard node per §7.4 — deterministic canonical recheck.

Distinct from the Task 7 ``PermissionGuard`` helper — this is the
graph node that receives candidate IDs, delegates to the full
``AuthorizationService`` via ``AuthorizationContext``, and returns
allowed evidence IDs with audit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from documind.rag.state import AgentState

if TYPE_CHECKING:
    from documind.domain.authorization_context import AuthorizationContext

logger = structlog.get_logger(__name__)


async def permission_guard_node(
    state: AgentState,
    *,
    auth_context: AuthorizationContext | None = None,
    session_factory: Any = None,
    audit_service: Any | None = None,
) -> dict[str, Any]:
    """Canonical authorization recheck on every candidate.

    Uses ``AuthorizationContext`` for full §4.2 authorization decisions
    (principal, labels, lifecycle, holds, tombstones, policy) instead of
    static document-ID set checks.

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

    # T8-06: Use AuthorizationContext for live auth checks.
    sf = auth_context.session_factory if auth_context else session_factory
    async with sf() as session:
        result = await permission_guard(
            input_data,
            session,
            auth_context=auth_context,
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
