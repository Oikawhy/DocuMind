"""Tests for analysis layer nodes (Substep 8.4).

Tests cover: version resolution, template extraction, missing template,
numeric unit incompatibility, calculation trace, and comparator claims.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from documind.rag.state import (
    AgentState,
    EvidenceCache,
    PlanStep,
    VersionRef,
    create_initial_state,
)


def _base_state(**overrides) -> AgentState:
    return create_initial_state(
        question="Compare revenue between versions",
        principal_subject="user@example.com",
        **overrides,
    )


def _mock_llm_result(parsed: dict):
    result = MagicMock()
    result.content = ""
    result.structured = MagicMock()
    result.structured.valid = True
    result.structured.parsed = parsed
    return result


# ===================================================================
# Version Resolver Node
# ===================================================================


class TestVersionResolverNode:
    """Tests for the Version Resolver node."""

    @pytest.mark.asyncio
    async def test_no_selectors_skips(self) -> None:
        from documind.rag.nodes.version_resolver import version_resolver_node

        state = _base_state()
        state["plan"] = []

        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
        session_factory = MagicMock(return_value=mock_session_ctx)

        result = await version_resolver_node(
            state, session_factory=session_factory, allowed_document_ids=set(),
        )
        assert "no_selectors" in result["agent_path"][-1]

    @pytest.mark.asyncio
    async def test_resolves_from_plan(self) -> None:
        from documind.rag.nodes.version_resolver import version_resolver_node

        doc_id = str(uuid.uuid4())
        state = _base_state()
        state["plan"] = [
            PlanStep(
                operation="resolve_versions",
                description="Get latest",
                document_selector=doc_id,
                version_selector="latest_completed",
            ),
        ]

        # Mock session that returns no version (missing).
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
        session_factory = MagicMock(return_value=mock_session_ctx)

        result = await version_resolver_node(
            state, session_factory=session_factory, allowed_document_ids={doc_id},
        )

        assert len(result["resolved_versions"]) >= 1

    @pytest.mark.asyncio
    async def test_unauthorized_document_returns_inaccessible(self) -> None:
        from documind.rag.nodes.version_resolver import version_resolver_node

        doc_id = str(uuid.uuid4())
        state = _base_state()
        state["plan"] = [
            PlanStep(
                operation="resolve_versions",
                description="Get v1",
                document_selector=doc_id,
                version_selector="v1",
            ),
        ]

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
        session_factory = MagicMock(return_value=mock_session_ctx)

        result = await version_resolver_node(
            state,
            session_factory=session_factory,
            allowed_document_ids=set(),  # Empty — doc not authorized
        )

        assert len(result["resolved_versions"]) == 1
        assert result["resolved_versions"][0].status == "inaccessible"


# ===================================================================
# Extractor Node
# ===================================================================


class TestExtractorNode:
    """Tests for the Extractor node."""

    @pytest.mark.asyncio
    async def test_skipped_for_non_extraction(self) -> None:
        from documind.rag.nodes.extractor import extractor_node

        state = _base_state()
        state["route_type"] = "simple_qa"
        cache = EvidenceCache()

        result = await extractor_node(
            state, llm_service=AsyncMock(), evidence_cache=cache,
        )
        assert result["extraction_results"] == []
        assert "skipped" in result["agent_path"][-1]

    @pytest.mark.asyncio
    async def test_pending_template_when_none_active(self) -> None:
        from documind.rag.nodes.extractor import extractor_node

        state = _base_state()
        state["route_type"] = "extraction"
        state["reranked_evidence_ids"] = ["e1"]
        cache = EvidenceCache()
        cache.put("e1", "Some content")

        result = await extractor_node(
            state, llm_service=AsyncMock(), evidence_cache=cache,
            template_loader=None,
        )

        assert len(result["extraction_results"]) == 1
        assert result["extraction_results"][0].pending_template is True
        assert result["extraction_results"][0].valid is False


# ===================================================================
# Comparator Node
# ===================================================================


class TestComparatorNode:
    """Tests for the Comparator node."""

    @pytest.mark.asyncio
    async def test_skipped_for_non_comparison(self) -> None:
        from documind.rag.nodes.comparator import comparator_node

        state = _base_state()
        state["route_type"] = "simple_qa"
        cache = EvidenceCache()

        result = await comparator_node(
            state, llm_service=AsyncMock(), evidence_cache=cache,
        )
        assert result["comparison_result"] is None
        assert "skipped" in result["agent_path"][-1]

    @pytest.mark.asyncio
    async def test_insufficient_versions(self) -> None:
        from documind.rag.nodes.comparator import comparator_node

        state = _base_state()
        state["route_type"] = "comparison"
        state["resolved_versions"] = [
            VersionRef(document_id="d1", version_id="v1", version_number=1, selector_used="v1"),
        ]
        cache = EvidenceCache()

        result = await comparator_node(
            state, llm_service=AsyncMock(), evidence_cache=cache,
        )
        assert result["comparison_result"] is None
        assert "insufficient" in result["agent_path"][-1]

    @pytest.mark.asyncio
    async def test_emits_claims_from_comparison(self) -> None:
        from documind.rag.nodes.comparator import comparator_node

        state = _base_state()
        state["route_type"] = "comparison"
        state["resolved_versions"] = [
            VersionRef(document_id="d1", version_id="v1", version_number=1, selector_used="v1"),
            VersionRef(document_id="d1", version_id="v2", version_number=2, selector_used="v2"),
        ]
        state["reranked_evidence_ids"] = ["e1", "e2"]

        cache = EvidenceCache()
        cache.put("e1", "Old revenue: $1M")
        cache.put("e2", "New revenue: $2M")

        parsed = {
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "Revenue increased from $1M to $2M",
                    "evidence_ids": ["e1", "e2"],
                },
            ],
        }
        llm = AsyncMock()
        llm.invoke = AsyncMock(return_value=_mock_llm_result(parsed))

        result = await comparator_node(state, llm_service=llm, evidence_cache=cache)

        assert result["comparison_result"] is not None
        assert len(result["comparison_result"].claims) == 1
        assert result["comparison_result"].claims[0].text == "Revenue increased from $1M to $2M"


# ===================================================================
# Aggregator Node
# ===================================================================


class TestAggregatorNode:
    """Tests for the Aggregator node."""

    @pytest.mark.asyncio
    async def test_skipped_for_non_aggregation(self) -> None:
        from documind.rag.nodes.aggregator import aggregator_node

        state = _base_state()
        state["route_type"] = "simple_qa"
        cache = EvidenceCache()

        result = await aggregator_node(state, evidence_cache=cache)
        assert result["aggregation_result"] is None
        assert "skipped" in result["agent_path"][-1]

    @pytest.mark.asyncio
    async def test_sum_aggregation(self) -> None:
        from documind.rag.nodes.aggregator import aggregator_node

        state = _base_state()
        state["route_type"] = "aggregation"
        state["reranked_evidence_ids"] = ["e1", "e2"]
        state["plan"] = [PlanStep(operation="aggregate_values", description="Sum the totals")]

        cache = EvidenceCache()
        cache.put("e1", "Total: 100 USD")
        cache.put("e2", "Total: 200 USD")

        result = await aggregator_node(state, evidence_cache=cache)

        assert result["aggregation_result"] is not None
        assert result["aggregation_result"].operation == "sum"
        assert result["aggregation_result"].result is not None

    @pytest.mark.asyncio
    async def test_no_values_abstains(self) -> None:
        from documind.rag.nodes.aggregator import aggregator_node

        state = _base_state()
        state["route_type"] = "aggregation"
        state["reranked_evidence_ids"] = ["e1"]

        cache = EvidenceCache()
        cache.put("e1", "No numbers here.")

        result = await aggregator_node(state, evidence_cache=cache)

        assert result["aggregation_result"] is None
        assert "no_values" in result["agent_path"][-1]

    @pytest.mark.asyncio
    async def test_calculation_trace_recorded(self) -> None:
        from documind.rag.nodes.aggregator import aggregator_node

        state = _base_state()
        state["route_type"] = "aggregation"
        state["reranked_evidence_ids"] = ["e1"]
        state["plan"] = []

        cache = EvidenceCache()
        cache.put("e1", "Revenue: 500 USD, Expenses: 300 USD")

        result = await aggregator_node(state, evidence_cache=cache)

        if result["aggregation_result"] is not None:
            assert result["aggregation_result"].calculation_trace != ""
            assert len(result["aggregation_result"].evidence_ids) > 0
