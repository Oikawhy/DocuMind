"""Planner node per §7.4 — bounded declarative sub-task planner.

Produces up to MAX_PLAN_STEPS declarative sub-tasks with whitelisted
operation types.  Rejects backend syntax (Cypher/SQL/search DSL),
group IDs, label IDs, and provider settings.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from documind.rag.prompts.safety import wrap_with_safety
from documind.rag.state import MAX_PLAN_STEPS, AgentState, PlanStep
from documind.services.llm_service import ModelRole

logger = structlog.get_logger(__name__)

_WHITELISTED_OPERATIONS = frozenset({
    "resolve_versions", "retrieve_evidence", "extract_structured",
    "compare_versions", "aggregate_values",
})

# Patterns that indicate backend syntax injection.
_FORBIDDEN_PATTERNS = [
    re.compile(r"\b(MATCH|CREATE|MERGE|DELETE|DETACH|RETURN|WHERE)\b", re.IGNORECASE),  # Cypher
    re.compile(r"\b(SELECT|INSERT|UPDATE|DROP|ALTER|FROM|JOIN)\b", re.IGNORECASE),  # SQL
    re.compile(r"\b(bool|must|should|match_all|query_string)\b", re.IGNORECASE),  # Search DSL
    re.compile(r"group_id|label_id|provider_setting", re.IGNORECASE),  # Forbidden fields
]


async def planner_node(
    state: AgentState,
    *,
    llm_service: Any,
    prompt_registry: Any | None = None,
) -> dict[str, Any]:
    """Produce a bounded plan of declarative sub-tasks.

    Only runs for comparison, aggregation, extraction, or complex
    summarization routes.
    """
    route = state.get("route_type", "simple_qa")

    # Planner only runs for complex routes.
    if route not in {"comparison", "aggregation", "extraction", "summarization"}:
        return {
            "plan": [],
            "agent_path": [*state.get("agent_path", []), "planner:skipped"],
        }

    template_text = (
        "Break the question into at most 5 declarative sub-tasks using "
        "whitelisted operations: resolve_versions, retrieve_evidence, "
        "extract_structured, compare_versions, aggregate_values.\n"
        "Return JSON: {\"steps\": [{\"operation\": \"...\", \"description\": \"...\"}]}"
    )
    if prompt_registry is not None:
        try:
            template = prompt_registry.resolve("planner")
            template_text = template.text
        except KeyError:
            pass

    system_prompt = wrap_with_safety(template_text)

    user_prompt = (
        f"Question: {state['original_question']}\n"
        f"Route: {route}\n"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    output_schema = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "maxItems": MAX_PLAN_STEPS,
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string"},
                        "description": {"type": "string"},
                        "document_selector": {"type": "string"},
                        "version_selector": {"type": "string"},
                        "entity_filter": {"type": "string"},
                        "date_filter": {"type": "string"},
                    },
                    "required": ["operation", "description"],
                },
            },
        },
        "required": ["steps"],
    }

    try:
        result = await llm_service.invoke(ModelRole.QUERY, messages, json_schema=output_schema)

        if result.structured and result.structured.valid:
            parsed = result.structured.parsed
        else:
            try:
                parsed = json.loads(result.content)
            except (json.JSONDecodeError, TypeError):
                logger.warning("planner_invalid_json")
                return {"plan": [], "agent_path": [*state.get("agent_path", []), "planner:invalid_json"]}

        raw_steps = parsed.get("steps", [])

        # Validate and filter steps.
        validated_steps: list[PlanStep] = []
        for step_data in raw_steps[:MAX_PLAN_STEPS]:
            operation = step_data.get("operation", "")

            # Reject invalid operations.
            if operation not in _WHITELISTED_OPERATIONS:
                logger.warning("planner_rejected_operation", operation=operation)
                continue

            description = step_data.get("description", "")

            # Reject backend syntax in description.
            if _contains_forbidden_syntax(description):
                logger.warning("planner_rejected_syntax", description=description[:100])
                continue

            # Reject backend syntax in selectors.
            for field_key in ("document_selector", "version_selector", "entity_filter", "date_filter"):
                val = step_data.get(field_key, "")
                if val and _contains_forbidden_syntax(val):
                    logger.warning("planner_rejected_syntax_in_field", field=field_key)
                    step_data[field_key] = None

            validated_steps.append(
                PlanStep(
                    operation=operation,
                    description=description,
                    document_selector=step_data.get("document_selector"),
                    version_selector=step_data.get("version_selector"),
                    entity_filter=step_data.get("entity_filter"),
                    date_filter=step_data.get("date_filter"),
                    value_filter=step_data.get("value_filter"),
                )
            )

        if prompt_registry is not None:
            prompt_registry.record_invocation(
                "planner", 1, ModelRole.QUERY,
                input_valid=True, output_valid=len(validated_steps) > 0,
            )

        return {
            "plan": validated_steps,
            "agent_path": [*state.get("agent_path", []), f"planner:{len(validated_steps)}_steps"],
        }

    except Exception:
        logger.exception("planner_node_error")
        return {"plan": [], "agent_path": [*state.get("agent_path", []), "planner:error"]}


def _contains_forbidden_syntax(text: str) -> bool:
    """Check if text contains forbidden backend syntax patterns."""
    return any(pattern.search(text) for pattern in _FORBIDDEN_PATTERNS)
