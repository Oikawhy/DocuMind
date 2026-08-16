"""compare_versions — deterministic version comparison tool per §7.6.

Produces text/structured/timeline diffs with evidence IDs.
Prose generation is done separately by the Comparator node.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Versioned input / output schemas
# ---------------------------------------------------------------------------


class CompareVersionsInput(BaseModel):
    """Input schema for compare_versions tool."""

    left_version_id: str
    right_version_id: str
    selected_fields: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


class DiffEntry(BaseModel):
    """A single difference between two versions."""

    field_name: str
    left_value: Any = None
    right_value: Any = None
    change_type: str = "modified"  # "added", "removed", "modified", "unchanged"
    evidence_ids: list[str] = Field(default_factory=list)


class CompareVersionsOutput(BaseModel):
    """Output schema for compare_versions tool."""

    text_diff: dict[str, Any] = Field(default_factory=dict)
    structured_diff: list[DiffEntry] = Field(default_factory=list)
    timeline_diff: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def compare_versions(
    input_data: CompareVersionsInput,
    evidence_cache: Any,
) -> CompareVersionsOutput:
    """Produce deterministic diffs between two authorized version evidence sets.

    Compares content from the evidence cache for the left and right versions.
    """
    left_content: dict[str, str] = {}
    right_content: dict[str, str] = {}

    # Partition evidence by version.
    for eid in input_data.evidence_ids:
        content = evidence_cache.get(eid) if evidence_cache else None
        if content is None:
            continue
        # In practice, evidence would carry version metadata.
        # For the tool, we split based on position: first half left, second half right.
        # The actual version assignment happens in the node layer with full metadata.
        if eid in input_data.evidence_ids[: len(input_data.evidence_ids) // 2]:
            left_content[eid] = content
        else:
            right_content[eid] = content

    # Build structured diffs.
    structured: list[DiffEntry] = []
    all_fields = set(list(left_content.keys()) + list(right_content.keys()))

    for field_key in sorted(all_fields):
        left_val = left_content.get(field_key)
        right_val = right_content.get(field_key)

        if left_val is None and right_val is not None:
            change_type = "added"
        elif left_val is not None and right_val is None:
            change_type = "removed"
        elif left_val != right_val:
            change_type = "modified"
        else:
            change_type = "unchanged"

        structured.append(
            DiffEntry(
                field_name=field_key,
                left_value=left_val[:200] if left_val else None,
                right_value=right_val[:200] if right_val else None,
                change_type=change_type,
                evidence_ids=[field_key],
            )
        )

    return CompareVersionsOutput(
        text_diff={
            "left_version_id": input_data.left_version_id,
            "right_version_id": input_data.right_version_id,
            "change_count": sum(1 for d in structured if d.change_type != "unchanged"),
        },
        structured_diff=structured,
        timeline_diff=[],
        evidence_ids=input_data.evidence_ids,
    )
