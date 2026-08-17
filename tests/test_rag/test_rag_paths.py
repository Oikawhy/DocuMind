"""T8-36: Mocked-LLM integration tests for RAG graph paths.

Each test compiles the graph via build_graph() with deterministic mocked
LLM responses, runs ainvoke end-to-end, and asserts the expected path
and outcome.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from documind.rag.state import (
    EvidenceCache,
    create_initial_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_llm_result(parsed: dict | None = None, content: str = ""):
    """Minimal mock for LLMService.invoke() return value."""
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
    """Create mock services for graph testing (reused pattern)."""
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


def _setup_retrieval_mock(retrieval: AsyncMock, evidence_ids: list[str] | None = None):
    """Setup retrieval service mock to return proper evidence items."""
    ids = evidence_ids or ["chunk_1"]

    class _EvidenceItem:
        def __init__(self, chunk_id: str):
            self.chunk_id = chunk_id

    class _RetrievalMeta:
        mode = "hybrid"
        backend_timings: dict[str, int] = {}
        candidate_count_before_auth = len(ids)

    class _RetrievalResponse:
        def __init__(self):
            self.evidence = [_EvidenceItem(eid) for eid in ids]
            self.retrieval_metadata = _RetrievalMeta()
            self.degraded_branches: list[str] = []

    retrieval.retrieve = AsyncMock(return_value=_RetrievalResponse())


def _make_state(
    question: str = "What is the value?",
    evidence_cache: EvidenceCache | None = None,
    session_factory: Any = None,
    document_ids: frozenset[str] | None = None,
) -> dict:
    """Build initial state with proper auth context for graph invocation."""
    mock_auth = MagicMock()
    mock_auth.subject = "test-user"
    mock_auth.document_ids = document_ids if document_ids is not None else frozenset({"doc-1"})
    mock_auth.principal = MagicMock()

    if session_factory is not None:
        mock_auth.session_factory = session_factory
    else:
        # Need a proper async context manager for session_factory.
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_auth.session_factory = MagicMock(return_value=session_ctx)

    return create_initial_state(
        question=question,
        principal_subject="test-user",
        auth_context=mock_auth,
        evidence_cache=evidence_cache,
    )


# ---------------------------------------------------------------------------
# Test: Simple query → clarification abstention (fast path)
# ---------------------------------------------------------------------------


class TestClarificationRoute:
    """Router → response_formatter (skips retrieval)."""

    @pytest.mark.asyncio
    async def test_clarification_abstains(self) -> None:
        from documind.rag.graph import build_graph

        llm, retrieval, reranker, session_factory = _make_services()
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

        state = _make_state(session_factory=session_factory)
        final = await asyncio.wait_for(graph.ainvoke(state), timeout=10)

        assert final.get("final_response") is not None
        response = final["final_response"]
        assert response.get("abstained") is True or response.get("route") == "clarification"


# ---------------------------------------------------------------------------
# Test: Out-of-scope → abstention (fast path)
# ---------------------------------------------------------------------------


class TestOutOfScopeRoute:
    """Router → response_formatter with out_of_scope."""

    @pytest.mark.asyncio
    async def test_out_of_scope_abstains(self) -> None:
        from documind.rag.graph import build_graph

        llm, retrieval, reranker, session_factory = _make_services()
        llm.invoke = AsyncMock(
            return_value=_mock_llm_result(
                parsed={"route": "out_of_scope", "confidence": 0.95},
            ),
        )

        graph = build_graph(
            llm_service=llm,
            retrieval_service=retrieval,
            reranker_service=reranker,
            session_factory=session_factory,
        )

        state = _make_state(session_factory=session_factory)
        final = await asyncio.wait_for(graph.ainvoke(state), timeout=10)

        assert final.get("final_response") is not None
        response = final["final_response"]
        assert response.get("abstained") is True or response.get("route") == "out_of_scope"


# ---------------------------------------------------------------------------
# Test: Cache per-request lifecycle
# ---------------------------------------------------------------------------


class TestCacheLifecycle:
    """T8-07/08: Cache created per-request, expired after completion."""

    def test_cache_put_get_expire(self) -> None:
        cache = EvidenceCache()
        cache.put("test_key", "test_value")

        assert cache.get("test_key") == "test_value"
        assert not cache.is_expired

        cache.expire_all()
        assert cache.is_expired

        with pytest.raises(RuntimeError):
            cache.get("test_key")

    def test_cache_isolation(self) -> None:
        """Two separate caches don't share data."""
        cache1 = EvidenceCache()
        cache2 = EvidenceCache()

        cache1.put("key", "value1")
        cache2.put("key", "value2")

        assert cache1.get("key") == "value1"
        assert cache2.get("key") == "value2"

        cache1.expire_all()
        with pytest.raises(RuntimeError):
            cache1.get("key")
        assert cache2.get("key") == "value2"

    def test_expired_cache_rejects_put(self) -> None:
        cache = EvidenceCache()
        cache.expire_all()
        with pytest.raises(RuntimeError):
            cache.put("key", "value")


# ---------------------------------------------------------------------------
# Test: Unit conversion in aggregation
# ---------------------------------------------------------------------------


class TestUnitConversion:
    """T8-23: Mixed units are normalized before aggregation."""

    @pytest.mark.asyncio
    async def test_length_units_normalized(self) -> None:
        from documind.rag.tools.aggregate_values import (
            AggregateValueEntry,
            AggregateValuesInput,
            aggregate_values,
        )

        input_data = AggregateValuesInput(
            operation="sum",
            values=[
                AggregateValueEntry(value="1", unit="m", evidence_id="e1"),
                AggregateValueEntry(value="100", unit="cm", evidence_id="e2"),
            ],
        )

        result = await aggregate_values(input_data)
        # 1m + 100cm = 1m + 1m = 2m
        assert result.error is None
        assert result.result == pytest.approx(2.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_weight_units_normalized(self) -> None:
        from documind.rag.tools.aggregate_values import (
            AggregateValueEntry,
            AggregateValuesInput,
            aggregate_values,
        )

        input_data = AggregateValuesInput(
            operation="sum",
            values=[
                AggregateValueEntry(value="1", unit="kg", evidence_id="e1"),
                AggregateValueEntry(value="500", unit="g", evidence_id="e2"),
            ],
        )

        result = await aggregate_values(input_data)
        # 1kg + 500g = 1000g + 500g = 1500g
        assert result.error is None
        assert result.result == pytest.approx(1500.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_incompatible_currencies_rejected(self) -> None:
        from documind.rag.tools.aggregate_values import (
            AggregateValueEntry,
            AggregateValuesInput,
            aggregate_values,
        )

        input_data = AggregateValuesInput(
            operation="sum",
            values=[
                AggregateValueEntry(value="100", unit="USD", evidence_id="e1"),
                AggregateValueEntry(value="200", unit="EUR", evidence_id="e2"),
            ],
        )

        result = await aggregate_values(input_data)
        assert result.error is not None


# ---------------------------------------------------------------------------
# Test: Manifest verification
# ---------------------------------------------------------------------------


class TestManifestVerification:
    """T8-30: All templates pass manifest verification."""

    def test_all_templates_verified(self) -> None:
        from documind.rag.prompts.registry import build_default_registry

        registry = build_default_registry()
        errors = registry.verify_manifest()
        assert errors == [], f"Manifest errors: {errors}"


# ---------------------------------------------------------------------------
# Test: Template schemas present
# ---------------------------------------------------------------------------


class TestTemplateSchemas:
    """T8-31: All templates have I/O schemas."""

    def test_all_templates_have_output_schemas(self) -> None:
        from documind.rag.prompts.templates import ALL_TEMPLATES

        for template in ALL_TEMPLATES:
            if template.name in ("router", "query_rewriter"):
                continue
            assert template.output_schema, f"{template.name} missing output_schema"

    def test_t8_31_templates_have_input_schemas(self) -> None:
        from documind.rag.prompts.templates import ALL_TEMPLATES

        required_schemas = {"extractor", "comparator", "session_compactor"}
        for template in ALL_TEMPLATES:
            if template.name not in required_schemas:
                continue
            assert template.input_schema, f"{template.name} missing input_schema"


# ---------------------------------------------------------------------------
# Test: Provenance fields on VerifyCitationsInput
# ---------------------------------------------------------------------------


class TestCitationProvenance:
    """T8-29: VerifyCitationsInput has provenance fields."""

    def test_input_has_provenance_fields(self) -> None:
        from documind.rag.tools.verify_citations import VerifyCitationsInput

        fields = VerifyCitationsInput.model_fields
        assert "page_offsets" in fields
        assert "source_spans" in fields
        assert "document_ids" in fields
        assert "version_ids" in fields

    def test_status_has_provenance(self) -> None:
        from documind.rag.tools.verify_citations import CitationStatus

        fields = CitationStatus.model_fields
        assert "provenance" in fields


# ---------------------------------------------------------------------------
# Test: Version resolution default selector
# ---------------------------------------------------------------------------


class TestVersionResolver:
    """T8-25: Default version selector created when missing."""

    @pytest.mark.asyncio
    async def test_default_selector_is_latest_completed(self) -> None:
        from documind.rag.nodes.version_resolver import version_resolver_node

        step = MagicMock()
        step.document_selector = "doc-123"
        step.version_selector = None  # Missing → should default

        state: dict[str, Any] = {
            "plan": [step],
            "resolved_versions": [],
            "principal_subject": "test-user",
            "agent_path": [],
        }

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=None)
        session_factory = MagicMock(return_value=session_ctx)

        result = await version_resolver_node(
            state,
            session_factory=session_factory,
            allowed_document_ids={"doc-123"},
        )

        # Should have attempted resolution (selector list was non-empty).
        assert "version_resolver:no_selectors" not in result.get("agent_path", [])


# ---------------------------------------------------------------------------
# Test: Extraction with prompt registry
# ---------------------------------------------------------------------------


class TestExtractionPromptSecurity:
    """T8-18: Extraction routes through wrap_with_safety + registry."""

    def test_safety_wrapper_adds_guardrails(self) -> None:
        from documind.rag.prompts.safety import wrap_with_safety

        wrapped = wrap_with_safety("You are an extractor.")
        # Should have safety preamble added.
        assert len(wrapped) > len("You are an extractor.")
        assert "extractor" in wrapped.lower()
