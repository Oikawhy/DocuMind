"""Aggregator node per §7.4 — deterministic numeric aggregation.

Validates field types and units, performs sum/avg/min/max/count/group_by.
Refuses mixed currencies, incompatible units, and malformed fields.
Records input values, evidence IDs, and calculation trace.
"""

from __future__ import annotations

from typing import Any

import structlog

from documind.rag.state import AgentState, AggregationResult, EvidenceCache

logger = structlog.get_logger(__name__)


async def aggregator_node(
    state: AgentState,
    *,
    evidence_cache: EvidenceCache,
) -> dict[str, Any]:
    """Perform deterministic numeric aggregation.

    Only runs for ``aggregation`` route type.
    """
    route = state.get("route_type", "simple_qa")

    if route != "aggregation":
        return {
            "aggregation_result": None,
            "agent_path": [*state.get("agent_path", []), "aggregator:skipped"],
        }

    from documind.rag.tools.aggregate_values import (
        AggregateValueEntry,
        AggregateValuesInput,
        aggregate_values,
    )

    # Extract numeric values from evidence cache.
    reranked_ids = state.get("reranked_evidence_ids", [])
    values: list[AggregateValueEntry] = []

    for eid in reranked_ids:
        content = evidence_cache.get(eid)
        if content:
            # Simple numeric extraction — in production this would use
            # structured extraction results.
            extracted = _extract_numeric_values(content, eid)
            values.extend(extracted)

    if not values:
        return {
            "aggregation_result": None,
            "abstention_reason": "No numeric values found for aggregation",
            "agent_path": [*state.get("agent_path", []), "aggregator:no_values"],
        }

    # Determine operation from plan or default to sum.
    operation = "sum"
    plan = state.get("plan", [])
    for step in plan:
        if step.operation == "aggregate_values":
            # Try to infer operation from description.
            desc = step.description.lower()
            for op in ("avg", "average", "mean"):
                if op in desc:
                    operation = "avg"
                    break
            for op in ("count",):
                if op in desc:
                    operation = "count"
                    break
            for op in ("min", "minimum"):
                if op in desc:
                    operation = "min"
                    break
            for op in ("max", "maximum"):
                if op in desc:
                    operation = "max"
                    break
            break

    input_data = AggregateValuesInput(operation=operation, values=values)

    result = await aggregate_values(input_data)

    if result.error:
        return {
            "aggregation_result": None,
            "abstention_reason": f"Aggregation error: {result.error}",
            "agent_path": [*state.get("agent_path", []), f"aggregator:error:{result.error[:50]}"],
        }

    aggregation = AggregationResult(
        operation=operation,
        field_name="value",
        result=result.result,
        unit=result.unit,
        input_values=[{"value": v.value, "unit": v.unit, "evidence_id": v.evidence_id} for v in values],
        evidence_ids=result.evidence_ids,
        calculation_trace=result.calculation_trace,
    )

    return {
        "aggregation_result": aggregation,
        "agent_path": [
            *state.get("agent_path", []),
            f"aggregator:{operation}={result.result}",
        ],
    }


def _extract_numeric_values(content: str, evidence_id: str) -> list:
    """Extract numeric values with basic unit detection from evidence content.

    This is a simplified extractor — in production, structured extraction
    results would supply the values.
    """
    import re

    from documind.rag.tools.aggregate_values import AggregateValueEntry

    entries: list[AggregateValueEntry] = []

    # Pattern: number optionally followed by unit.
    pattern = re.compile(
        r"(?:[$€£])?([\d,]+(?:\.\d+)?)\s*"
        r"(USD|EUR|GBP|[$€£%]|kg|g|mg|lb|oz|m|cm|km|mi|units?|items?)?",
    )

    for match in pattern.finditer(content):
        value_str = match.group(1).replace(",", "")
        unit = match.group(2) or ""

        # Normalize currency symbols.
        if "$" in content[:match.start() + 5]:
            unit = unit or "USD"
        if "€" in content[:match.start() + 5]:
            unit = unit or "EUR"
        if "£" in content[:match.start() + 5]:
            unit = unit or "GBP"

        try:
            float(value_str)
        except ValueError:
            continue

        entries.append(
            AggregateValueEntry(
                value=value_str,
                unit=unit,
                evidence_id=evidence_id,
                field_name="value",
            )
        )

    return entries
