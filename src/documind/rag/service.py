"""RAGService — top-level service interface per §7.

Wraps graph invocation with evidence cache lifecycle, runtime budget,
and typed response conversion.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from documind.domain.authorization_context import AuthorizationContext

from documind.rag.state import RUNTIME_BUDGET_SECONDS, AgentState, create_initial_state

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RAGResponse:
    """Typed response from the RAG pipeline."""

    answer: str | None = None
    abstained: bool = False
    abstention_reason: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    route: str = "simple_qa"
    agent_path: list[str] = field(default_factory=list)
    plan_steps: list[dict[str, Any]] = field(default_factory=list)
    policy_revisions: dict[str, int] = field(default_factory=dict)
    model_route_revisions: dict[str, int] = field(default_factory=dict)
    prompt_revisions: list[dict[str, Any]] = field(default_factory=list)
    retry_count: int = 0
    timing_ms: int = 0
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
        self._prompt_registry: Any | None = None  # Set after build_graph

    def set_prompt_registry(self, registry: Any) -> None:
        """Attach prompt registry for invocation log persistence (T8-32)."""
        self._prompt_registry = registry

    async def run_rag_query(
        self,
        question: str,
        auth_context: AuthorizationContext,
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
        from documind.rag.state import EvidenceCache

        start_wall = time.monotonic()
        # T8-07: Per-request cache — never shared across requests.
        cache = EvidenceCache()
        initial_state = create_initial_state(
            question=question,
            principal_subject=auth_context.subject,
            auth_context=auth_context,
            evidence_cache=cache,
            session_id=session_id,
            session_summary=session_summary,
            chat_history=chat_history,
            locale=locale,
            authorization_revision=authorization_revision,
            retrieval_policy_revision=retrieval_policy_revision,
            model_route_revisions=model_route_revisions,
            trace_id=trace_id,
        )

        # T8-33: Trace graph entry.
        await self._write_trace(
            "graph_entry", auth_context.subject,
            initial_state["trace_id"],
            {"question_length": len(question), "route": "pending"},
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
            # T8-33: Trace timeout.
            await self._write_trace(
                "graph_timeout", auth_context.subject,
                initial_state["trace_id"],
                {"budget_seconds": RUNTIME_BUDGET_SECONDS},
            )
            elapsed_ms = int((time.monotonic() - start_wall) * 1000)
            return RAGResponse(
                abstained=True,
                abstention_reason=f"Runtime budget exceeded ({RUNTIME_BUDGET_SECONDS}s)",
                trace_id=initial_state["trace_id"],
                limitation_code="TIMEOUT",
                timing_ms=elapsed_ms,
            )
        except Exception as exc:
            logger.exception("rag_graph_error")
            # T8-33: Trace error.
            await self._write_trace(
                "graph_error", auth_context.subject,
                initial_state["trace_id"],
                {"error_type": type(exc).__name__},
            )
            elapsed_ms = int((time.monotonic() - start_wall) * 1000)
            return RAGResponse(
                abstained=True,
                abstention_reason=f"Graph execution error: {type(exc).__name__}",
                trace_id=initial_state["trace_id"],
                limitation_code="INTERNAL_ERROR",
                timing_ms=elapsed_ms,
            )
        finally:
            # T8-08: Always expire cache — even on timeout/exception.
            cache.expire_all()

        # T8-33: Trace graph completion.
        await self._write_trace(
            "graph_complete", auth_context.subject,
            initial_state["trace_id"],
            {"abstained": final_state.get("abstention_reason") is not None},
        )

        elapsed_ms = int((time.monotonic() - start_wall) * 1000)
        response = _state_to_response(final_state, timing_ms=elapsed_ms)

        # T8-32: Attach prompt invocation log.
        if self._prompt_registry is not None:
            response = RAGResponse(
                answer=response.answer,
                abstained=response.abstained,
                abstention_reason=response.abstention_reason,
                citations=response.citations,
                claims=response.claims,
                confidence=response.confidence,
                route=response.route,
                agent_path=response.agent_path,
                plan_steps=response.plan_steps,
                policy_revisions=response.policy_revisions,
                model_route_revisions=response.model_route_revisions,
                prompt_revisions=self._prompt_registry.invocation_log,
                retry_count=response.retry_count,
                timing_ms=response.timing_ms,
                trace_id=response.trace_id,
                limitation_code=response.limitation_code,
            )

        return response

    async def _write_trace(
        self,
        event_type: str,
        principal_subject: str,
        trace_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """T8-33: Write a content-free audit trace event."""
        if self._audit_service is None:
            return
        try:
            from documind.rag.tools.write_trace import WriteTraceInput, write_trace

            input_data = WriteTraceInput(
                event_type=event_type,
                principal_subject=principal_subject,
                trace_id=trace_id,
                metadata=metadata or {},
            )
            await write_trace(input_data, self._audit_service)
        except Exception:
            logger.warning("write_trace_failed", event_type=event_type, exc_info=True)


def _state_to_response(state: AgentState, *, timing_ms: int = 0) -> RAGResponse:
    """Convert final graph state to a typed RAGResponse."""
    final = state.get("final_response")

    if final is not None:
        return RAGResponse(
            answer=final.get("answer"),
            abstained=final.get("abstained", False),
            abstention_reason=final.get("abstention_reason"),
            citations=final.get("citations", []),
            claims=final.get("claims", []),
            confidence=final.get("confidence", "low"),
            route=final.get("route", "simple_qa"),
            agent_path=final.get("agent_path", []),
            plan_steps=final.get("plan_steps", []),
            policy_revisions=final.get("policy_revisions", {}),
            model_route_revisions=final.get("model_route_revisions", {}),
            prompt_revisions=final.get("prompt_revisions", []),
            retry_count=final.get("retry_count", 0),
            timing_ms=timing_ms,
            trace_id=final.get("trace_id", state.get("trace_id", "")),
            limitation_code=final.get("limitation_code"),
        )

    # Fallback if response_formatter didn't run.
    return RAGResponse(
        abstained=True,
        abstention_reason=state.get("abstention_reason", "Graph did not produce a response"),
        trace_id=state.get("trace_id", ""),
        limitation_code="NO_RESPONSE",
        timing_ms=timing_ms,
    )
