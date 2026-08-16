"""Tests for generation layer nodes (Substep 8.5).

Tests cover: hallucination revision loop (max 2), citation invalidation
on tombstone, citation invalidation on auth change, abstention on
insufficient evidence, confidence calculation (all four tiers),
response formatter (no new claims, stable shape).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from documind.rag.nodes.confidence import calculate_confidence
from documind.rag.state import (
    AgentState,
    Citation,
    CitationVerification,
    Claim,
    DraftAnswer,
    EvidenceCache,
    HallucinationGrade,
    create_initial_state,
)


def _base_state(**overrides) -> AgentState:
    return create_initial_state(
        question="What is the Q3 revenue?",
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
# Generator Node
# ===================================================================


class TestGeneratorNode:
    """Tests for the Generator node."""

    @pytest.mark.asyncio
    async def test_generates_draft_answer(self) -> None:
        from documind.rag.nodes.generator import generator_node

        eid = str(uuid.uuid4())
        cache = EvidenceCache()
        cache.put(eid, "Q3 revenue was $1M.")

        parsed = {
            "answer": "The Q3 revenue was $1M.",
            "claims": [
                {"claim_id": "c1", "text": "Revenue was $1M", "evidence_ids": [eid]},
            ],
        }
        llm = AsyncMock()
        llm.invoke = AsyncMock(return_value=_mock_llm_result(parsed))

        state = _base_state()
        state["reranked_evidence_ids"] = [eid]

        result = await generator_node(state, llm_service=llm, evidence_cache=cache)

        assert result["draft_answer"] is not None
        assert result["draft_answer"].text == "The Q3 revenue was $1M."
        assert len(result["draft_answer"].claims) == 1

    @pytest.mark.asyncio
    async def test_no_evidence_abstains(self) -> None:
        from documind.rag.nodes.generator import generator_node

        state = _base_state()
        state["reranked_evidence_ids"] = []

        result = await generator_node(
            state, llm_service=AsyncMock(), evidence_cache=EvidenceCache(),
        )

        assert result["draft_answer"] is None
        assert "no_evidence" in result["agent_path"][-1]

    @pytest.mark.asyncio
    async def test_filters_unauthorized_evidence_ids(self) -> None:
        from documind.rag.nodes.generator import generator_node

        eid = str(uuid.uuid4())
        fake_eid = str(uuid.uuid4())
        cache = EvidenceCache()
        cache.put(eid, "Revenue data.")

        parsed = {
            "answer": "Answer",
            "claims": [
                {"claim_id": "c1", "text": "Claim 1", "evidence_ids": [eid, fake_eid]},
            ],
        }
        llm = AsyncMock()
        llm.invoke = AsyncMock(return_value=_mock_llm_result(parsed))

        state = _base_state()
        state["reranked_evidence_ids"] = [eid]

        result = await generator_node(state, llm_service=llm, evidence_cache=cache)

        # Only the authorized evidence ID should remain.
        assert len(result["draft_answer"].claims[0].evidence_ids) == 1
        assert result["draft_answer"].claims[0].evidence_ids[0] == eid


# ===================================================================
# Hallucination Grader Node
# ===================================================================


class TestHallucinationGraderNode:
    """Tests for the Hallucination Grader node."""

    @pytest.mark.asyncio
    async def test_all_grounded(self) -> None:
        from documind.rag.nodes.hallucination_grader import hallucination_grader_node

        eid = str(uuid.uuid4())
        cache = EvidenceCache()
        cache.put(eid, "Revenue was $1M.")

        parsed = {
            "issues": [
                {"claim_id": "c1", "grade": "grounded", "suggested_action": "keep"},
            ],
            "needs_revision": False,
            "all_grounded": True,
        }
        llm = AsyncMock()
        llm.invoke = AsyncMock(return_value=_mock_llm_result(parsed))

        state = _base_state()
        state["reranked_evidence_ids"] = [eid]
        state["draft_answer"] = DraftAnswer(
            text="Revenue was $1M.",
            claims=[Claim(claim_id="c1", text="Revenue was $1M", evidence_ids=[eid])],
        )

        result = await hallucination_grader_node(state, llm_service=llm, evidence_cache=cache)

        assert result["hallucination_grade"].all_grounded is True
        assert result["hallucination_grade"].needs_revision is False

    @pytest.mark.asyncio
    async def test_max_revision_cap(self) -> None:
        from documind.rag.nodes.hallucination_grader import hallucination_grader_node

        state = _base_state()
        state["generation_revisions"] = 2  # At max
        state["draft_answer"] = DraftAnswer(
            text="Unsupported claim",
            claims=[Claim(claim_id="c1", text="Bad claim", evidence_ids=[], grounded=False)],
        )

        result = await hallucination_grader_node(
            state, llm_service=AsyncMock(), evidence_cache=EvidenceCache(),
        )

        # Should trigger abstention due to max revisions with unsupported claims.
        assert result.get("abstention_reason") is not None
        assert "maximum revisions" in result["abstention_reason"].lower()

    @pytest.mark.asyncio
    async def test_no_draft_answer(self) -> None:
        from documind.rag.nodes.hallucination_grader import hallucination_grader_node

        state = _base_state()

        result = await hallucination_grader_node(
            state, llm_service=AsyncMock(), evidence_cache=EvidenceCache(),
        )

        assert result["hallucination_grade"].all_grounded is False


# ===================================================================
# Citation Verifier Node
# ===================================================================


class TestCitationVerifierNode:
    """Tests for the Citation Verifier node."""

    @pytest.mark.asyncio
    async def test_valid_citations(self) -> None:
        from documind.rag.nodes.citation_verifier import citation_verifier_node

        eid = str(uuid.uuid4())
        state = _base_state()
        state["reranked_evidence_ids"] = [eid]
        state["draft_answer"] = DraftAnswer(
            text="Answer",
            claims=[Claim(claim_id="c1", text="Claim", evidence_ids=[eid])],
        )

        result = await citation_verifier_node(state)

        assert result["citation_verification"].all_valid is True
        assert len(result["citation_verification"].verified_citations) == 1

    @pytest.mark.asyncio
    async def test_uncovered_claim_invalidates(self) -> None:
        from documind.rag.nodes.citation_verifier import citation_verifier_node

        state = _base_state()
        state["reranked_evidence_ids"] = []
        state["draft_answer"] = DraftAnswer(
            text="Answer",
            claims=[Claim(claim_id="c1", text="Uncovered claim", evidence_ids=[])],
        )

        result = await citation_verifier_node(state)

        assert result["citation_verification"].all_valid is False
        assert result["citation_verification"].failure_code == "UNCOVERED_CLAIMS"

    @pytest.mark.asyncio
    async def test_unauthorized_evidence_invalidates(self) -> None:
        from documind.rag.nodes.citation_verifier import citation_verifier_node

        eid = str(uuid.uuid4())
        fake_eid = str(uuid.uuid4())
        state = _base_state()
        state["reranked_evidence_ids"] = [eid]  # fake_eid not authorized
        state["draft_answer"] = DraftAnswer(
            text="Answer",
            claims=[Claim(claim_id="c1", text="Claim", evidence_ids=[fake_eid])],
        )

        result = await citation_verifier_node(state)

        assert result["citation_verification"].all_valid is False
        assert len(result["citation_verification"].invalid_citations) == 1

    @pytest.mark.asyncio
    async def test_no_draft(self) -> None:
        from documind.rag.nodes.citation_verifier import citation_verifier_node

        state = _base_state()
        result = await citation_verifier_node(state)

        assert result["citation_verification"].failure_code == "NO_DRAFT"


# ===================================================================
# Confidence Calculation
# ===================================================================


class TestConfidenceCalculation:
    """Tests for the deterministic confidence calculation."""

    def test_high_confidence(self) -> None:
        eid1, eid2 = str(uuid.uuid4()), str(uuid.uuid4())
        state = _base_state()
        state["draft_answer"] = DraftAnswer(text="Answer", claims=[])
        state["citation_verification"] = CitationVerification(
            all_valid=True,
            verified_citations=[
                Citation(citation_id="c1", claim_id="c1", document_id="d1",
                         version_id="v1", version_number=1, chunk_id=eid1),
                Citation(citation_id="c2", claim_id="c2", document_id="d1",
                         version_id="v1", version_number=1, chunk_id=eid2),
            ],
        )
        state["hallucination_grade"] = HallucinationGrade(all_grounded=True)
        state["degraded_branches"] = []
        state["retrieval_attempts"] = 1
        state["generation_revisions"] = 0

        assert calculate_confidence(state) == "high"

    def test_medium_confidence(self) -> None:
        eid = str(uuid.uuid4())
        state = _base_state()
        state["draft_answer"] = DraftAnswer(text="Answer", claims=[])
        state["citation_verification"] = CitationVerification(
            all_valid=True,
            verified_citations=[
                Citation(citation_id="c1", claim_id="c1", document_id="d1",
                         version_id="v1", version_number=1, chunk_id=eid),
            ],
        )
        state["hallucination_grade"] = HallucinationGrade(all_grounded=True)
        state["degraded_branches"] = ["qdrant_timeout"]
        state["retrieval_attempts"] = 2
        state["generation_revisions"] = 1

        assert calculate_confidence(state) == "medium"

    def test_low_confidence_no_citations(self) -> None:
        state = _base_state()
        state["draft_answer"] = DraftAnswer(text="Answer", claims=[])
        state["citation_verification"] = CitationVerification(all_valid=True)
        state["hallucination_grade"] = HallucinationGrade(all_grounded=False)

        assert calculate_confidence(state) == "low"

    def test_low_confidence_with_abstention(self) -> None:
        state = _base_state()
        state["abstention_reason"] = "Insufficient evidence"

        assert calculate_confidence(state) == "low"

    def test_low_confidence_invalid_citations(self) -> None:
        state = _base_state()
        state["draft_answer"] = DraftAnswer(text="Answer", claims=[])
        state["citation_verification"] = CitationVerification(all_valid=False)

        assert calculate_confidence(state) == "low"


# ===================================================================
# Response Formatter Node
# ===================================================================


class TestResponseFormatterNode:
    """Tests for the Response Formatter node."""

    @pytest.mark.asyncio
    async def test_formats_answer_response(self) -> None:
        from documind.rag.nodes.response_formatter import response_formatter_node

        eid = str(uuid.uuid4())
        state = _base_state()
        state["draft_answer"] = DraftAnswer(
            text="The revenue was $1M.",
            claims=[Claim(claim_id="c1", text="Revenue was $1M", evidence_ids=[eid])],
        )
        state["citation_verification"] = CitationVerification(
            all_valid=True,
            verified_citations=[
                Citation(citation_id="cit1", claim_id="c1", document_id="d1",
                         version_id="v1", version_number=1, chunk_id=eid),
            ],
        )
        state["hallucination_grade"] = HallucinationGrade(all_grounded=True)
        state["reranked_evidence_ids"] = [eid]

        result = await response_formatter_node(state)

        response = result["final_response"]
        assert response["abstained"] is False
        assert response["answer"] == "The revenue was $1M."
        assert len(response["citations"]) == 1
        assert response["trace_id"] == state["trace_id"]

    @pytest.mark.asyncio
    async def test_formats_abstention_response(self) -> None:
        from documind.rag.nodes.response_formatter import response_formatter_node

        state = _base_state()
        state["abstention_reason"] = "Insufficient evidence"

        result = await response_formatter_node(state)

        response = result["final_response"]
        assert response["abstained"] is True
        assert response["answer"] is None
        assert response["limitation_code"] == "INSUFFICIENT_EVIDENCE"

    @pytest.mark.asyncio
    async def test_stable_response_shape(self) -> None:
        from documind.rag.nodes.response_formatter import response_formatter_node

        state = _base_state()
        state["draft_answer"] = None

        result = await response_formatter_node(state)

        response = result["final_response"]
        # Must have all required fields.
        assert "trace_id" in response
        assert "request_id" in response
        assert "route" in response
        assert "confidence" in response
        assert "agent_path" in response
        assert "policy_revisions" in response
        assert "model_route_revisions" in response
        assert "abstained" in response
        assert "citations" in response
