"""LangGraph state graph assembly per §7.

Builds the complete RAG agent graph with conditional edges and loop guards.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from documind.rag.state import AgentState


def build_graph(
    *,
    llm_service: Any,
    retrieval_service: Any,
    reranker_service: Any,
    session_factory: Any,
    audit_service: Any | None = None,
    prompt_registry: Any | None = None,
    template_loader: Any | None = None,
) -> Any:
    """Build and compile the LangGraph RAG agent.

    Returns a compiled ``StateGraph`` ready for invocation.

    The graph is stateless — per-request data (``AuthorizationContext``,
    ``EvidenceCache``, document IDs) is threaded through ``AgentState``,
    not captured in closures.

    Conditional edges per §7 Mermaid:
    - Router → {Rewrite, Planner, Abstain}
    - Grade → {QueryRewriter (max 3 retrieval), Planner (max 2 expansion), Generate}
    - Hallucination → {Generate (max 2 revision), Verify}
    - Verify → {Format, Abstain}
    """
    from documind.rag.nodes.aggregator import aggregator_node
    from documind.rag.nodes.citation_verifier import citation_verifier_node
    from documind.rag.nodes.comparator import comparator_node
    from documind.rag.nodes.extractor import extractor_node
    from documind.rag.nodes.generator import generator_node
    from documind.rag.nodes.hallucination_grader import hallucination_grader_node
    from documind.rag.nodes.permission_guard import permission_guard_node
    from documind.rag.nodes.planner import planner_node
    from documind.rag.nodes.query_rewriter import query_rewriter_node
    from documind.rag.nodes.relevance_grader import relevance_grader_node
    from documind.rag.nodes.reranker import reranker_node
    from documind.rag.nodes.response_formatter import response_formatter_node
    from documind.rag.nodes.retrieval_orchestrator import retrieval_orchestrator_node
    from documind.rag.nodes.router import router_node
    from documind.rag.nodes.version_resolver import version_resolver_node
    from documind.rag.state import EvidenceCache

    # -- Node wrapper functions (bind dependencies via closures) --
    # NOTE: auth_context, evidence_cache, and document_ids are read from
    # state per-request, not captured at compile time.

    async def _router(state: AgentState) -> dict[str, Any]:
        return await router_node(
            state, llm_service=llm_service, prompt_registry=prompt_registry,
        )

    async def _planner(state: AgentState) -> dict[str, Any]:
        return await planner_node(
            state, llm_service=llm_service, prompt_registry=prompt_registry,
        )

    async def _query_rewriter(state: AgentState) -> dict[str, Any]:
        return await query_rewriter_node(
            state, llm_service=llm_service, prompt_registry=prompt_registry,
        )

    # T8-04: Read principal from state["auth_context"] instead of passing None.
    async def _retrieval(state: AgentState) -> dict[str, Any]:
        auth_ctx = state.get("auth_context")
        principal = auth_ctx.principal if auth_ctx else None
        return await retrieval_orchestrator_node(
            state, retrieval_service=retrieval_service, principal=principal,
        )

    # T8-05/T8-06: Read auth_context from state for live authorization.
    async def _permission_guard(state: AgentState) -> dict[str, Any]:
        auth_ctx = state.get("auth_context")
        return await permission_guard_node(
            state,
            auth_context=auth_ctx,
            session_factory=session_factory,
            audit_service=audit_service,
        )

    async def _reranker(state: AgentState) -> dict[str, Any]:
        return await reranker_node(
            state, reranker_service=reranker_service,
            evidence_cache=state.get("evidence_cache"),
            session_factory=session_factory,
        )

    async def _relevance_grader(state: AgentState) -> dict[str, Any]:
        return await relevance_grader_node(
            state, llm_service=llm_service,
            evidence_cache=state.get("evidence_cache"),
            prompt_registry=prompt_registry,
        )

    async def _version_resolver(state: AgentState) -> dict[str, Any]:
        auth_ctx = state.get("auth_context")
        doc_ids = auth_ctx.document_ids if auth_ctx else set()
        return await version_resolver_node(
            state, session_factory=session_factory,
            allowed_document_ids=doc_ids,
        )

    async def _extractor(state: AgentState) -> dict[str, Any]:
        return await extractor_node(
            state, llm_service=llm_service,
            evidence_cache=state.get("evidence_cache"),
            template_loader=template_loader, prompt_registry=prompt_registry,
        )

    async def _comparator(state: AgentState) -> dict[str, Any]:
        return await comparator_node(
            state, llm_service=llm_service,
            evidence_cache=state.get("evidence_cache"),
            prompt_registry=prompt_registry,
        )

    async def _aggregator(state: AgentState) -> dict[str, Any]:
        return await aggregator_node(
            state, evidence_cache=state.get("evidence_cache"),
        )

    async def _generator(state: AgentState) -> dict[str, Any]:
        return await generator_node(
            state, llm_service=llm_service,
            evidence_cache=state.get("evidence_cache"),
            prompt_registry=prompt_registry,
        )

    async def _hallucination_grader(state: AgentState) -> dict[str, Any]:
        return await hallucination_grader_node(
            state, llm_service=llm_service,
            evidence_cache=state.get("evidence_cache"),
            prompt_registry=prompt_registry,
        )

    # T8-28: Read auth_context from state for citation doc-ID check.
    async def _citation_verifier(state: AgentState) -> dict[str, Any]:
        auth_ctx = state.get("auth_context")
        return await citation_verifier_node(
            state,
            auth_context=auth_ctx,
            session_factory=session_factory,
        )

    async def _response_formatter(state: AgentState) -> dict[str, Any]:
        return await response_formatter_node(state)

    # -- Build the graph --

    graph = StateGraph(AgentState)

    # Add nodes.
    graph.add_node("router", _router)
    graph.add_node("planner", _planner)
    graph.add_node("query_rewriter", _query_rewriter)
    graph.add_node("retrieval", _retrieval)
    graph.add_node("permission_guard", _permission_guard)
    graph.add_node("reranker", _reranker)
    graph.add_node("relevance_grader", _relevance_grader)
    graph.add_node("version_resolver", _version_resolver)
    graph.add_node("extractor", _extractor)
    graph.add_node("comparator", _comparator)
    graph.add_node("aggregator", _aggregator)
    graph.add_node("generator", _generator)
    graph.add_node("hallucination_grader", _hallucination_grader)
    graph.add_node("citation_verifier", _citation_verifier)
    graph.add_node("response_formatter", _response_formatter)

    # Entry point.
    graph.set_entry_point("router")

    # Conditional edge: Router →
    #   {query_rewriter, planner, response_formatter (clarification/out_of_scope)}
    def route_after_router(state: AgentState) -> str:
        route = state.get("route_type", "simple_qa")
        if route in {"clarification", "out_of_scope"}:
            return "response_formatter"
        if route in {"comparison", "aggregation", "extraction"}:
            return "planner"
        return "query_rewriter"

    graph.add_conditional_edges("router", route_after_router, {
        "response_formatter": "response_formatter",
        "planner": "planner",
        "query_rewriter": "query_rewriter",
    })

    # Planner → version_resolver → query_rewriter
    graph.add_edge("planner", "version_resolver")
    graph.add_edge("version_resolver", "query_rewriter")

    # Query Rewriter → Retrieval → Permission Guard → Reranker → Relevance Grader
    graph.add_edge("query_rewriter", "retrieval")
    graph.add_edge("retrieval", "permission_guard")
    graph.add_edge("permission_guard", "reranker")
    graph.add_edge("reranker", "relevance_grader")

    # T8-09: Conditional edge: Relevance Grader →
    #   rewrite → query_rewriter (was: retrieval — skipped rewrite)
    #   targeted_expansion → planner (was: retrieval — skipped plan)
    #   abstain → response_formatter
    #   answer → {extractor, comparator, aggregator, generator}
    def route_after_grader(state: AgentState) -> str:
        kind = state.get("relevance_request_kind", "abstain")
        if kind == "rewrite":
            return "query_rewriter"
        if kind == "targeted_expansion":
            return "planner"
        if kind == "abstain":
            return "response_formatter"
        # "answer" — proceed to analysis/generation based on route type.
        route = state.get("route_type", "simple_qa")
        if route == "extraction":
            return "extractor"
        if route == "comparison":
            return "comparator"
        if route == "aggregation":
            return "aggregator"
        return "generator"

    graph.add_conditional_edges("relevance_grader", route_after_grader, {
        "query_rewriter": "query_rewriter",
        "planner": "planner",
        "response_formatter": "response_formatter",
        "extractor": "extractor",
        "comparator": "comparator",
        "aggregator": "aggregator",
        "generator": "generator",
    })

    # T8-16: Extractor routes conditionally —
    #   comparison route → comparator (schema-aware extraction before comparison)
    #   otherwise → generator
    def route_after_extractor(state: AgentState) -> str:
        if state.get("route_type") == "comparison":
            return "comparator"
        return "generator"

    graph.add_conditional_edges("extractor", route_after_extractor, {
        "comparator": "comparator",
        "generator": "generator",
    })

    # Comparator / Aggregator → Generator
    graph.add_edge("comparator", "generator")
    graph.add_edge("aggregator", "generator")

    # Generator → Hallucination Grader
    graph.add_edge("generator", "hallucination_grader")

    # T8-11: Conditional edge: Hallucination Grader →
    #   revision needed AND under cap → generator
    #   otherwise → citation_verifier or response_formatter (abstention)
    #   Fixed: use strict < instead of <= to prevent off-by-one
    def route_after_hallucination(state: AgentState) -> str:
        grade = state.get("hallucination_grade")
        if grade is not None and grade.needs_revision:
            revisions = state.get("generation_revisions", 0)
            from documind.rag.state import MAX_GENERATION_REVISIONS
            if revisions < MAX_GENERATION_REVISIONS:
                return "generator"
        # Check for abstention.
        if state.get("abstention_reason"):
            return "response_formatter"
        return "citation_verifier"

    graph.add_conditional_edges("hallucination_grader", route_after_hallucination, {
        "generator": "generator",
        "citation_verifier": "citation_verifier",
        "response_formatter": "response_formatter",
    })

    # Conditional edge: Citation Verifier → response_formatter
    # T8-12: The citation_verifier_node now sets abstention_reason when
    # all_valid is False, so the formatter will produce an abstention.
    def route_after_citation(state: AgentState) -> str:
        return "response_formatter"

    graph.add_conditional_edges("citation_verifier", route_after_citation, {
        "response_formatter": "response_formatter",
    })

    # Response Formatter → END
    graph.add_edge("response_formatter", END)

    return graph.compile()
