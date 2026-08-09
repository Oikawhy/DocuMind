"""Enrichment coordinator: type suggestion, template extraction, proposals, and graph facts.

Loads the pinned template, canonical normalized content, and already-persisted
chunks under a version/tombstone lock.  Performs no retrieval, projection,
label mutation, or lifecycle completion.  All LLM output is non-authoritative.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from documind.services.graph_fact_service import (
    FactPersistenceResult,
    GraphFactValidationError,
    RawFact,
)
from documind.services.llm_service import (
    ModelRole,
    ModelRouteError,
)


class EnrichmentError(RuntimeError):
    """Fatal enrichment failure that should transition the stage to failed."""


@dataclass
class EnrichmentResult:
    """Summary of all enrichment sub-tasks for the stage output."""

    type_suggestion: dict[str, Any] | None = None
    extraction_status: str = "not_requested"
    extraction_id: uuid.UUID | None = None
    proposal_id: uuid.UUID | None = None
    fact_result: FactPersistenceResult | None = None
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Protocols for dependency inversion
# ---------------------------------------------------------------------------


class VersionLoader(Protocol):
    """Load version metadata, chunks, and template revisions."""

    async def load_version(self, version_id: uuid.UUID) -> Any:
        """Load the document version; raise if tombstoned."""

    async def load_chunks(self, version_id: uuid.UUID) -> list[Any]:
        """Load all persisted chunks for the version."""

    async def load_template(self, revision_id: uuid.UUID) -> Any | None:
        """Load the extraction template revision, or None if absent."""

    async def update_type_suggestion(self, version_id: uuid.UUID, suggestion: dict) -> None:
        """Store the non-authoritative type suggestion on the version."""

    async def update_extraction_state(self, version_id: uuid.UUID, state: str) -> None:
        """Update DocumentVersion.extraction_state."""

    async def save_extraction(self, extraction: Any) -> None:
        """Persist a StructuredExtraction row."""

    async def save_proposal(self, proposal: Any) -> None:
        """Persist a TemplateProposal row."""


# ---------------------------------------------------------------------------
# JSON Schema definitions for LLM structured output
# ---------------------------------------------------------------------------

_TYPE_SUGGESTION_SCHEMA: dict[str, Any] = {
    "title": "type_suggestion",
    "type": "object",
    "required": ["suggested_type", "confidence"],
    "properties": {
        "suggested_type": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence_summary": {"type": "string"},
    },
}

_GRAPH_FACTS_SCHEMA: dict[str, Any] = {
    "title": "graph_facts",
    "type": "object",
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "subject_type",
                    "subject_value",
                    "predicate",
                    "confidence",
                    "source_chunk_id",
                ],
                "properties": {
                    "subject_type": {"type": "string"},
                    "subject_value": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object_type": {"type": "string"},
                    "object_value": {"type": "string"},
                    "literal_type": {"type": "string"},
                    "literal_unit": {"type": "string"},
                    "literal_value": {"type": "string"},
                    "confidence": {"type": "number"},
                    "source_chunk_id": {"type": "string"},
                    "evidence_span": {"type": "string"},
                    "conflict_group_key": {"type": "string"},
                },
            },
        },
    },
}

_TEMPLATE_PROPOSAL_SCHEMA: dict[str, Any] = {
    "title": "template_proposal",
    "type": "object",
    "required": ["proposed_type_key", "candidate_schema", "rationale", "sample_spans"],
    "properties": {
        "proposed_type_key": {"type": "string"},
        "candidate_schema": {"type": "object"},
        "rationale": {"type": "string"},
        "sample_spans": {"type": "array"},
    },
}

#: Default proposal expiry: 30 days.
_PROPOSAL_EXPIRY_DAYS = 30


class EnrichmentService:
    """Orchestrate type suggestion, template extraction, and graph fact persistence.

    Does not perform retrieval, projection, label mutation, or lifecycle
    completion.  All LLM output is non-authoritative.
    """

    def __init__(
        self,
        *,
        llm_service: Any,
        graph_fact_service: Any,
        version_loader: VersionLoader,
    ) -> None:
        self._llm = llm_service
        self._gfs = graph_fact_service
        self._loader = version_loader

    async def enrich(
        self,
        *,
        version_id: uuid.UUID,
        route_revision_id: uuid.UUID | None = None,
    ) -> EnrichmentResult:
        """Run all enrichment sub-tasks for a version."""
        result = EnrichmentResult()

        # Resolve route_revision_id from LLM service if not provided
        if route_revision_id is None:
            try:
                route = await self._llm._route_resolver.newest_active(ModelRole.EXTRACT)
                if route is None:
                    result.errors.append("No active EXTRACT route available for enrichment.")
                    return result
                route_revision_id = route.revision_id
            except Exception as exc:
                result.errors.append(f"Route resolution failed: {type(exc).__name__}")
                return result

        # 1. Load version and chunks
        version = await self._loader.load_version(version_id)
        chunks = await self._loader.load_chunks(version_id)
        if not chunks:
            result.errors.append("No chunks found for version.")
            return result

        chunk_ids = {c.id for c in chunks}
        chunk_texts = [c.content for c in chunks]

        # 2. Type suggestion (non-authoritative, non-blocking)
        await self._request_type_suggestion(version_id, route_revision_id, chunk_texts, result)

        # 3. Template extraction or proposal
        template_revision_id = getattr(version, "selected_template_revision_id", None)
        if template_revision_id is not None:
            template = await self._loader.load_template(template_revision_id)
            if template is not None:
                await self._extract_with_template(
                    version_id,
                    route_revision_id,
                    template,
                    chunk_texts,
                    chunks,
                    result,
                )
            else:
                result.extraction_status = "pending_template"
                await self._loader.update_extraction_state(version_id, "pending_template")
        else:
            result.extraction_status = "pending_template"
            await self._loader.update_extraction_state(version_id, "pending_template")
            # Optionally create a template proposal
            await self._create_template_proposal(version_id, route_revision_id, chunk_texts, chunks, result)

        # 4. Graph fact extraction (non-blocking on failure)
        await self._extract_graph_facts(version_id, route_revision_id, chunk_texts, chunk_ids, chunks, result)

        return result

    # ------------------------------------------------------------------
    # Type suggestion
    # ------------------------------------------------------------------

    async def _request_type_suggestion(
        self,
        version_id: uuid.UUID,
        route_revision_id: uuid.UUID,
        chunk_texts: list[str],
        result: EnrichmentResult,
    ) -> None:
        """Request a non-authoritative type suggestion via EXTRACT."""
        combined = "\n---\n".join(chunk_texts[:5])  # First 5 chunks
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a document classification assistant. "
                    "Analyze the document text and suggest a document type. "
                    "Respond with JSON matching the provided schema."
                ),
            },
            {"role": "user", "content": combined},
        ]
        try:
            llm_result = await self._llm.invoke(
                ModelRole.EXTRACT,
                messages,
                json_schema=_TYPE_SUGGESTION_SCHEMA,
            )
            if llm_result.structured and llm_result.structured.valid:
                suggestion = llm_result.structured.parsed
                suggestion["route_revision_id"] = str(route_revision_id)
                suggestion["evidence_hash"] = hashlib.sha256(combined.encode("utf-8")).hexdigest()
                result.type_suggestion = suggestion
                await self._loader.update_type_suggestion(version_id, suggestion)
        except (ModelRouteError, Exception) as exc:
            result.errors.append(f"Type suggestion failed: {type(exc).__name__}")

    # ------------------------------------------------------------------
    # Template extraction
    # ------------------------------------------------------------------

    async def _extract_with_template(
        self,
        version_id: uuid.UUID,
        route_revision_id: uuid.UUID,
        template: Any,
        chunk_texts: list[str],
        chunks: list[Any],
        result: EnrichmentResult,
    ) -> None:
        """Extract structured data against the pinned template's JSON Schema."""
        combined = "\n---\n".join(chunk_texts)
        schema = template.json_schema
        extraction_schema = {
            "title": "template_extraction",
            "type": "object",
            "required": schema.get("required", []),
            "properties": schema.get("properties", {}),
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a document data extraction assistant. "
                    "Extract structured data from the document text according "
                    "to the provided schema. "
                    "Respond with JSON matching the schema exactly."
                ),
            },
            {"role": "user", "content": combined},
        ]
        try:
            llm_result = await self._llm.invoke(
                ModelRole.EXTRACT,
                messages,
                json_schema=extraction_schema,
            )
        except (ModelRouteError, Exception) as exc:
            result.extraction_status = "failed"
            result.errors.append(f"Template extraction LLM failed: {type(exc).__name__}")
            await self._loader.update_extraction_state(version_id, "failed")
            return

        if llm_result.structured and llm_result.structured.valid:
            parsed = llm_result.structured.parsed
            validation_errors = self._validate_against_schema(parsed, schema)
            source_spans = self._build_source_spans(chunks)
            if not validation_errors:
                extraction_id = uuid.uuid4()
                await self._loader.save_extraction(
                    {
                        "id": extraction_id,
                        "version_id": version_id,
                        "template_revision_id": template.id,
                        "model_route_revision_id": route_revision_id,
                        "status": "validated",
                        "data": parsed,
                        "source_spans": source_spans,
                        "validation_errors": [],
                    }
                )
                result.extraction_status = "completed"
                result.extraction_id = extraction_id
                await self._loader.update_extraction_state(version_id, "completed")
            else:
                extraction_id = uuid.uuid4()
                await self._loader.save_extraction(
                    {
                        "id": extraction_id,
                        "version_id": version_id,
                        "template_revision_id": template.id,
                        "model_route_revision_id": route_revision_id,
                        "status": "validation_failed",
                        "data": parsed,
                        "source_spans": source_spans,
                        "validation_errors": validation_errors,
                    }
                )
                result.extraction_status = "validation_failed"
                result.extraction_id = extraction_id
                # Validation failure does not block indexing
                await self._loader.update_extraction_state(version_id, "failed")
        else:
            result.extraction_status = "failed"
            result.errors.append("Template extraction produced invalid structured output.")
            await self._loader.update_extraction_state(version_id, "failed")

    # ------------------------------------------------------------------
    # Template proposal
    # ------------------------------------------------------------------

    async def _create_template_proposal(
        self,
        version_id: uuid.UUID,
        route_revision_id: uuid.UUID,
        chunk_texts: list[str],
        chunks: list[Any],
        result: EnrichmentResult,
    ) -> None:
        """Optionally create a draft template proposal (never auto-activates)."""
        combined = "\n---\n".join(chunk_texts[:5])
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a document schema proposal assistant. "
                    "Analyze the document text and propose a JSON Schema "
                    "template for extracting structured data from similar "
                    "documents. Include a rationale and sample source spans."
                ),
            },
            {"role": "user", "content": combined},
        ]
        try:
            llm_result = await self._llm.invoke(
                ModelRole.EXTRACT,
                messages,
                json_schema=_TEMPLATE_PROPOSAL_SCHEMA,
            )
            if llm_result.structured and llm_result.structured.valid:
                parsed = llm_result.structured.parsed
                proposal_id = uuid.uuid4()
                await self._loader.save_proposal(
                    {
                        "id": proposal_id,
                        "version_id": version_id,
                        "proposed_declared_type_key": parsed.get("proposed_type_key", "unknown"),
                        "candidate_json_schema": parsed.get("candidate_schema", {}),
                        "rationale": parsed.get("rationale", ""),
                        "sample_source_spans": parsed.get("sample_spans", []),
                        "model_route_revision_id": route_revision_id,
                        "state": "draft",
                        "expires_at": datetime.now(UTC) + timedelta(days=_PROPOSAL_EXPIRY_DAYS),
                    }
                )
                result.proposal_id = proposal_id
        except (ModelRouteError, Exception) as exc:
            result.errors.append(f"Template proposal failed: {type(exc).__name__}")

    # ------------------------------------------------------------------
    # Graph fact extraction
    # ------------------------------------------------------------------

    async def _extract_graph_facts(
        self,
        version_id: uuid.UUID,
        route_revision_id: uuid.UUID,
        chunk_texts: list[str],
        chunk_ids: set[uuid.UUID],
        chunks: list[Any],
        result: EnrichmentResult,
    ) -> None:
        """Extract and persist graph facts via GraphFactService."""
        combined = "\n---\n".join(f"[chunk:{c.id}]\n{c.content}" for c in chunks)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a knowledge graph extraction assistant. "
                    "Extract entities, relationships, and facts from the "
                    "document text. Each fact must reference a source_chunk_id "
                    "from the provided chunks. "
                    "Respond with JSON matching the provided schema."
                ),
            },
            {"role": "user", "content": combined},
        ]
        try:
            llm_result = await self._llm.invoke(
                ModelRole.EXTRACT,
                messages,
                json_schema=_GRAPH_FACTS_SCHEMA,
            )
        except (ModelRouteError, Exception) as exc:
            result.errors.append(f"Graph fact extraction failed: {type(exc).__name__}")
            return

        if not (llm_result.structured and llm_result.structured.valid):
            result.errors.append("Graph fact extraction produced invalid structured output.")
            return

        raw_facts_data = llm_result.structured.parsed.get("facts", [])
        raw_facts = self._parse_raw_facts(raw_facts_data)

        if raw_facts:
            try:
                fact_result = await self._gfs.persist_facts(
                    version_id=version_id,
                    route_revision_id=route_revision_id,
                    raw_facts=raw_facts,
                    chunk_ids=chunk_ids,
                )
                result.fact_result = fact_result
            except GraphFactValidationError as exc:
                result.errors.append(f"Graph fact persistence failed: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_raw_facts(facts_data: list[dict[str, Any]]) -> list[RawFact]:
        """Convert LLM output dicts to RawFact dataclasses."""
        raw_facts: list[RawFact] = []
        for fact_dict in facts_data:
            try:
                chunk_id = uuid.UUID(fact_dict["source_chunk_id"])
            except (KeyError, ValueError):
                continue

            has_entity = bool(fact_dict.get("object_type") and fact_dict.get("object_value"))
            has_literal = bool(fact_dict.get("literal_type") and fact_dict.get("literal_value"))

            raw = RawFact(
                subject_entity_type=fact_dict.get("subject_type", ""),
                subject_display_value=fact_dict.get("subject_value", ""),
                predicate_key=fact_dict.get("predicate", ""),
                object_entity_type=(fact_dict.get("object_type") if has_entity else None),
                object_display_value=(fact_dict.get("object_value") if has_entity else None),
                object_literal_type=(fact_dict.get("literal_type") if has_literal else None),
                object_literal_unit=(fact_dict.get("literal_unit") if has_literal else None),
                object_literal_value=(fact_dict.get("literal_value") if has_literal else None),
                source_chunk_id=chunk_id,
                confidence=float(fact_dict.get("confidence", 0.0)),
                evidence_span=fact_dict.get("evidence_span"),
                conflict_group_key=fact_dict.get("conflict_group_key"),
            )
            raw_facts.append(raw)
        return raw_facts

    @staticmethod
    def _validate_against_schema(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
        """Basic Draft 2020-12 JSON Schema validation (required + types)."""
        errors: list[str] = []
        required = schema.get("required", [])
        properties = schema.get("properties", {})

        for key in required:
            if key not in data:
                errors.append(f"Missing required field: {key}")

        for key, prop_schema in properties.items():
            if key in data:
                expected_type = prop_schema.get("type")
                if expected_type and not _schema_type_matches(data[key], expected_type):
                    errors.append(f"Field '{key}' expected type '{expected_type}', got '{type(data[key]).__name__}'")
        return errors

    @staticmethod
    def _build_source_spans(chunks: list[Any]) -> dict[str, Any]:
        """Build source span metadata from chunks."""
        return {
            "chunks": [
                {
                    "chunk_id": str(c.id),
                    "chunk_index": c.chunk_index,
                    "start_offset": c.start_offset,
                    "end_offset": c.end_offset,
                }
                for c in chunks
            ]
        }


def _schema_type_matches(value: Any, json_type: str) -> bool:
    """Check if a Python value matches a JSON Schema type."""
    type_map: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    expected = type_map.get(json_type)
    if expected is None:
        return True
    if json_type == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, expected)
