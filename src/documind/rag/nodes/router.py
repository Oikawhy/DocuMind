"""Router node per §7.4 — classifies query intent using KEYWORDS role.

Classifies into: simple_qa, comparison, aggregation, extraction,
summarization, clarification, or out_of_scope.  Invalid output defaults
to clarification.  Low confidence (<0.55) triggers hybrid retrieval
if policy permits, else clarification.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from documind.rag.prompts.safety import wrap_with_safety
from documind.rag.state import AgentState
from documind.services.llm_service import ModelRole

logger = structlog.get_logger(__name__)

_VALID_ROUTES = frozenset({
    "simple_qa", "comparison", "aggregation", "extraction",
    "summarization", "clarification", "out_of_scope",
})

_LOW_CONFIDENCE_THRESHOLD = 0.55


async def router_node(
    state: AgentState,
    *,
    llm_service: Any,
    prompt_registry: Any | None = None,
) -> dict[str, Any]:
    """Classify the user's question into a route type.

    Uses the KEYWORDS role.  On invalid output, defaults to clarification.
    On low confidence (<0.55), checks retrieval policy for hybrid mode.
    """
    # Resolve the prompt template.
    template_text = (
        "Classify this question into one of: simple_qa, comparison, "
        "aggregation, extraction, summarization, clarification, out_of_scope.\n"
        "Return JSON: {\"route\": \"...\", \"confidence\": 0.0-1.0, "
        "\"clarification_topic\": \"optional\"}"
    )
    if prompt_registry is not None:
        try:
            template = prompt_registry.resolve("router")
            template_text = template.text
        except KeyError:
            pass

    system_prompt = wrap_with_safety(template_text)

    # Build the user prompt.
    user_parts = [f"Question: {state['original_question']}"]
    if state.get("session_summary"):
        user_parts.append(f"Session context: {state['session_summary']}")
    user_parts.append(f"Locale: {state.get('locale', 'en')}")
    user_prompt = "\n".join(user_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Output JSON Schema for structured output.
    output_schema = {
        "type": "object",
        "properties": {
            "route": {"type": "string", "enum": list(_VALID_ROUTES)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "clarification_topic": {"type": "string"},
        },
        "required": ["route", "confidence"],
    }

    try:
        result = await llm_service.invoke(ModelRole.KEYWORDS, messages, json_schema=output_schema)

        # Parse the structured output.
        if result.structured and result.structured.valid:
            parsed = result.structured.parsed
        else:
            try:
                parsed = json.loads(result.content)
            except (json.JSONDecodeError, TypeError):
                logger.warning("router_invalid_json", content=result.content[:100])
                return _clarification_fallback(state, "Invalid router output — could not parse JSON")

        # Validate the route.
        route = parsed.get("route", "")
        if route not in _VALID_ROUTES:
            logger.warning("router_invalid_route", route=route)
            return _clarification_fallback(state, f"Invalid route: {route}")

        confidence = float(parsed.get("confidence", 0.0))
        clarification_topic = parsed.get("clarification_topic")

        # Record prompt revision.
        if prompt_registry is not None:
            prompt_registry.record_invocation(
                "router", 1, ModelRole.KEYWORDS,
                input_valid=True, output_valid=True,
            )

        # Low confidence handling.
        if confidence < _LOW_CONFIDENCE_THRESHOLD:
            # Check if hybrid retrieval is enabled in the policy.
            retrieval_policy = state.get("retrieval_policy_revision", 0)
            if retrieval_policy > 0:
                # Hybrid retrieval available — proceed but mark as low confidence.
                return {
                    "route_type": route,
                    "route_confidence": confidence,
                    "agent_path": [*state.get("agent_path", []), "router:low_confidence_hybrid"],
                }
            # No hybrid — clarify.
            return _clarification_fallback(
                state,
                clarification_topic or "Low confidence classification — please clarify your question",
            )

        return {
            "route_type": route,
            "route_confidence": confidence,
            "agent_path": [*state.get("agent_path", []), f"router:{route}"],
        }

    except Exception as exc:
        logger.exception("router_node_error")
        return _clarification_fallback(state, f"Router error: {type(exc).__name__}")


def _clarification_fallback(state: AgentState, reason: str) -> dict[str, Any]:
    """Fallback to clarification when the router fails or has invalid output."""
    return {
        "route_type": "clarification",
        "route_confidence": 0.0,
        "abstention_reason": reason,
        "agent_path": [*state.get("agent_path", []), "router:clarification_fallback"],
    }
