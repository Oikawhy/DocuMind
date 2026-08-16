"""extract_structured — schema-aware extraction tool per §7.6.

Reads an active approved extraction template, supplies its JSON Schema
and deterministic field dictionary to the EXTRACT role via ``LLMService``.
Validates returned JSON and source spans.  Returns ``pending_template``
when no active template exists.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Versioned input / output schemas
# ---------------------------------------------------------------------------


class ExtractStructuredInput(BaseModel):
    """Input schema for extract_structured tool."""

    template_revision_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    document_id: str | None = None
    schema_version: str = SCHEMA_VERSION


class ExtractStructuredOutput(BaseModel):
    """Output schema for extract_structured tool."""

    extraction: dict[str, Any] = Field(default_factory=dict)
    pending_template: bool = False
    valid: bool = True
    validation_errors: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def extract_structured(
    input_data: ExtractStructuredInput,
    llm_service: Any,
    evidence_cache: Any,
    template_loader: Any | None = None,
) -> ExtractStructuredOutput:
    """Extract structured data using an approved template and the EXTRACT role.

    If no active template exists, returns ``pending_template=True`` without
    persisting an approved structured result.
    """
    from documind.services.llm_service import ModelRole

    # Load the active template.
    template = None
    if template_loader is not None and input_data.template_revision_id:
        template = await template_loader.load(input_data.template_revision_id)

    if template is None:
        return ExtractStructuredOutput(pending_template=True, valid=False)

    # Gather evidence content from cache.
    evidence_texts: list[str] = []
    for eid in input_data.evidence_ids:
        content = evidence_cache.get(eid) if evidence_cache else None
        if content:
            evidence_texts.append(f"[Evidence {eid}]: {content}")

    if not evidence_texts:
        return ExtractStructuredOutput(
            pending_template=False,
            valid=False,
            validation_errors=["No evidence available for extraction"],
        )

    evidence_block = "\n\n".join(evidence_texts)

    # Build the extraction prompt.
    template_schema = template.get("json_schema", {}) if isinstance(template, dict) else {}
    field_dict = template.get("field_dictionary", {}) if isinstance(template, dict) else {}

    system_prompt = (
        "You are a structured data extractor. Extract data from the provided evidence "
        "according to the JSON Schema below. For each populated field, provide source spans "
        "(exact quotes from the evidence). Return valid JSON matching the schema.\n\n"
        f"JSON Schema:\n```json\n{json.dumps(template_schema, indent=2)}\n```\n\n"
        f"Field Dictionary:\n```json\n{json.dumps(field_dict, indent=2)}\n```"
    )

    user_prompt = f"Extract structured data from this evidence:\n\n{evidence_block}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        result = await llm_service.invoke(
            ModelRole.EXTRACT,
            messages,
            json_schema=template_schema if template_schema else None,
        )

        # Parse the result.
        if result.structured and result.structured.valid:
            parsed = result.structured.parsed
        else:
            try:
                parsed = json.loads(result.content)
            except json.JSONDecodeError:
                return ExtractStructuredOutput(
                    valid=False,
                    validation_errors=["LLM output is not valid JSON"],
                )

        # Validate evidence IDs in source spans.
        source_spans = parsed.get("source_spans", {})
        validation_errors: list[str] = []
        for field_name, spans in source_spans.items():
            if isinstance(spans, list):
                for span in spans:
                    if isinstance(span, dict):
                        ref_eid = span.get("evidence_id", "")
                        if ref_eid and ref_eid not in input_data.evidence_ids:
                            validation_errors.append(
                                f"Field '{field_name}' references unknown evidence ID: {ref_eid}"
                            )

        return ExtractStructuredOutput(
            extraction=parsed,
            pending_template=False,
            valid=len(validation_errors) == 0,
            validation_errors=validation_errors,
        )

    except Exception as exc:
        return ExtractStructuredOutput(
            valid=False,
            validation_errors=[f"Extraction failed: {type(exc).__name__}"],
        )
