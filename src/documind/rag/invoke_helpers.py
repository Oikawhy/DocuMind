"""Shared LLM invocation helpers for RAG graph nodes.

Provides the retry-then-abstain pattern used by Generator, Hallucination
Grader, and Relevance Grader nodes.  On model output parse failure, a
single silent retry is attempted before returning ``None`` with a
typed ``limitation_code``.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


async def invoke_with_retry(
    llm_service: Any,
    role: Any,
    messages: list[dict[str, str]],
    *,
    output_schema: dict[str, Any],
    max_retries: int = 1,
    template_name: str = "unknown",
) -> tuple[dict[str, Any] | None, str | None]:
    """Invoke an LLM with structured output, retrying once on parse failure.

    Returns ``(parsed_result, None)`` on success, or
    ``(None, limitation_code)`` on exhausted retries.

    The retry is silent — same prompt, no rewrite — to recover from
    transient model output glitches common with smaller self-hosted models.
    """
    limitation_code = f"{template_name.upper()}_SCHEMA_VIOLATION"

    for attempt in range(1 + max_retries):
        try:
            result = await llm_service.invoke(role, messages, json_schema=output_schema)
        except Exception:
            logger.exception(
                "llm_invoke_error",
                template=template_name,
                attempt=attempt + 1,
            )
            if attempt == max_retries:
                return None, limitation_code
            continue

        # Try structured output first.
        if result.structured and result.structured.valid:
            return result.structured.parsed, None

        # Fall back to content parsing.
        try:
            parsed = json.loads(result.content)
            return parsed, None
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "llm_parse_failure",
                template=template_name,
                attempt=attempt + 1,
                max_retries=max_retries,
            )
            if attempt == max_retries:
                return None, limitation_code

    return None, limitation_code
