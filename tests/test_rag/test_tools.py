"""Tests for RAG agent state, evidence cache, and typed tools (Substep 8.1)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from documind.rag.state import (
    MAX_EVIDENCE_CHUNKS,
    MAX_GENERATION_REVISIONS,
    MAX_PLAN_STEPS,
    MAX_QUERY_CHARS,
    MAX_RETRIEVAL_ATTEMPTS,
    MAX_REWRITTEN_QUERIES,
    MAX_TARGETED_EXPANSIONS,
    RUNTIME_BUDGET_SECONDS,
    AggregationResult,
    Citation,
    Claim,
    DraftAnswer,
    EvidenceCache,
    PlanStep,
    QueryHints,
    RelevanceGrade,
    VersionRef,
    create_initial_state,
)

# ===================================================================
# AgentState creation and invariants
# ===================================================================


class TestAgentState:
    """Tests for AgentState TypedDict and creation."""

    def test_create_initial_state_defaults(self) -> None:
        state = create_initial_state(
            question="What is the revenue?",
            principal_subject="user@example.com",
        )
        assert state["original_question"] == "What is the revenue?"
        assert state["principal_subject"] == "user@example.com"
        assert state["locale"] == "en"
        assert state["retrieval_attempts"] == 0
        assert state["targeted_expansions"] == 0
        assert state["generation_revisions"] == 0
        assert state["confidence"] == "low"
        assert state["abstention_reason"] is None
        assert state["agent_path"] == []
        assert state["plan"] == []
        assert state["rewritten_queries"] == []
        assert state["draft_answer"] is None
        assert state["final_response"] is None

    def test_create_initial_state_with_session(self) -> None:
        state = create_initial_state(
            question="Tell me more",
            principal_subject="user@example.com",
            session_id="sess-123",
            session_summary="Previous conversation about revenue",
            chat_history=[{"role": "user", "content": "hi"}],
        )
        assert state["session_id"] == "sess-123"
        assert state["session_summary"] == "Previous conversation about revenue"
        assert len(state["chat_history"]) == 1

    def test_create_initial_state_with_revisions(self) -> None:
        state = create_initial_state(
            question="Q",
            principal_subject="user@example.com",
            authorization_revision=5,
            retrieval_policy_revision=3,
            model_route_revisions={"QUERY": 2, "KEYWORDS": 1},
        )
        assert state["authorization_revision"] == 5
        assert state["retrieval_policy_revision"] == 3
        assert state["model_route_revisions"] == {"QUERY": 2, "KEYWORDS": 1}

    def test_invariant_constants(self) -> None:
        assert MAX_REWRITTEN_QUERIES == 3
        assert MAX_QUERY_CHARS == 512
        assert MAX_PLAN_STEPS == 5
        assert MAX_RETRIEVAL_ATTEMPTS == 3
        assert MAX_TARGETED_EXPANSIONS == 2
        assert MAX_EVIDENCE_CHUNKS == 10
        assert MAX_GENERATION_REVISIONS == 2
        assert RUNTIME_BUDGET_SECONDS == 60


# ===================================================================
# Supporting types
# ===================================================================


class TestSupportingTypes:
    """Tests for supporting dataclass types."""

    def test_plan_step(self) -> None:
        step = PlanStep(operation="resolve_versions", description="Get latest v1")
        assert step.operation == "resolve_versions"
        assert step.document_selector is None

    def test_query_hints(self) -> None:
        hints = QueryHints(entities=["Acme Corp"], dates=["2025-Q3"])
        assert hints.entities == ["Acme Corp"]
        assert hints.locale == "en"

    def test_relevance_grade(self) -> None:
        grade = RelevanceGrade(evidence_id="e1", grade="relevant")
        assert grade.grade == "relevant"

    def test_version_ref(self) -> None:
        ref = VersionRef(
            document_id="d1",
            version_id="v1",
            version_number=3,
            selector_used="v3",
            status="resolved",
        )
        assert ref.status == "resolved"

    def test_claim_and_citation(self) -> None:
        claim = Claim(claim_id="c1", text="Revenue was $1M", evidence_ids=["e1"])
        citation = Citation(
            citation_id="cit1",
            claim_id="c1",
            document_id="d1",
            version_id="v1",
            version_number=1,
            chunk_id="ch1",
        )
        assert claim.grounded is True
        assert citation.valid is True

    def test_draft_answer(self) -> None:
        claim = Claim(claim_id="c1", text="Revenue was $1M", evidence_ids=["e1"])
        draft = DraftAnswer(text="The revenue was $1M.", claims=[claim])
        assert len(draft.claims) == 1

    def test_aggregation_result(self) -> None:
        result = AggregationResult(
            operation="sum",
            field_name="revenue",
            result=1000000.0,
            unit="USD",
            calculation_trace="sum = 1000000.0",
        )
        assert result.operation == "sum"


# ===================================================================
# Evidence Cache
# ===================================================================


class TestEvidenceCache:
    """Tests for the ephemeral encrypted evidence cache."""

    def test_put_and_get(self) -> None:
        cache = EvidenceCache()
        cache.put("e1", "Revenue was $1M in Q3 2025.")
        assert cache.get("e1") == "Revenue was $1M in Q3 2025."

    def test_get_missing_returns_none(self) -> None:
        cache = EvidenceCache()
        assert cache.get("missing") is None

    def test_contains(self) -> None:
        cache = EvidenceCache()
        cache.put("e1", "content")
        assert cache.contains("e1")
        assert not cache.contains("e2")

    def test_keys(self) -> None:
        cache = EvidenceCache()
        cache.put("e1", "c1")
        cache.put("e2", "c2")
        assert sorted(cache.keys()) == ["e1", "e2"]

    def test_len(self) -> None:
        cache = EvidenceCache()
        assert len(cache) == 0
        cache.put("e1", "c1")
        assert len(cache) == 1

    def test_expire_all_clears_data(self) -> None:
        cache = EvidenceCache()
        cache.put("e1", "content")
        cache.expire_all()
        assert cache.is_expired
        assert len(cache) == 0
        assert cache.keys() == []

    def test_put_after_expire_raises(self) -> None:
        cache = EvidenceCache()
        cache.expire_all()
        with pytest.raises(RuntimeError, match="expired"):
            cache.put("e1", "content")

    def test_get_after_expire_raises(self) -> None:
        cache = EvidenceCache()
        cache.put("e1", "content")
        cache.expire_all()
        with pytest.raises(RuntimeError, match="expired"):
            cache.get("e1")

    def test_encryption_is_applied(self) -> None:
        """Verify that stored values are not plaintext."""
        cache = EvidenceCache()
        cache.put("e1", "sensitive content")
        # Access the internal store directly — values should be encrypted bytes.
        raw = cache._store["e1"]
        assert isinstance(raw, bytes)
        assert b"sensitive content" not in raw

    def test_different_caches_have_different_keys(self) -> None:
        """Each cache instance uses a unique encryption key."""
        c1 = EvidenceCache()
        c2 = EvidenceCache()
        c1.put("e1", "same content")
        c2.put("e1", "same content")
        # Encrypted representations should differ due to different keys.
        assert c1._store["e1"] != c2._store["e1"]


# ===================================================================
# Aggregate Values Tool
# ===================================================================


class TestAggregateValuesTool:
    """Tests for the aggregate_values tool."""

    @pytest.mark.asyncio
    async def test_sum(self) -> None:
        from documind.rag.tools.aggregate_values import (
            AggregateValueEntry,
            AggregateValuesInput,
            aggregate_values,
        )

        input_data = AggregateValuesInput(
            operation="sum",
            values=[
                AggregateValueEntry(value="100", unit="USD", evidence_id="e1"),
                AggregateValueEntry(value="200", unit="USD", evidence_id="e2"),
            ],
        )
        result = await aggregate_values(input_data)
        assert result.error is None
        assert result.result == 300.0
        assert result.unit == "USD"
        assert "sum = 300" in result.calculation_trace

    @pytest.mark.asyncio
    async def test_avg(self) -> None:
        from documind.rag.tools.aggregate_values import (
            AggregateValueEntry,
            AggregateValuesInput,
            aggregate_values,
        )

        input_data = AggregateValuesInput(
            operation="avg",
            values=[
                AggregateValueEntry(value="10", evidence_id="e1"),
                AggregateValueEntry(value="20", evidence_id="e2"),
                AggregateValueEntry(value="30", evidence_id="e3"),
            ],
        )
        result = await aggregate_values(input_data)
        assert result.error is None
        assert result.result == 20.0

    @pytest.mark.asyncio
    async def test_mixed_currencies_rejected(self) -> None:
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
        assert "Incompatible" in result.error or "currencies" in result.error.lower()

    @pytest.mark.asyncio
    async def test_incompatible_units_rejected(self) -> None:
        from documind.rag.tools.aggregate_values import (
            AggregateValueEntry,
            AggregateValuesInput,
            aggregate_values,
        )

        input_data = AggregateValuesInput(
            operation="sum",
            values=[
                AggregateValueEntry(value="100", unit="kg", evidence_id="e1"),
                AggregateValueEntry(value="200", unit="USD", evidence_id="e2"),
            ],
        )
        result = await aggregate_values(input_data)
        assert result.error is not None
        assert "Incompatible" in result.error

    @pytest.mark.asyncio
    async def test_malformed_value_rejected(self) -> None:
        from documind.rag.tools.aggregate_values import (
            AggregateValueEntry,
            AggregateValuesInput,
            aggregate_values,
        )

        input_data = AggregateValuesInput(
            operation="sum",
            values=[
                AggregateValueEntry(value="not_a_number", evidence_id="e1"),
            ],
        )
        result = await aggregate_values(input_data)
        assert result.error is not None
        assert "Malformed" in result.error

    @pytest.mark.asyncio
    async def test_count(self) -> None:
        from documind.rag.tools.aggregate_values import (
            AggregateValueEntry,
            AggregateValuesInput,
            aggregate_values,
        )

        input_data = AggregateValuesInput(
            operation="count",
            values=[
                AggregateValueEntry(value="1", evidence_id="e1"),
                AggregateValueEntry(value="2", evidence_id="e2"),
                AggregateValueEntry(value="3", evidence_id="e3"),
            ],
        )
        result = await aggregate_values(input_data)
        assert result.error is None
        assert result.result == 3

    @pytest.mark.asyncio
    async def test_calculation_trace_records_inputs(self) -> None:
        from documind.rag.tools.aggregate_values import (
            AggregateValueEntry,
            AggregateValuesInput,
            aggregate_values,
        )

        input_data = AggregateValuesInput(
            operation="sum",
            values=[
                AggregateValueEntry(value="100", unit="USD", evidence_id="e1", field_name="revenue"),
                AggregateValueEntry(value="200", unit="USD", evidence_id="e2", field_name="revenue"),
            ],
        )
        result = await aggregate_values(input_data)
        assert "revenue" in result.calculation_trace
        assert "e1" in result.calculation_trace
        assert "e2" in result.calculation_trace


# ===================================================================
# Write Trace Tool
# ===================================================================


class TestWriteTraceTool:
    """Tests for the write_trace tool."""

    @pytest.mark.asyncio
    async def test_write_trace_calls_audit(self) -> None:
        from documind.rag.tools.write_trace import WriteTraceInput, write_trace

        audit_service = AsyncMock()
        audit_service.write_event = AsyncMock()

        input_data = WriteTraceInput(
            event_type="retrieval_started",
            principal_subject="user@example.com",
            trace_id=str(uuid.uuid4()),
        )

        result = await write_trace(input_data, audit_service)
        assert result.event_id
        audit_service.write_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_trace_event_id_is_uuid(self) -> None:
        from documind.rag.tools.write_trace import WriteTraceInput, write_trace

        audit_service = AsyncMock()
        audit_service.write_event = AsyncMock()

        input_data = WriteTraceInput(
            event_type="generation_complete",
            principal_subject="user@example.com",
            trace_id=str(uuid.uuid4()),
        )

        result = await write_trace(input_data, audit_service)
        uuid.UUID(result.event_id)  # Should not raise


# ===================================================================
# Compare Versions Tool
# ===================================================================


class TestCompareVersionsTool:
    """Tests for the compare_versions tool."""

    @pytest.mark.asyncio
    async def test_compare_empty_evidence(self) -> None:
        from documind.rag.tools.compare_versions import CompareVersionsInput, compare_versions

        cache = EvidenceCache()
        input_data = CompareVersionsInput(
            left_version_id="v1",
            right_version_id="v2",
            evidence_ids=[],
        )
        result = await compare_versions(input_data, cache)
        assert result.structured_diff == []
        assert result.text_diff["change_count"] == 0

    @pytest.mark.asyncio
    async def test_compare_with_evidence(self) -> None:
        from documind.rag.tools.compare_versions import CompareVersionsInput, compare_versions

        cache = EvidenceCache()
        cache.put("e1", "Old content for v1")
        cache.put("e2", "New content for v2")

        input_data = CompareVersionsInput(
            left_version_id="v1",
            right_version_id="v2",
            evidence_ids=["e1", "e2"],
        )
        result = await compare_versions(input_data, cache)
        assert len(result.evidence_ids) == 2


# ===================================================================
# Extract Structured Tool
# ===================================================================


class TestExtractStructuredTool:
    """Tests for the extract_structured tool."""

    @pytest.mark.asyncio
    async def test_no_template_returns_pending(self) -> None:
        from documind.rag.tools.extract_structured import ExtractStructuredInput, extract_structured

        input_data = ExtractStructuredInput(
            template_revision_id=None,
            evidence_ids=["e1"],
        )
        result = await extract_structured(input_data, llm_service=None, evidence_cache=None)
        assert result.pending_template is True
        assert result.valid is False

    @pytest.mark.asyncio
    async def test_template_with_no_evidence(self) -> None:
        from documind.rag.tools.extract_structured import ExtractStructuredInput, extract_structured

        mock_loader = AsyncMock()
        mock_loader.load = AsyncMock(
            return_value={"json_schema": {"type": "object"}, "field_dictionary": {}}
        )

        cache = EvidenceCache()

        input_data = ExtractStructuredInput(
            template_revision_id="tmpl-1",
            evidence_ids=["e1"],
        )
        result = await extract_structured(
            input_data, llm_service=None, evidence_cache=cache, template_loader=mock_loader,
        )
        assert result.valid is False
        assert "No evidence" in result.validation_errors[0]
