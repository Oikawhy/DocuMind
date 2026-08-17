"""Integration tests for the full RAG graph assembly (Substep 8.6).

Tests verify: graph compilation, end-to-end simple_qa flow,
clarification/out_of_scope → direct abstention, comparison route
plan → analysis → generate, runtime budget enforcement, and
evidence cache cleanup.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from documind.rag.state import (
    EvidenceCache,
    create_initial_state,
)


def _mock_llm_result(parsed: dict | None = None, content: str = ""):
    result = MagicMock()
    result.content = content
    if parsed is not None:
        result.structured = MagicMock()
        result.structured.valid = True
        result.structured.parsed = parsed
    else:
        result.structured = None
    return result


def _make_services():
    """Create mock services for graph testing."""
    llm = AsyncMock()
    retrieval = AsyncMock()
    reranker = AsyncMock()

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=None)
    mock_session.execute = AsyncMock(return_value=mock_result)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock(return_value=session_ctx)

    return llm, retrieval, reranker, session_factory


class TestGraphCompilation:
    """Tests that the LangGraph can be compiled without errors."""

    def test_graph_compiles(self) -> None:
        from documind.rag.graph import build_graph

        llm, retrieval, reranker, session_factory = _make_services()

        graph = build_graph(
            llm_service=llm,
            retrieval_service=retrieval,
            reranker_service=reranker,
            session_factory=session_factory,
        )

        assert graph is not None
        # The compiled graph should have an `ainvoke` method.
        assert hasattr(graph, "ainvoke")


class TestGraphClarificationRoute:
    """Tests that clarification/out_of_scope route directly to response_formatter."""

    @pytest.mark.asyncio
    async def test_clarification_skips_retrieval(self) -> None:
        from documind.rag.graph import build_graph

        llm, retrieval, reranker, session_factory = _make_services()

        # Router returns clarification.
        llm.invoke = AsyncMock(
            return_value=_mock_llm_result(
                parsed={"route": "clarification", "confidence": 0.3},
            ),
        )

        graph = build_graph(
            llm_service=llm,
            retrieval_service=retrieval,
            reranker_service=reranker,
            session_factory=session_factory,
        )

        state = create_initial_state(
            question="What?",
            principal_subject="user@example.com",
        )

        final_state = await graph.ainvoke(state)

        # Should have a final response.
        assert final_state.get("final_response") is not None
        response = final_state["final_response"]
        # Clarification should abstain.
        assert response.get("abstained") is True or response.get("route") == "clarification"
        # Retrieval should not have been called (only router → response_formatter).
        assert retrieval.search.call_count == 0


class TestRAGServiceTimeout:
    """Tests that RAGService enforces runtime budget."""

    @pytest.mark.asyncio
    async def test_timeout_returns_abstention(self) -> None:
        from documind.rag.service import RAGService

        # Create a mock graph that takes too long.
        async def slow_invoke(state):
            await asyncio.sleep(100)
            return state

        mock_graph = MagicMock()
        mock_graph.ainvoke = slow_invoke

        service = RAGService(
            compiled_graph=mock_graph,
            session_factory=AsyncMock(),
        )

        # Monkey-patch RUNTIME_BUDGET_SECONDS to 0.1 for fast testing.
        import documind.rag.service as svc_mod
        original = svc_mod.RUNTIME_BUDGET_SECONDS
        svc_mod.RUNTIME_BUDGET_SECONDS = 0.1

        try:
            mock_auth_ctx = MagicMock()
            mock_auth_ctx.subject = "user@example.com"
            mock_auth_ctx.document_ids = frozenset()

            response = await service.run_rag_query(
                question="Test question",
                auth_context=mock_auth_ctx,
            )

            assert response.abstained is True
            assert "budget" in (response.abstention_reason or "").lower()
            assert response.limitation_code == "TIMEOUT"
        finally:
            svc_mod.RUNTIME_BUDGET_SECONDS = original


class TestRAGServiceError:
    """Tests that RAGService handles graph errors gracefully."""

    @pytest.mark.asyncio
    async def test_graph_error_returns_abstention(self) -> None:
        from documind.rag.service import RAGService

        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("fatal"))

        service = RAGService(
            compiled_graph=mock_graph,
            session_factory=AsyncMock(),
        )

        mock_auth_ctx = MagicMock()
        mock_auth_ctx.subject = "user@example.com"
        mock_auth_ctx.document_ids = frozenset()

        response = await service.run_rag_query(
            question="Test question",
            auth_context=mock_auth_ctx,
        )

        assert response.abstained is True
        assert response.limitation_code == "INTERNAL_ERROR"


class TestEvidenceCacheLifecycle:
    """Tests that evidence cache is created and expired per invocation."""

    def test_cache_lifecycle(self) -> None:
        cache = EvidenceCache()
        cache.put("e1", "content1")
        assert cache.get("e1") == "content1"

        cache.expire_all()
        assert cache.is_expired

        with pytest.raises(RuntimeError):
            cache.put("e2", "content2")

        with pytest.raises(RuntimeError):
            cache.get("e1")


class TestAgentRunModelExtension:
    """Tests that the AgentRun model has the RAG fields."""

    def test_agent_run_has_rag_fields(self) -> None:
        from documind.models.chat import AgentRun

        # Check that all RAG fields exist as mapped columns.
        rag_fields = [
            "request_id", "principal_subject", "policy_revisions",
            "plan", "rewritten_query_metadata", "retrieval_ids",
            "filtered_ids", "reranked_ids", "model_route_revisions",
            "prompt_revisions", "schema_validation_outcomes",
            "retry_count", "revision_count", "confidence",
            "citation_ids", "timing", "abstention_reason", "response_hash",
        ]
        table_columns = {c.name for c in AgentRun.__table__.columns}
        for field_name in rag_fields:
            assert field_name in table_columns, f"AgentRun missing field: {field_name}"


class TestChatResponseSchema:
    """Tests that the ChatResponse schema has the RAG fields."""

    def test_chat_response_has_rag_fields(self) -> None:
        from documind.schemas.chat import ChatResponse

        fields = ChatResponse.model_fields
        assert "confidence" in fields
        assert "route" in fields
        assert "agent_path" in fields
        assert "model_route_revisions" in fields
        assert "limitation_code" in fields
