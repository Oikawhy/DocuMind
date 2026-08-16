"""RAGService — top-level service interface per §7.

Wraps graph invocation with evidence cache lifecycle, runtime budget,
and typed response conversion.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from documind.rag.state import RUNTIME_BUDGET_SECONDS, AgentState, create_initial_state

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RAGResponse:
    """Typed response from the RAG pipeline."""

    answer: str | None = None
    abstained: bool = False
    abstention_reason: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    route: str = "simple_qa"
    agent_path: list[str] = field(default_factory=list)
    policy_revisions: dict[str, int] = field(default_factory=dict)
    model_route_revisions: dict[str, int] = field(default_factory=dict)
    trace_id: str = ""
    limitation_code: str | None = None


class RAGService:
    """Top-level RAG service — builds graph and runs queries.

    Each ``run_rag_query`` call:
    1. Constructs a fresh ``AgentState``
    2. Invokes the compiled graph with runtime budget
    3. Manages ``EvidenceCache`` lifecycle
    4. Converts graph output to typed ``RAGResponse``
    """

    def __init__(
        self,
        *,
        compiled_graph: Any,
        session_factory: Any,
        llm_service: Any | None = None,
        audit_service: Any | None = None,
    ) -> None:
        self._graph = compiled_graph
        self._session_factory = session_factory
        self._llm_service = llm_service
        self._audit_service = audit_service

    async def run_rag_query(
        self,
        question: str,
        principal_subject: str,
        *,
        session_id: str | None = None,
        session_summary: str | None = None,
        chat_history: list[dict[str, str]] | None = None,
        locale: str = "en",
        authorization_revision: int = 0,
        retrieval_policy_revision: int = 0,
        model_route_revisions: dict[str, int] | None = None,
        trace_id: str | None = None,
    ) -> RAGResponse:
        """Execute a single RAG query with runtime budget enforcement."""
        initial_state = create_initial_state(
            question=question,
            principal_subject=principal_subject,
            session_id=session_id,
            session_summary=session_summary,
            chat_history=chat_history,
            locale=locale,
            authorization_revision=authorization_revision,
            retrieval_policy_revision=retrieval_policy_revision,
            model_route_revisions=model_route_revisions,
            trace_id=trace_id,
        )

        try:
            # Invoke graph with runtime budget.
            final_state = await asyncio.wait_for(
                self._graph.ainvoke(initial_state),
                timeout=RUNTIME_BUDGET_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "rag_runtime_budget_exceeded",
                budget_seconds=RUNTIME_BUDGET_SECONDS,
                trace_id=initial_state["trace_id"],
            )
            return RAGResponse(
                abstained=True,
                abstention_reason=f"Runtime budget exceeded ({RUNTIME_BUDGET_SECONDS}s)",
                trace_id=initial_state["trace_id"],
                limitation_code="TIMEOUT",
            )
        except Exception as exc:
            logger.exception("rag_graph_error")
            return RAGResponse(
                abstained=True,
                abstention_reason=f"Graph execution error: {type(exc).__name__}",
                trace_id=initial_state["trace_id"],
                limitation_code="INTERNAL_ERROR",
            )

        return _state_to_response(final_state)


def _state_to_response(state: AgentState) -> RAGResponse:
    """Convert final graph state to a typed RAGResponse."""
    final = state.get("final_response")

    if final is not None:
        return RAGResponse(
            answer=final.get("answer"),
            abstained=final.get("abstained", False),
            abstention_reason=final.get("abstention_reason"),
            citations=final.get("citations", []),
            confidence=final.get("confidence", "low"),
            route=final.get("route", "simple_qa"),
            agent_path=final.get("agent_path", []),
            policy_revisions=final.get("policy_revisions", {}),
            model_route_revisions=final.get("model_route_revisions", {}),
            trace_id=final.get("trace_id", state.get("trace_id", "")),
            limitation_code=final.get("limitation_code"),
        )

    # Fallback if response_formatter didn't run.
    return RAGResponse(
        abstained=True,
        abstention_reason=state.get("abstention_reason", "Graph did not produce a response"),
        trace_id=state.get("trace_id", ""),
        limitation_code="NO_RESPONSE",
    )
