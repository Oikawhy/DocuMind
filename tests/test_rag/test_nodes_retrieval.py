"""Tests for retrieval layer nodes (Substep 8.3).

Tests cover: permission divergence filtering, max retrieval retry (3),
max targeted expansion (2), abstention on exhaustion, reranker on
permitted IDs only.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from documind.rag.state import (
    AgentState,
    EvidenceCache,
    create_initial_state,
)


def _base_state(**overrides) -> AgentState:
    state = create_initial_state(
        question="What is the Q3 revenue?",
        principal_subject="user@example.com",
        **overrides,
    )
    state["rewritten_queries"] = ["Q3 revenue", "third quarter revenue"]
    return state


def _mock_llm_result(parsed: dict):
    result = MagicMock()
    result.content = ""
    result.structured = MagicMock()
    result.structured.valid = True
    result.structured.parsed = parsed
    return result


# ===================================================================
# Retrieval Orchestrator
# ===================================================================


class TestRetrievalOrchestrator:
    """Tests for the Retrieval Orchestrator node."""

    @pytest.mark.asyncio
    async def test_first_retrieval_attempt(self) -> None:
        from documind.rag.nodes.retrieval_orchestrator import retrieval_orchestrator_node

        retrieval_service = AsyncMock()
        principal = MagicMock()

        # Mock the search method to return evidence items.
        mock_evidence = MagicMock()
        mock_evidence.chunk_id = uuid.uuid4()
        mock_response = MagicMock()
        mock_response.evidence = [mock_evidence]
        mock_response.retrieval_metadata = MagicMock()
        mock_response.retrieval_metadata.mode = "hybrid"
        mock_response.retrieval_metadata.backend_timings = {"qdrant": 50}
        mock_response.retrieval_metadata.candidate_count_before_auth = 10
        mock_response.degraded_branches = []
        retrieval_service.search = AsyncMock(return_value=mock_response)

        state = _base_state()
        result = await retrieval_orchestrator_node(
            state, retrieval_service=retrieval_service, principal=principal,
        )

        assert result["retrieval_attempts"] == 1
        assert len(result["candidate_ids"]) > 0

    @pytest.mark.asyncio
    async def test_max_retrieval_attempts(self) -> None:
        from documind.rag.nodes.retrieval_orchestrator import retrieval_orchestrator_node

        state = _base_state()
        state["retrieval_attempts"] = 3  # Already at max

        result = await retrieval_orchestrator_node(
            state, retrieval_service=AsyncMock(), principal=MagicMock(),
        )

        assert result.get("abstention_reason") is not None
        assert "Maximum retrieval attempts" in result["abstention_reason"]

    @pytest.mark.asyncio
    async def test_retrieval_error_degrades(self) -> None:
        from documind.rag.nodes.retrieval_orchestrator import retrieval_orchestrator_node

        retrieval_service = AsyncMock()
        retrieval_service.search = AsyncMock(side_effect=RuntimeError("backend down"))

        state = _base_state()
        result = await retrieval_orchestrator_node(
            state, retrieval_service=retrieval_service, principal=MagicMock(),
        )

        assert result["retrieval_attempts"] == 1
        assert len(result["degraded_branches"]) > 0


# ===================================================================
# Permission Guard Node
# ===================================================================


class TestPermissionGuardNode:
    """Tests for the Permission Guard node."""

    @pytest.mark.asyncio
    async def test_filters_unauthorized_candidates(self) -> None:
        from documind.rag.nodes.permission_guard import permission_guard_node

        doc_id = str(uuid.uuid4())
        chunk_id = str(uuid.uuid4())

        state = _base_state()
        state["candidate_ids"] = [chunk_id, "invalid-uuid", str(uuid.uuid4())]

        # Mock session factory — return empty results for chunk lookups.
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)

        session_factory = MagicMock()
        session_factory.__aenter__ = AsyncMock(return_value=mock_session)
        session_factory.__aexit__ = AsyncMock(return_value=None)
        session_factory_fn = MagicMock(return_value=session_factory)

        result = await permission_guard_node(
            state,
            session_factory=session_factory_fn,
            allowed_document_ids={doc_id},
        )

        # All candidates should be filtered since chunks don't exist in DB.
        assert result["filtered_out_count"] >= 2

    @pytest.mark.asyncio
    async def test_no_candidates(self) -> None:
        from documind.rag.nodes.permission_guard import permission_guard_node

        state = _base_state()
        state["candidate_ids"] = []

        result = await permission_guard_node(
            state,
            session_factory=AsyncMock(),
            allowed_document_ids=set(),
        )

        assert result["filtered_candidate_ids"] == []
        assert "no_candidates" in result["agent_path"][-1]


# ===================================================================
# Reranker Node
# ===================================================================


class TestRerankerNode:
    """Tests for the Reranker node."""

    @pytest.mark.asyncio
    async def test_reranks_permitted_only(self) -> None:
        from documind.rag.nodes.reranker import reranker_node

        cache = EvidenceCache()
        eid1, eid2 = str(uuid.uuid4()), str(uuid.uuid4())
        cache.put(eid1, "Revenue was $1M.")
        cache.put(eid2, "Expenses were $500K.")

        # Mock reranker.
        mock_scored_1 = MagicMock()
        mock_scored_1.chunk_id = uuid.UUID(eid1)
        mock_scored_1.score = 0.9
        mock_scored_2 = MagicMock()
        mock_scored_2.chunk_id = uuid.UUID(eid2)
        mock_scored_2.score = 0.7

        reranker = AsyncMock()
        reranker.rerank = AsyncMock(return_value=[mock_scored_1, mock_scored_2])

        # Mock session factory for load_evidence.
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)

        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=None)
        session_factory = MagicMock(return_value=session_ctx)

        state = _base_state()
        state["filtered_candidate_ids"] = [eid1, eid2]

        result = await reranker_node(
            state,
            reranker_service=reranker,
            evidence_cache=cache,
            session_factory=session_factory,
        )

        assert len(result["reranked_evidence_ids"]) == 2

    @pytest.mark.asyncio
    async def test_no_evidence(self) -> None:
        from documind.rag.nodes.reranker import reranker_node

        state = _base_state()
        state["filtered_candidate_ids"] = []

        result = await reranker_node(
            state,
            reranker_service=AsyncMock(),
            evidence_cache=EvidenceCache(),
            session_factory=AsyncMock(),
        )

        assert result["reranked_evidence_ids"] == []


# ===================================================================
# Relevance Grader Node
# ===================================================================


class TestRelevanceGraderNode:
    """Tests for the Relevance Grader node."""

    @pytest.mark.asyncio
    async def test_grades_evidence(self) -> None:
        from documind.rag.nodes.relevance_grader import relevance_grader_node

        eid1, eid2 = str(uuid.uuid4()), str(uuid.uuid4())
        cache = EvidenceCache()
        cache.put(eid1, "Q3 revenue was $1M.")
        cache.put(eid2, "Employee count is 500.")

        parsed = {
            "grades": [
                {"evidence_id": eid1, "grade": "relevant", "reason": "Direct answer"},
                {"evidence_id": eid2, "grade": "irrelevant", "reason": "Unrelated"},
            ],
            "request_kind": "answer",
        }
        llm = AsyncMock()
        llm.invoke = AsyncMock(return_value=_mock_llm_result(parsed))

        state = _base_state()
        state["reranked_evidence_ids"] = [eid1, eid2]

        result = await relevance_grader_node(state, llm_service=llm, evidence_cache=cache)

        assert len(result["relevance_grades"]) == 2
        assert result["relevance_request_kind"] == "answer"

    @pytest.mark.asyncio
    async def test_max_retrieval_retry_enforced(self) -> None:
        from documind.rag.nodes.relevance_grader import relevance_grader_node

        eid = str(uuid.uuid4())
        cache = EvidenceCache()
        cache.put(eid, "some content")

        parsed = {
            "grades": [{"evidence_id": eid, "grade": "irrelevant"}],
            "request_kind": "rewrite",
        }
        llm = AsyncMock()
        llm.invoke = AsyncMock(return_value=_mock_llm_result(parsed))

        state = _base_state()
        state["reranked_evidence_ids"] = [eid]
        state["retrieval_attempts"] = 3  # At max

        result = await relevance_grader_node(state, llm_service=llm, evidence_cache=cache)

        # Should convert "rewrite" to "abstain" because max retries reached.
        assert result["relevance_request_kind"] == "abstain"

    @pytest.mark.asyncio
    async def test_max_targeted_expansion_enforced(self) -> None:
        from documind.rag.nodes.relevance_grader import relevance_grader_node

        eid = str(uuid.uuid4())
        cache = EvidenceCache()
        cache.put(eid, "some content")

        parsed = {
            "grades": [{"evidence_id": eid, "grade": "partially_relevant"}],
            "request_kind": "targeted_expansion",
        }
        llm = AsyncMock()
        llm.invoke = AsyncMock(return_value=_mock_llm_result(parsed))

        state = _base_state()
        state["reranked_evidence_ids"] = [eid]
        state["targeted_expansions"] = 2  # At max

        result = await relevance_grader_node(state, llm_service=llm, evidence_cache=cache)

        # Should convert to "abstain".
        assert result["relevance_request_kind"] == "abstain"

    @pytest.mark.asyncio
    async def test_abstention_on_no_evidence(self) -> None:
        from documind.rag.nodes.relevance_grader import relevance_grader_node

        state = _base_state()
        state["reranked_evidence_ids"] = []
        cache = EvidenceCache()

        result = await relevance_grader_node(
            state, llm_service=AsyncMock(), evidence_cache=cache,
        )

        assert result["relevance_request_kind"] == "abstain"

    @pytest.mark.asyncio
    async def test_validates_evidence_ids_in_authorized_set(self) -> None:
        from documind.rag.nodes.relevance_grader import relevance_grader_node

        eid = str(uuid.uuid4())
        fake_eid = str(uuid.uuid4())  # Not in authorized set
        cache = EvidenceCache()
        cache.put(eid, "some content")

        parsed = {
            "grades": [
                {"evidence_id": eid, "grade": "relevant"},
                {"evidence_id": fake_eid, "grade": "relevant"},  # Should be filtered
            ],
            "request_kind": "answer",
        }
        llm = AsyncMock()
        llm.invoke = AsyncMock(return_value=_mock_llm_result(parsed))

        state = _base_state()
        state["reranked_evidence_ids"] = [eid]  # Only eid is authorized

        result = await relevance_grader_node(state, llm_service=llm, evidence_cache=cache)

        # Only the authorized evidence ID should be graded.
        assert len(result["relevance_grades"]) == 1
        assert result["relevance_grades"][0].evidence_id == eid
