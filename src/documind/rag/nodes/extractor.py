"""Extractor node per §7.4 — EXTRACT role structured data extraction.

Obtains the active approved extraction template, supplies JSON Schema
and deterministic field dictionary.  Validates JSON, source spans,
units, and evidence IDs.  Returns ``pending_template`` when no active
template exists.
"""

from __future__ import annotations

from typing import Any

import structlog

from documind.rag.state import AgentState, EvidenceCache, StructuredExtraction

logger = structlog.get_logger(__name__)


async def extractor_node(
    state: AgentState,
    *,
    llm_service: Any,
    evidence_cache: EvidenceCache,
    template_loader: Any | None = None,
    prompt_registry: Any | None = None,
) -> dict[str, Any]:
    """Extract structured data using an approved template.

    Only runs for ``extraction`` route type.
    """
    route = state.get("route_type", "simple_qa")

    if route != "extraction":
        return {
            "extraction_results": [],
            "agent_path": [*state.get("agent_path", []), "extractor:skipped"],
        }

    from documind.rag.tools.extract_structured import (
        ExtractStructuredInput,
        extract_structured,
    )

    reranked_ids = state.get("reranked_evidence_ids", [])

    # T8-17: Resolve template_revision_id from plan steps.
    template_revision_id = None
    for step in state.get("plan", []):
        if hasattr(step, "operation") and step.operation == "extract_structured":
            template_revision_id = getattr(step, "template_revision_id", None)
            break
        elif isinstance(step, dict) and step.get("operation") == "extract_structured":
            template_revision_id = step.get("template_revision_id")
            break

    input_data = ExtractStructuredInput(
        template_revision_id=template_revision_id,
        evidence_ids=reranked_ids,
    )

    result = await extract_structured(
        input_data,
        llm_service=llm_service,
        evidence_cache=evidence_cache,
        template_loader=template_loader,
        prompt_registry=prompt_registry,
    )

    extraction = StructuredExtraction(
        template_id=result.extraction.get("template_id", "") if result.extraction else "",
        template_revision=0,
        fields=result.extraction if result.extraction else {},
        evidence_ids=reranked_ids,
        valid=result.valid,
        pending_template=result.pending_template,
    )

    return {
        "extraction_results": [*state.get("extraction_results", []), extraction],
        "agent_path": [
            *state.get("agent_path", []),
            f"extractor:valid={result.valid},pending={result.pending_template}",
        ],
    }
