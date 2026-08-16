"""Query Rewriter node per §7.4 — KEYWORDS role structured hint generation.

Produces up to MAX_REWRITTEN_QUERIES query variants (max MAX_QUERY_CHARS
each), entity/date/amount hints, and coreference resolution from
bounded session context.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from documind.rag.prompts.safety import wrap_with_safety
from documind.rag.state import MAX_QUERY_CHARS, MAX_REWRITTEN_QUERIES, AgentState, QueryHints
from documind.services.llm_service import ModelRole

logger = structlog.get_logger(__name__)


async def query_rewriter_node(
    state: AgentState,
    *,
    llm_service: Any,
    prompt_registry: Any | None = None,
) -> dict[str, Any]:
    """Produce localized query variants and structured hints.

    Uses the KEYWORDS role.  Resolves coreference, normalizes dates
    and quantities, and extracts entity hints.
    """
    template_text = (
        "Rewrite the question into up to 3 alternative formulations "
        "(max 512 chars each). Extract entity, date, and amount hints. "
        "Return JSON: {\"queries\": [...], \"hints\": {\"entities\": [], "
        "\"dates\": [], \"amounts\": []}}"
    )
    if prompt_registry is not None:
        try:
            template = prompt_registry.resolve("query_rewriter")
            template_text = template.text
        except KeyError:
            pass

    system_prompt = wrap_with_safety(template_text)

    # Build user prompt with bounded session context.
    user_parts = [f"Question: {state['original_question']}"]
    if state.get("session_summary"):
        user_parts.append(f"Session summary: {state['session_summary']}")
    if state.get("chat_history"):
        # Include only the last few messages for coreference resolution.
        recent = state["chat_history"][-5:]
        context = "; ".join(f"{m.get('role', '?')}: {m.get('content', '')[:200]}" for m in recent)
        user_parts.append(f"Recent context: {context}")
    user_parts.append(f"Locale: {state.get('locale', 'en')}")
    user_prompt = "\n".join(user_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    output_schema = {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "maxItems": MAX_REWRITTEN_QUERIES,
                "items": {"type": "string", "maxLength": MAX_QUERY_CHARS},
            },
            "hints": {
                "type": "object",
                "properties": {
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "dates": {"type": "array", "items": {"type": "string"}},
                    "amounts": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "required": ["queries"],
    }

    try:
        result = await llm_service.invoke(ModelRole.KEYWORDS, messages, json_schema=output_schema)

        if result.structured and result.structured.valid:
            parsed = result.structured.parsed
        else:
            try:
                parsed = json.loads(result.content)
            except (json.JSONDecodeError, TypeError):
                # Fallback: use the original question as the only variant.
                logger.warning("query_rewriter_invalid_json")
                return {
                    "rewritten_queries": [state["original_question"][:MAX_QUERY_CHARS]],
                    "query_hints": QueryHints(),
                    "agent_path": [*state.get("agent_path", []), "rewriter:fallback"],
                }

        # Enforce bounds on queries.
        raw_queries = parsed.get("queries", [])
        queries = [q[:MAX_QUERY_CHARS] for q in raw_queries[:MAX_REWRITTEN_QUERIES] if isinstance(q, str) and q.strip()]

        if not queries:
            queries = [state["original_question"][:MAX_QUERY_CHARS]]

        # Parse hints.
        hints_data = parsed.get("hints", {})
        hints = QueryHints(
            entities=hints_data.get("entities", [])[:20] if isinstance(hints_data, dict) else [],
            dates=hints_data.get("dates", [])[:10] if isinstance(hints_data, dict) else [],
            amounts=hints_data.get("amounts", [])[:10] if isinstance(hints_data, dict) else [],
            locale=state.get("locale", "en"),
        )

        if prompt_registry is not None:
            prompt_registry.record_invocation(
                "query_rewriter", 1, ModelRole.KEYWORDS,
                input_valid=True, output_valid=True,
            )

        return {
            "rewritten_queries": queries,
            "query_hints": hints,
            "agent_path": [*state.get("agent_path", []), f"rewriter:{len(queries)}_variants"],
        }

    except Exception:
        logger.exception("query_rewriter_node_error")
        return {
            "rewritten_queries": [state["original_question"][:MAX_QUERY_CHARS]],
            "query_hints": QueryHints(),
            "agent_path": [*state.get("agent_path", []), "rewriter:error"],
        }
