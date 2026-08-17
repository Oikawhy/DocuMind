"""Version Resolver node per §7.4 — deterministic version resolution.

Resolves explicit versions, latest completed, date ranges, and comparison
selectors within the authorized document set.  Produces safe non-disclosing
results for missing, inaccessible, failed, or erased versions.
"""

from __future__ import annotations

from typing import Any

import structlog

from documind.rag.state import AgentState, VersionRef

logger = structlog.get_logger(__name__)


async def version_resolver_node(
    state: AgentState,
    *,
    session_factory: Any,
    allowed_document_ids: set[str],
) -> dict[str, Any]:
    """Resolve version selectors from the plan.

    Uses the ``resolve_versions`` tool for each plan step that needs
    version resolution.
    """
    from documind.rag.tools.resolve_versions import (
        ResolveVersionsInput,
        VersionSelector,
        resolve_versions,
    )

    plan = state.get("plan", [])
    resolved: list[VersionRef] = list(state.get("resolved_versions", []))

    # Collect version selectors from the plan.
    selectors: list[VersionSelector] = []
    for step in plan:
        if step.document_selector:
            # T8-25: Default to latest_completed when version_selector absent.
            version_sel = step.version_selector or "latest_completed"
            selectors.append(
                VersionSelector(
                    document_id=step.document_selector,
                    selector=version_sel,
                )
            )

    if not selectors:
        return {
            "resolved_versions": resolved,
            "agent_path": [*state.get("agent_path", []), "version_resolver:no_selectors"],
        }

    input_data = ResolveVersionsInput(
        selectors=selectors,
        principal_subject=state["principal_subject"],
    )

    async with session_factory() as session:
        result = await resolve_versions(input_data, session, allowed_document_ids)

    for ref_data in result.resolved:
        resolved.append(
            VersionRef(
                document_id=ref_data.get("document_id", ""),
                version_id=ref_data.get("version_id", ""),
                version_number=ref_data.get("version_number", 0),
                selector_used=ref_data.get("selector_used", ""),
                status=ref_data.get("status", "missing"),
            )
        )

    return {
        "resolved_versions": resolved,
        "agent_path": [
            *state.get("agent_path", []),
            f"version_resolver:{len(resolved)}_resolved",
        ],
    }
