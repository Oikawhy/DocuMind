"""Tests for Router, Planner, and Query Rewriter nodes (Substep 8.2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from documind.rag.prompts.registry import PromptRegistry, PromptTemplate, build_default_registry
from documind.rag.prompts.safety import INJECTION_SAFETY_PREAMBLE, wrap_with_safety
from documind.rag.state import AgentState, QueryHints, create_initial_state
from documind.services.llm_service import ModelRole

# ===================================================================
# Helpers
# ===================================================================


def _mock_llm_result(content: str, parsed: dict | None = None):
    """Create a mock LLMResult with structured output."""
    result = MagicMock()
    result.content = content
    if parsed is not None:
        result.structured = MagicMock()
        result.structured.valid = True
        result.structured.parsed = parsed
    else:
        result.structured = None
    return result


def _make_llm_service(response_content: str = "", parsed: dict | None = None):
    """Create a mock LLMService that returns a fixed response."""
    service = AsyncMock()
    service.invoke = AsyncMock(return_value=_mock_llm_result(response_content, parsed))
    return service


def _base_state(**overrides) -> AgentState:
    return create_initial_state(
        question="What was the Q3 revenue?",
        principal_subject="user@example.com",
        **overrides,
    )


# ===================================================================
# Prompt Registry
# ===================================================================


class TestPromptRegistry:
    """Tests for the prompt registry."""

    def test_register_and_resolve(self) -> None:
        registry = PromptRegistry()
        template = PromptTemplate(
            name="test", revision=1, text="Hello", permitted_role=ModelRole.KEYWORDS,
        )
        registry.register(template)
        resolved = registry.resolve("test")
        assert resolved.name == "test"
        assert resolved.revision == 1

    def test_resolve_latest_revision(self) -> None:
        registry = PromptRegistry()
        t1 = PromptTemplate(name="test", revision=1, text="v1", permitted_role=ModelRole.KEYWORDS)
        t2 = PromptTemplate(name="test", revision=2, text="v2", permitted_role=ModelRole.KEYWORDS)
        registry.register(t1)
        registry.register(t2)
        assert registry.resolve("test").revision == 2
        assert registry.resolve("test", revision=1).revision == 1

    def test_resolve_missing_raises(self) -> None:
        registry = PromptRegistry()
        with pytest.raises(KeyError):
            registry.resolve("nonexistent")

    def test_integrity_check(self) -> None:
        import hashlib
        text = "test prompt"
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        template = PromptTemplate(
            name="test", revision=1, text=text,
            permitted_role=ModelRole.KEYWORDS, sha256=sha,
        )
        assert template.verify_integrity()

    def test_integrity_check_fails_on_tampered(self) -> None:
        template = PromptTemplate(
            name="test", revision=1, text="original",
            permitted_role=ModelRole.KEYWORDS, sha256="wrong_hash",
        )
        assert not template.verify_integrity()

    def test_register_tampered_raises(self) -> None:
        registry = PromptRegistry()
        template = PromptTemplate(
            name="test", revision=1, text="original",
            permitted_role=ModelRole.KEYWORDS, sha256="wrong_hash",
        )
        with pytest.raises(ValueError, match="integrity"):
            registry.register(template)

    def test_invocation_logging(self) -> None:
        registry = PromptRegistry()
        registry.record_invocation("router", 1, ModelRole.KEYWORDS)
        assert len(registry.invocation_log) == 1
        assert registry.invocation_log[0]["template_name"] == "router"

    def test_build_default_registry(self) -> None:
        registry = build_default_registry()
        names = registry.list_templates()
        assert "router" in names
        assert "planner" in names
        assert "query_rewriter" in names
        assert "generator" in names
        assert "hallucination_grader" in names


# ===================================================================
# Safety Preamble
# ===================================================================


class TestSafetyPreamble:
    """Tests for the injection-safety preamble."""

    def test_preamble_content(self) -> None:
        assert "UNTRUSTED DATA" in INJECTION_SAFETY_PREAMBLE
        assert "system instructions" in INJECTION_SAFETY_PREAMBLE
        assert "credentials" in INJECTION_SAFETY_PREAMBLE

    def test_wrap_with_safety(self) -> None:
        wrapped = wrap_with_safety("Classify this question.")
        assert wrapped.startswith(INJECTION_SAFETY_PREAMBLE)
        assert "Classify this question." in wrapped


# ===================================================================
# Router Node
# ===================================================================


class TestRouterNode:
    """Tests for the Router node."""

    @pytest.mark.asyncio
    async def test_valid_classification(self) -> None:
        from documind.rag.nodes.router import router_node

        llm = _make_llm_service(parsed={"route": "simple_qa", "confidence": 0.85})
        state = _base_state()

        result = await router_node(state, llm_service=llm)
        assert result["route_type"] == "simple_qa"
        assert result["route_confidence"] == 0.85
        assert "router:simple_qa" in result["agent_path"]

    @pytest.mark.asyncio
    async def test_invalid_route_defaults_to_clarification(self) -> None:
        from documind.rag.nodes.router import router_node

        llm = _make_llm_service(parsed={"route": "invalid_type", "confidence": 0.9})
        state = _base_state()

        result = await router_node(state, llm_service=llm)
        assert result["route_type"] == "clarification"
        assert "clarification_fallback" in result["agent_path"][-1]

    @pytest.mark.asyncio
    async def test_invalid_json_defaults_to_clarification(self) -> None:
        from documind.rag.nodes.router import router_node

        llm = _make_llm_service(response_content="not json at all")
        state = _base_state()

        result = await router_node(state, llm_service=llm)
        assert result["route_type"] == "clarification"

    @pytest.mark.asyncio
    async def test_low_confidence_no_hybrid_clarifies(self) -> None:
        from documind.rag.nodes.router import router_node

        llm = _make_llm_service(parsed={"route": "simple_qa", "confidence": 0.3})
        state = _base_state(retrieval_policy_revision=0)

        result = await router_node(state, llm_service=llm)
        assert result["route_type"] == "clarification"

    @pytest.mark.asyncio
    async def test_low_confidence_with_hybrid_proceeds(self) -> None:
        from documind.rag.nodes.router import router_node

        llm = _make_llm_service(parsed={"route": "simple_qa", "confidence": 0.4})
        state = _base_state(retrieval_policy_revision=1)

        result = await router_node(state, llm_service=llm)
        assert result["route_type"] == "simple_qa"
        assert "low_confidence_hybrid" in result["agent_path"][-1]

    @pytest.mark.asyncio
    async def test_llm_error_defaults_to_clarification(self) -> None:
        from documind.rag.nodes.router import router_node

        llm = AsyncMock()
        llm.invoke = AsyncMock(side_effect=RuntimeError("model unavailable"))
        state = _base_state()

        result = await router_node(state, llm_service=llm)
        assert result["route_type"] == "clarification"


# ===================================================================
# Planner Node
# ===================================================================


class TestPlannerNode:
    """Tests for the Planner node."""

    @pytest.mark.asyncio
    async def test_skipped_for_simple_qa(self) -> None:
        from documind.rag.nodes.planner import planner_node

        llm = _make_llm_service()
        state = _base_state()
        state["route_type"] = "simple_qa"

        result = await planner_node(state, llm_service=llm)
        assert result["plan"] == []
        assert "planner:skipped" in result["agent_path"]
        llm.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_plan_for_comparison(self) -> None:
        from documind.rag.nodes.planner import planner_node

        parsed = {
            "steps": [
                {"operation": "resolve_versions", "description": "Get latest version of doc A"},
                {"operation": "compare_versions", "description": "Compare v1 and v2"},
            ],
        }
        llm = _make_llm_service(parsed=parsed)
        state = _base_state()
        state["route_type"] = "comparison"

        result = await planner_node(state, llm_service=llm)
        assert len(result["plan"]) == 2
        assert result["plan"][0].operation == "resolve_versions"
        assert result["plan"][1].operation == "compare_versions"

    @pytest.mark.asyncio
    async def test_max_5_steps(self) -> None:
        from documind.rag.nodes.planner import planner_node

        parsed = {
            "steps": [
                {"operation": "retrieve_evidence", "description": f"Step {i}"}
                for i in range(10)
            ],
        }
        llm = _make_llm_service(parsed=parsed)
        state = _base_state()
        state["route_type"] = "aggregation"

        result = await planner_node(state, llm_service=llm)
        assert len(result["plan"]) <= 5

    @pytest.mark.asyncio
    async def test_rejects_cypher_syntax(self) -> None:
        from documind.rag.nodes.planner import planner_node

        parsed = {
            "steps": [
                {"operation": "retrieve_evidence", "description": "MATCH (n) RETURN n"},
            ],
        }
        llm = _make_llm_service(parsed=parsed)
        state = _base_state()
        state["route_type"] = "comparison"

        result = await planner_node(state, llm_service=llm)
        assert len(result["plan"]) == 0  # Rejected

    @pytest.mark.asyncio
    async def test_rejects_sql_syntax(self) -> None:
        from documind.rag.nodes.planner import planner_node

        parsed = {
            "steps": [
                {"operation": "retrieve_evidence", "description": "SELECT * FROM documents"},
            ],
        }
        llm = _make_llm_service(parsed=parsed)
        state = _base_state()
        state["route_type"] = "extraction"

        result = await planner_node(state, llm_service=llm)
        assert len(result["plan"]) == 0

    @pytest.mark.asyncio
    async def test_rejects_invalid_operation(self) -> None:
        from documind.rag.nodes.planner import planner_node

        parsed = {
            "steps": [
                {"operation": "execute_shell", "description": "Run a shell command"},
            ],
        }
        llm = _make_llm_service(parsed=parsed)
        state = _base_state()
        state["route_type"] = "extraction"

        result = await planner_node(state, llm_service=llm)
        assert len(result["plan"]) == 0


# ===================================================================
# Query Rewriter Node
# ===================================================================


class TestQueryRewriterNode:
    """Tests for the Query Rewriter node."""

    @pytest.mark.asyncio
    async def test_produces_query_variants(self) -> None:
        from documind.rag.nodes.query_rewriter import query_rewriter_node

        parsed = {
            "queries": ["Q3 2025 revenue", "third quarter revenue 2025", "revenue Q3"],
            "hints": {"entities": ["Q3", "2025"], "dates": ["2025-Q3"], "amounts": []},
        }
        llm = _make_llm_service(parsed=parsed)
        state = _base_state()

        result = await query_rewriter_node(state, llm_service=llm)
        assert len(result["rewritten_queries"]) == 3
        assert isinstance(result["query_hints"], QueryHints)
        assert "Q3" in result["query_hints"].entities

    @pytest.mark.asyncio
    async def test_max_3_variants(self) -> None:
        from documind.rag.nodes.query_rewriter import query_rewriter_node

        parsed = {
            "queries": ["q1", "q2", "q3", "q4", "q5"],
            "hints": {},
        }
        llm = _make_llm_service(parsed=parsed)
        state = _base_state()

        result = await query_rewriter_node(state, llm_service=llm)
        assert len(result["rewritten_queries"]) <= 3

    @pytest.mark.asyncio
    async def test_512_char_enforcement(self) -> None:
        from documind.rag.nodes.query_rewriter import query_rewriter_node

        long_query = "x" * 1000
        parsed = {"queries": [long_query], "hints": {}}
        llm = _make_llm_service(parsed=parsed)
        state = _base_state()

        result = await query_rewriter_node(state, llm_service=llm)
        assert len(result["rewritten_queries"][0]) <= 512

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_json(self) -> None:
        from documind.rag.nodes.query_rewriter import query_rewriter_node

        llm = _make_llm_service(response_content="invalid json response")
        state = _base_state()

        result = await query_rewriter_node(state, llm_service=llm)
        assert len(result["rewritten_queries"]) >= 1
        assert result["rewritten_queries"][0] == state["original_question"]

    @pytest.mark.asyncio
    async def test_llm_error_uses_original(self) -> None:
        from documind.rag.nodes.query_rewriter import query_rewriter_node

        llm = AsyncMock()
        llm.invoke = AsyncMock(side_effect=RuntimeError("fail"))
        state = _base_state()

        result = await query_rewriter_node(state, llm_service=llm)
        assert result["rewritten_queries"][0] == state["original_question"]
