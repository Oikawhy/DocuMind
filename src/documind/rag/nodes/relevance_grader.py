"""Relevance Grader node per §7.4 — QUERY role evidence assessment.

Returns per-evidence grades (relevant/partially_relevant/irrelevant/
needs_more_context) plus a request kind (answer/rewrite/targeted_expansion/
abstain).  Validates that referenced evidence IDs exist in the authorized set.
"""

from __future__ import annotations

from typing import Any

import structlog

from documind.rag.prompts.safety import wrap_with_safety
from documind.rag.state import (
    MAX_RETRIEVAL_ATTEMPTS,
    MAX_TARGETED_EXPANSIONS,
    AgentState,
    EvidenceCache,
    RelevanceGrade,
)
from documind.services.llm_service import ModelRole

logger = structlog.get_logger(__name__)


async def relevance_grader_node(
    state: AgentState,
    *,
    llm_service: Any,
    evidence_cache: EvidenceCache,
    prompt_registry: Any | None = None,
) -> dict[str, Any]:
    """Grade evidence relevance and determine next action.

    Validates that referenced evidence IDs exist in the authorized set.
    Enforces loop guards for retry and targeted expansion.
    """
    reranked_ids = state.get("reranked_evidence_ids", [])

    if not reranked_ids:
        return {
            "relevance_grades": [],
            "relevance_request_kind": "abstain",
            "abstention_reason": "No evidence available for grading",
            "agent_path": [*state.get("agent_path", []), "grader:no_evidence"],
        }

    # Build evidence context for grading.
    evidence_texts: list[str] = []
    for eid in reranked_ids:
        content = evidence_cache.get(eid)
        if content:
            evidence_texts.append(f"[Evidence {eid}]: {content[:500]}")

    if not evidence_texts:
        return {
            "relevance_grades": [],
            "relevance_request_kind": "abstain",
            "abstention_reason": "No evidence content available",
            "agent_path": [*state.get("agent_path", []), "grader:no_content"],
        }

    template_text = (
        "Grade each evidence item for relevance. Return JSON with "
        "'grades' array and 'request_kind'."
    )
    if prompt_registry is not None:
        try:
            template = prompt_registry.resolve("relevance_grader")
            template_text = template.text
        except KeyError:
            pass

    system_prompt = wrap_with_safety(template_text)

    user_prompt = (
        f"Question: {state['original_question']}\n"
        f"Route: {state.get('route_type', 'simple_qa')}\n\n"
        f"Evidence:\n" + "\n\n".join(evidence_texts)
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    output_schema = {
        "type": "object",
        "properties": {
            "grades": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "grade": {
                            "type": "string",
                            "enum": [
                                "relevant", "partially_relevant",
                                "irrelevant", "needs_more_context",
                            ],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["evidence_id", "grade"],
                },
            },
            "request_kind": {
                "type": "string",
                "enum": ["answer", "rewrite", "targeted_expansion", "abstain"],
            },
        },
        "required": ["grades", "request_kind"],
    }

    try:
        from documind.rag.invoke_helpers import invoke_with_retry

        parsed, limitation_code = await invoke_with_retry(
            llm_service, ModelRole.QUERY, messages,
            output_schema=output_schema,
            template_name="relevance_grader",
        )

        # T8-15: Fail-closed on parse failure after retry.
        if parsed is None:
            return _abstain_fallback(
                state, f"Relevance grader output invalid ({limitation_code})",
            )

        raw_grades = parsed.get("grades", [])
        request_kind = parsed.get("request_kind", "abstain")

        # Validate referenced evidence IDs.
        authorized_set = set(reranked_ids)
        validated_grades: list[RelevanceGrade] = []
        for grade_data in raw_grades:
            eid = grade_data.get("evidence_id", "")
            if eid not in authorized_set:
                logger.warning("grader_invalid_evidence_id", evidence_id=eid)
                continue
            validated_grades.append(
                RelevanceGrade(
                    evidence_id=eid,
                    grade=grade_data.get("grade", "irrelevant"),
                    reason=grade_data.get("reason", ""),
                )
            )

        # Enforce loop guards.
        request_kind = _enforce_loop_guards(state, request_kind)

        if prompt_registry is not None:
            prompt_registry.record_invocation(
                "relevance_grader", 1, ModelRole.QUERY,
                input_valid=True, output_valid=True,
            )

        return {
            "relevance_grades": validated_grades,
            "relevance_request_kind": request_kind,
            # T8-10: Increment targeted_expansions counter.
            **({
                "targeted_expansions": state.get("targeted_expansions", 0) + 1,
            } if request_kind == "targeted_expansion" else {}),
            "agent_path": [
                *state.get("agent_path", []),
                f"grader:{request_kind}:{len(validated_grades)}_graded",
            ],
        }

    except Exception as exc:
        logger.exception("relevance_grader_error")
        return _abstain_fallback(state, f"Grader error: {type(exc).__name__}")


def _enforce_loop_guards(
    state: AgentState,
    request_kind: str,
) -> str:
    """Enforce retrieval retry and targeted expansion caps.

    Returns the adjusted request_kind after applying guards.
    """
    retrieval_attempts = state.get("retrieval_attempts", 0)
    targeted_expansions = state.get("targeted_expansions", 0)

    if request_kind == "rewrite" and retrieval_attempts >= MAX_RETRIEVAL_ATTEMPTS:
        logger.info("grader_max_retrieval_reached", attempts=retrieval_attempts)
        return "abstain"

    if request_kind == "targeted_expansion" and targeted_expansions >= MAX_TARGETED_EXPANSIONS:
        logger.info("grader_max_expansion_reached", expansions=targeted_expansions)
        return "abstain"

    # T8-15: Reject unknown request kinds — fail-closed.
    if request_kind not in {"answer", "rewrite", "targeted_expansion", "abstain"}:
        logger.warning("grader_unknown_request_kind", kind=request_kind)
        return "abstain"

    return request_kind


def _abstain_fallback(state: AgentState, reason: str) -> dict[str, Any]:
    """Fallback to abstention on grader failure."""
    return {
        "relevance_grades": [],
        "relevance_request_kind": "abstain",
        "abstention_reason": reason,
        "agent_path": [*state.get("agent_path", []), "grader:abstain_fallback"],
    }
