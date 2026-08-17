"""aggregate_values — deterministic data aggregation tool per §7.6.

Performs deterministic sum/avg/min/max/count/group_by over validated
authorized values.  Refuses mixed currencies, incompatible units, and
malformed numeric fields.  Records every input value, evidence ID,
and calculation trace.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"

# Known compatible unit groups.
_COMPATIBLE_UNITS: dict[str, set[str]] = {
    "length": {"m", "cm", "mm", "km", "in", "ft", "yd", "mi"},
    "weight": {"g", "kg", "mg", "lb", "oz", "t"},
    "currency_usd": {"USD", "$"},
    "currency_eur": {"EUR", "€"},
    "currency_gbp": {"GBP", "£"},
    "percentage": {"%", "percent"},
    "count": {"", "units", "items", "count"},
    "time": {"s", "ms", "min", "h", "d"},
}

# T8-23: Conversion factors to base units.
_CONVERSION_TO_BASE: dict[str, tuple[str, Decimal]] = {
    # length → meters
    "km": ("m", Decimal("1000")),
    "cm": ("m", Decimal("0.01")),
    "mm": ("m", Decimal("0.001")),
    "in": ("m", Decimal("0.0254")),
    "ft": ("m", Decimal("0.3048")),
    "yd": ("m", Decimal("0.9144")),
    "mi": ("m", Decimal("1609.344")),
    "m": ("m", Decimal("1")),
    # weight → grams
    "kg": ("g", Decimal("1000")),
    "mg": ("g", Decimal("0.001")),
    "t": ("g", Decimal("1000000")),
    "lb": ("g", Decimal("453.592")),
    "oz": ("g", Decimal("28.3495")),
    "g": ("g", Decimal("1")),
    # time → seconds
    "ms": ("s", Decimal("0.001")),
    "min": ("s", Decimal("60")),
    "h": ("s", Decimal("3600")),
    "d": ("s", Decimal("86400")),
    "s": ("s", Decimal("1")),
}


# ---------------------------------------------------------------------------
# Versioned input / output schemas
# ---------------------------------------------------------------------------


class AggregateValueEntry(BaseModel):
    """A single value to aggregate with its provenance."""

    value: str  # string to handle various numeric formats
    unit: str = ""
    evidence_id: str = ""
    field_name: str = ""


class AggregateValuesInput(BaseModel):
    """Input schema for aggregate_values tool."""

    operation: Literal["sum", "avg", "min", "max", "count", "group_by"]
    values: list[AggregateValueEntry]
    group_by_field: str | None = None
    schema_version: str = SCHEMA_VERSION


class AggregateValuesOutput(BaseModel):
    """Output schema for aggregate_values tool."""

    result: float | int | dict[str, float | int] | None = None
    unit: str | None = None
    calculation_trace: str = ""
    input_count: int = 0
    evidence_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


def _find_unit_group(unit: str) -> str | None:
    """Find the compatibility group for a unit."""
    for group_name, units in _COMPATIBLE_UNITS.items():
        if unit in units:
            return group_name
    return None


def _check_unit_compatibility(units: list[str]) -> str | None:
    """Check that all units are compatible.  Returns an error message or None."""
    if not units:
        return None

    non_empty = [u for u in units if u]
    if not non_empty:
        return None

    groups = set()
    for u in non_empty:
        group = _find_unit_group(u)
        if group is None:
            groups.add(f"unknown:{u}")
        else:
            groups.add(group)

    if len(groups) > 1:
        return f"Incompatible units detected: {', '.join(sorted(groups))}"

    # Check for mixed currencies explicitly.
    currency_groups = {g for g in groups if g.startswith("currency_")}
    if len(currency_groups) > 1:
        return f"Mixed currencies cannot be aggregated: {', '.join(sorted(currency_groups))}"

    return None


async def aggregate_values(input_data: AggregateValuesInput) -> AggregateValuesOutput:
    """Perform deterministic aggregation over validated authorized values.

    Refuses mixed currencies, incompatible units, and malformed numeric
    fields.  Records calculation trace.
    """
    # Validate units.
    units = [v.unit for v in input_data.values]
    unit_error = _check_unit_compatibility(units)
    if unit_error:
        return AggregateValuesOutput(
            error=unit_error,
            input_count=len(input_data.values),
            evidence_ids=[v.evidence_id for v in input_data.values if v.evidence_id],
        )

    # Parse numeric values.
    parsed: list[Decimal] = []
    evidence_ids: list[str] = []
    trace_lines: list[str] = []

    for entry in input_data.values:
        try:
            val = Decimal(entry.value.replace(",", "").strip())
        except (InvalidOperation, ValueError):
            return AggregateValuesOutput(
                error=f"Malformed numeric value: '{entry.value}' (evidence: {entry.evidence_id})",
                input_count=len(input_data.values),
                evidence_ids=[v.evidence_id for v in input_data.values if v.evidence_id],
            )

        # T8-23: Normalize to base unit if conversion exists.
        unit = entry.unit
        if unit and unit in _CONVERSION_TO_BASE:
            base_unit, factor = _CONVERSION_TO_BASE[unit]
            val = val * factor
            unit = base_unit

        parsed.append(val)
        if entry.evidence_id:
            evidence_ids.append(entry.evidence_id)
        trace_lines.append(f"  {entry.field_name or 'value'}: {val} {unit} [{entry.evidence_id}]")

    if not parsed:
        return AggregateValuesOutput(
            error="No values to aggregate",
            input_count=0,
        )

    # Determine the output unit.
    output_unit = next((u for u in units if u), None)

    # Compute result.
    result: float | int | dict[str, float | int] | None = None

    if input_data.operation == "count":
        result = len(parsed)
        trace_lines.insert(0, f"count({len(parsed)} values)")
    elif input_data.operation == "sum":
        total = sum(parsed)
        result = float(total)
        trace_lines.insert(0, f"sum = {total}")
    elif input_data.operation == "avg":
        avg = sum(parsed) / len(parsed)
        result = float(avg)
        trace_lines.insert(0, f"avg = {avg} (n={len(parsed)})")
    elif input_data.operation == "min":
        minimum = min(parsed)
        result = float(minimum)
        trace_lines.insert(0, f"min = {minimum}")
    elif input_data.operation == "max":
        maximum = max(parsed)
        result = float(maximum)
        trace_lines.insert(0, f"max = {maximum}")
    elif input_data.operation == "group_by":
        if not input_data.group_by_field:
            return AggregateValuesOutput(
                error="group_by requires a group_by_field",
                input_count=len(parsed),
                evidence_ids=evidence_ids,
            )
        # Group values by field name.
        groups: dict[str, list[Decimal]] = {}
        for entry, val in zip(input_data.values, parsed, strict=True):
            key = getattr(entry, input_data.group_by_field, entry.field_name) or "other"
            groups.setdefault(key, []).append(val)
        result = {k: float(sum(vs)) for k, vs in groups.items()}
        trace_lines.insert(0, f"group_by({input_data.group_by_field}) = {result}")

    return AggregateValuesOutput(
        result=result,
        unit=output_unit,
        calculation_trace="\n".join(trace_lines),
        input_count=len(parsed),
        evidence_ids=evidence_ids,
    )
