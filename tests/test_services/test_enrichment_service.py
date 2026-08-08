"""EnrichmentService coordinator tests.

Covers: type suggestions, active-template extraction success/failure,
no-template pending/proposal behavior, graph fact delegation, invalid
extraction never creates facts, and non-authoritative type suggestion safety.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from documind.services.enrichment_service import EnrichmentService
from documind.services.graph_fact_service import FactPersistenceResult, RawFact
from documind.services.llm_service import (
    LLMResult,
    ModelRole,
    ModelRouteError,
    StructuredOutputResult,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

VERSION_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ROUTE_REV_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TEMPLATE_REV_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
CHUNK_ID_1 = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccc01")
CHUNK_ID_2 = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccc02")


@dataclass
class FakeChunk:
    id: uuid.UUID
    content: str
    chunk_index: int
    start_offset: int
    end_offset: int


@dataclass
class FakeVersion:
    id: uuid.UUID = VERSION_ID
    document_id: uuid.UUID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    selected_template_revision_id: uuid.UUID | None = None
    type_suggestion: dict | None = None
    extraction_state: str = "not_requested"
    tombstone_generation: int = 0
    lifecycle: str = "processing"


@dataclass
class FakeTemplateRevision:
    id: uuid.UUID = TEMPLATE_REV_ID
    json_schema: dict = field(
        default_factory=lambda: {
            "type": "object",
            "required": ["title", "amount"],
            "properties": {
                "title": {"type": "string"},
                "amount": {"type": "number"},
            },
        }
    )
    field_dictionary: dict = field(
        default_factory=lambda: {
            "title": {"description": "Document title"},
            "amount": {"description": "Total amount"},
        }
    )
    status: str = "active"
    declared_type_id: uuid.UUID = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")


class FakeLLMService:
    """Records calls and returns configured responses by schema title."""

    def __init__(
        self,
        responses: dict[str, LLMResult] | None = None,
    ) -> None:
        self.responses: dict[str, LLMResult] = responses or {}
        self.calls: list[tuple[ModelRole, list[dict], dict | None]] = []

    async def invoke(
        self,
        role: ModelRole,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        self.calls.append((role, messages, json_schema))
        key = json_schema.get("title", "default") if json_schema else "default"
        if key in self.responses:
            return self.responses[key]
        return LLMResult(
            content="{}",
            input_tokens=10,
            output_tokens=5,
            route_revision_id=ROUTE_REV_ID,
            model_digest="sha256:test",
            model_alias="test-model",
            latency_ms=100.0,
        )


class FakeGraphFactService:
    """Records persist_facts calls and returns configured results."""

    def __init__(self, result: FactPersistenceResult | None = None) -> None:
        self.result = result or FactPersistenceResult()
        self.calls: list[dict] = []

    async def persist_facts(
        self,
        *,
        version_id: uuid.UUID,
        route_revision_id: uuid.UUID,
        raw_facts: list[RawFact],
        chunk_ids: set[uuid.UUID],
        gleaning_pass: int = 0,
    ) -> FactPersistenceResult:
        self.calls.append(
            {
                "version_id": version_id,
                "route_revision_id": route_revision_id,
                "raw_facts": raw_facts,
                "chunk_ids": chunk_ids,
                "gleaning_pass": gleaning_pass,
            }
        )
        return self.result


class FakeVersionLoader:
    """Loads a fake version and its chunks."""

    def __init__(
        self,
        version: FakeVersion | None = None,
        chunks: list[FakeChunk] | None = None,
        template: FakeTemplateRevision | None = None,
    ) -> None:
        self.version = version or FakeVersion()
        self.chunks = (
            chunks
            if chunks is not None
            else [
                FakeChunk(
                    id=CHUNK_ID_1,
                    content="Chunk one text.",
                    chunk_index=0,
                    start_offset=0,
                    end_offset=15,
                ),
                FakeChunk(
                    id=CHUNK_ID_2,
                    content="Chunk two text.",
                    chunk_index=1,
                    start_offset=15,
                    end_offset=30,
                ),
            ]
        )
        self.template = template
        self.saved_extractions: list[dict] = []
        self.saved_proposals: list[dict] = []

    async def load_version(self, version_id: uuid.UUID) -> FakeVersion:
        return self.version

    async def load_chunks(self, version_id: uuid.UUID) -> list[FakeChunk]:
        return self.chunks

    async def load_template(self, revision_id: uuid.UUID) -> FakeTemplateRevision | None:
        return self.template

    async def update_type_suggestion(self, version_id: uuid.UUID, suggestion: dict) -> None:
        self.version.type_suggestion = suggestion

    async def update_extraction_state(self, version_id: uuid.UUID, state: str) -> None:
        self.version.extraction_state = state

    async def save_extraction(self, extraction: Any) -> None:
        self.saved_extractions.append(extraction)

    async def save_proposal(self, proposal: Any) -> None:
        self.saved_proposals.append(proposal)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_result(
    parsed: dict[str, Any],
    *,
    valid: bool = True,
) -> LLMResult:
    return LLMResult(
        content=json.dumps(parsed),
        input_tokens=50,
        output_tokens=20,
        route_revision_id=ROUTE_REV_ID,
        model_digest="sha256:test",
        model_alias="test-model",
        latency_ms=150.0,
        structured=StructuredOutputResult(parsed=parsed, valid=valid),
    )


# ---------------------------------------------------------------------------
# Type suggestion tests
# ---------------------------------------------------------------------------


class TestEnrichmentTypeSuggestion:
    @pytest.mark.asyncio
    async def test_type_suggestion_stored_as_display_metadata(self):
        suggestion_data = {
            "suggested_type": "invoice",
            "confidence": 0.92,
            "evidence_summary": "Contains line items and totals",
        }
        llm = FakeLLMService(responses={"type_suggestion": _make_llm_result(suggestion_data)})
        loader = FakeVersionLoader()
        gfs = FakeGraphFactService()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert loader.version.type_suggestion is not None
        assert loader.version.type_suggestion["suggested_type"] == "invoice"
        assert loader.version.type_suggestion["confidence"] == 0.92
        assert loader.version.type_suggestion["route_revision_id"] == str(ROUTE_REV_ID)
        assert "evidence_hash" in loader.version.type_suggestion

    @pytest.mark.asyncio
    async def test_type_suggestion_llm_failure_is_non_blocking(self):
        """LLM failure for type suggestion should not block enrichment."""

        class FailingLLM:
            calls: list = []

            async def invoke(self, role, messages, *, json_schema=None):
                self.calls.append((role, messages, json_schema))
                key = json_schema.get("title", "default") if json_schema else "default"
                if key == "type_suggestion":
                    raise ModelRouteError("No route available.")
                return _make_llm_result({})

        loader = FakeVersionLoader()
        gfs = FakeGraphFactService()
        service = EnrichmentService(
            llm_service=FailingLLM(),
            graph_fact_service=gfs,
            version_loader=loader,
        )

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        # Type suggestion failed but enrichment completed
        assert loader.version.type_suggestion is None
        assert any("Type suggestion failed" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Template extraction tests
# ---------------------------------------------------------------------------


class TestEnrichmentTemplateExtraction:
    @pytest.mark.asyncio
    async def test_active_template_validated_extraction(self):
        template = FakeTemplateRevision()
        version = FakeVersion(selected_template_revision_id=TEMPLATE_REV_ID)
        extraction_data = {"title": "Invoice #42", "amount": 1500.00}
        llm = FakeLLMService(responses={"template_extraction": _make_llm_result(extraction_data)})
        loader = FakeVersionLoader(version=version, template=template)
        gfs = FakeGraphFactService()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert result.extraction_status == "completed"
        assert result.extraction_id is not None
        assert len(loader.saved_extractions) == 1
        assert loader.saved_extractions[0]["status"] == "validated"
        assert loader.saved_extractions[0]["data"] == extraction_data

    @pytest.mark.asyncio
    async def test_active_template_validation_failure_nonblocking(self):
        """Validation failure creates evidence but does not block indexing."""
        template = FakeTemplateRevision()
        version = FakeVersion(selected_template_revision_id=TEMPLATE_REV_ID)
        # Missing required "amount" field
        extraction_data = {"title": "Invoice #42"}
        llm = FakeLLMService(responses={"template_extraction": _make_llm_result(extraction_data)})
        loader = FakeVersionLoader(version=version, template=template)
        gfs = FakeGraphFactService()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert result.extraction_status == "validation_failed"
        assert len(loader.saved_extractions) == 1
        assert loader.saved_extractions[0]["status"] == "validation_failed"
        assert len(loader.saved_extractions[0]["validation_errors"]) > 0

    @pytest.mark.asyncio
    async def test_active_template_wrong_type_validation_failure(self):
        """Wrong field type creates validation evidence."""
        template = FakeTemplateRevision()
        version = FakeVersion(selected_template_revision_id=TEMPLATE_REV_ID)
        extraction_data = {"title": "Invoice #42", "amount": "not-a-number"}
        llm = FakeLLMService(responses={"template_extraction": _make_llm_result(extraction_data)})
        loader = FakeVersionLoader(version=version, template=template)
        gfs = FakeGraphFactService()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert result.extraction_status == "validation_failed"

    @pytest.mark.asyncio
    async def test_active_template_llm_failure(self):
        """LLM failure during extraction sets status to failed."""

        class FailingLLM:
            calls: list = []

            async def invoke(self, role, messages, *, json_schema=None):
                self.calls.append((role, messages, json_schema))
                key = json_schema.get("title", "default") if json_schema else "default"
                if key == "template_extraction":
                    raise ModelRouteError("Structured output exhausted.")
                return _make_llm_result({})

        template = FakeTemplateRevision()
        version = FakeVersion(selected_template_revision_id=TEMPLATE_REV_ID)
        loader = FakeVersionLoader(version=version, template=template)
        gfs = FakeGraphFactService()
        service = EnrichmentService(
            llm_service=FailingLLM(),
            graph_fact_service=gfs,
            version_loader=loader,
        )

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert result.extraction_status == "failed"
        assert any("Template extraction LLM failed" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_active_template_invalid_structured_output(self):
        """Invalid structured output from LLM sets status to failed."""
        template = FakeTemplateRevision()
        version = FakeVersion(selected_template_revision_id=TEMPLATE_REV_ID)
        llm = FakeLLMService(responses={"template_extraction": _make_llm_result({}, valid=False)})
        loader = FakeVersionLoader(version=version, template=template)
        gfs = FakeGraphFactService()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert result.extraction_status == "failed"


# ---------------------------------------------------------------------------
# No template tests
# ---------------------------------------------------------------------------


class TestEnrichmentNoTemplate:
    @pytest.mark.asyncio
    async def test_no_template_sets_pending(self):
        version = FakeVersion(selected_template_revision_id=None)
        llm = FakeLLMService()
        loader = FakeVersionLoader(version=version, template=None)
        gfs = FakeGraphFactService()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert result.extraction_status == "pending_template"
        assert loader.version.extraction_state == "pending_template"

    @pytest.mark.asyncio
    async def test_no_template_may_create_proposal(self):
        version = FakeVersion(selected_template_revision_id=None)
        proposal_data = {
            "proposed_type_key": "invoice",
            "candidate_schema": {
                "type": "object",
                "properties": {"total": {"type": "number"}},
            },
            "rationale": "Detected invoice-like structure",
            "sample_spans": [{"chunk_id": str(CHUNK_ID_1), "text": "Total: $100"}],
        }
        llm = FakeLLMService(responses={"template_proposal": _make_llm_result(proposal_data)})
        loader = FakeVersionLoader(version=version, template=None)
        gfs = FakeGraphFactService()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert result.extraction_status == "pending_template"
        assert result.proposal_id is not None
        assert len(loader.saved_proposals) == 1
        assert loader.saved_proposals[0]["state"] == "draft"
        assert "expires_at" in loader.saved_proposals[0]

    @pytest.mark.asyncio
    async def test_no_template_proposal_failure_is_non_blocking(self):
        """Proposal creation failure should not block enrichment."""

        class FailingLLM:
            calls: list = []

            async def invoke(self, role, messages, *, json_schema=None):
                self.calls.append((role, messages, json_schema))
                key = json_schema.get("title", "default") if json_schema else "default"
                if key == "template_proposal":
                    raise ModelRouteError("No route.")
                return _make_llm_result({})

        version = FakeVersion(selected_template_revision_id=None)
        loader = FakeVersionLoader(version=version, template=None)
        gfs = FakeGraphFactService()
        service = EnrichmentService(
            llm_service=FailingLLM(),
            graph_fact_service=gfs,
            version_loader=loader,
        )

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert result.extraction_status == "pending_template"
        assert result.proposal_id is None


# ---------------------------------------------------------------------------
# Graph fact tests
# ---------------------------------------------------------------------------


class TestEnrichmentGraphFacts:
    @pytest.mark.asyncio
    async def test_graph_facts_delegated_to_service(self):
        gfs = FakeGraphFactService(result=FactPersistenceResult(entities_created=2, facts_created=1))
        fact_data = {
            "facts": [
                {
                    "subject_type": "PERSON",
                    "subject_value": "Alice",
                    "predicate": "WORKS_AT",
                    "object_type": "ORG",
                    "object_value": "Acme Corp",
                    "confidence": 0.95,
                    "source_chunk_id": str(CHUNK_ID_1),
                }
            ]
        }
        llm = FakeLLMService(responses={"graph_facts": _make_llm_result(fact_data)})
        loader = FakeVersionLoader()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert len(gfs.calls) == 1
        assert gfs.calls[0]["version_id"] == VERSION_ID
        assert gfs.calls[0]["route_revision_id"] == ROUTE_REV_ID
        assert len(gfs.calls[0]["raw_facts"]) == 1
        assert result.fact_result is not None
        assert result.fact_result.entities_created == 2
        assert result.fact_result.facts_created == 1

    @pytest.mark.asyncio
    async def test_invalid_extraction_never_creates_graph_facts(self):
        """Invalid extraction output creates validation evidence only."""
        gfs = FakeGraphFactService()

        class FailingLLM:
            calls: list = []

            async def invoke(self, role, messages, *, json_schema=None):
                self.calls.append((role, messages, json_schema))
                key = json_schema.get("title", "default") if json_schema else "default"
                if key == "graph_facts":
                    raise ModelRouteError("Structured output exhausted after repair for role EXTRACT.")
                return _make_llm_result({})

        loader = FakeVersionLoader()
        service = EnrichmentService(
            llm_service=FailingLLM(),
            graph_fact_service=gfs,
            version_loader=loader,
        )

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        # Graph facts should NOT have been persisted
        assert len(gfs.calls) == 0
        assert any("Graph fact extraction failed" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_graph_facts_invalid_structured_output(self):
        """Invalid structured output skips graph fact persistence."""
        gfs = FakeGraphFactService()
        llm = FakeLLMService(responses={"graph_facts": _make_llm_result({}, valid=False)})
        loader = FakeVersionLoader()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert len(gfs.calls) == 0
        assert any("invalid structured output" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_graph_facts_with_literal_objects(self):
        gfs = FakeGraphFactService(result=FactPersistenceResult(entities_created=1, facts_created=1))
        fact_data = {
            "facts": [
                {
                    "subject_type": "CONTRACT",
                    "subject_value": "Agreement #123",
                    "predicate": "HAS_VALUE",
                    "literal_type": "currency",
                    "literal_unit": "USD",
                    "literal_value": "50000",
                    "confidence": 0.88,
                    "source_chunk_id": str(CHUNK_ID_1),
                }
            ]
        }
        llm = FakeLLMService(responses={"graph_facts": _make_llm_result(fact_data)})
        loader = FakeVersionLoader()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert len(gfs.calls) == 1
        raw_fact = gfs.calls[0]["raw_facts"][0]
        assert raw_fact.object_literal_type == "currency"
        assert raw_fact.object_literal_value == "50000"

    @pytest.mark.asyncio
    async def test_graph_facts_invalid_chunk_id_skipped(self):
        """Facts with unparseable chunk IDs are skipped during parsing."""
        gfs = FakeGraphFactService()
        fact_data = {
            "facts": [
                {
                    "subject_type": "PERSON",
                    "subject_value": "Alice",
                    "predicate": "KNOWS",
                    "object_type": "PERSON",
                    "object_value": "Bob",
                    "confidence": 0.9,
                    "source_chunk_id": "not-a-valid-uuid",
                }
            ]
        }
        llm = FakeLLMService(responses={"graph_facts": _make_llm_result(fact_data)})
        loader = FakeVersionLoader()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        # No facts parsed due to invalid chunk ID
        assert len(gfs.calls) == 0


# ---------------------------------------------------------------------------
# Non-authoritative safety test
# ---------------------------------------------------------------------------


class TestEnrichmentNonAuthoritative:
    @pytest.mark.asyncio
    async def test_type_suggestion_cannot_alter_lifecycle(self):
        """Type suggestion is display metadata only — does not change lifecycle."""
        suggestion_data = {"suggested_type": "contract", "confidence": 0.99}
        llm = FakeLLMService(responses={"type_suggestion": _make_llm_result(suggestion_data)})
        loader = FakeVersionLoader()
        gfs = FakeGraphFactService()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert loader.version.type_suggestion is not None
        # Lifecycle remains unchanged
        assert loader.version.lifecycle == "processing"


# ---------------------------------------------------------------------------
# No chunks test
# ---------------------------------------------------------------------------


class TestEnrichmentNoChunks:
    @pytest.mark.asyncio
    async def test_no_chunks_returns_early(self):
        loader = FakeVersionLoader(chunks=[])
        gfs = FakeGraphFactService()
        llm = FakeLLMService()
        service = EnrichmentService(llm_service=llm, graph_fact_service=gfs, version_loader=loader)

        result = await service.enrich(version_id=VERSION_ID, route_revision_id=ROUTE_REV_ID)

        assert any("No chunks" in e for e in result.errors)
        assert len(llm.calls) == 0


# ---------------------------------------------------------------------------
# Schema validation helper tests
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_validate_missing_required_field(self):
        errors = EnrichmentService._validate_against_schema(
            {"title": "Test"},
            {
                "required": ["title", "amount"],
                "properties": {
                    "title": {"type": "string"},
                    "amount": {"type": "number"},
                },
            },
        )
        assert len(errors) == 1
        assert "amount" in errors[0]

    def test_validate_wrong_type(self):
        errors = EnrichmentService._validate_against_schema(
            {"title": "Test", "amount": "not-a-number"},
            {
                "required": ["title", "amount"],
                "properties": {
                    "title": {"type": "string"},
                    "amount": {"type": "number"},
                },
            },
        )
        assert len(errors) == 1
        assert "amount" in errors[0]

    def test_validate_all_correct(self):
        errors = EnrichmentService._validate_against_schema(
            {"title": "Test", "amount": 42.0},
            {
                "required": ["title", "amount"],
                "properties": {
                    "title": {"type": "string"},
                    "amount": {"type": "number"},
                },
            },
        )
        assert len(errors) == 0

    def test_validate_extra_fields_allowed(self):
        errors = EnrichmentService._validate_against_schema(
            {"title": "Test", "amount": 10, "extra": "ok"},
            {
                "required": ["title", "amount"],
                "properties": {
                    "title": {"type": "string"},
                    "amount": {"type": "number"},
                },
            },
        )
        assert len(errors) == 0


# ---------------------------------------------------------------------------
# Raw fact parsing tests
# ---------------------------------------------------------------------------


class TestParseRawFacts:
    def test_parse_entity_fact(self):
        facts = EnrichmentService._parse_raw_facts(
            [
                {
                    "subject_type": "PERSON",
                    "subject_value": "Alice",
                    "predicate": "WORKS_AT",
                    "object_type": "ORG",
                    "object_value": "Acme",
                    "confidence": 0.9,
                    "source_chunk_id": str(CHUNK_ID_1),
                }
            ]
        )
        assert len(facts) == 1
        assert facts[0].object_entity_type == "ORG"
        assert facts[0].object_literal_type is None

    def test_parse_literal_fact(self):
        facts = EnrichmentService._parse_raw_facts(
            [
                {
                    "subject_type": "CONTRACT",
                    "subject_value": "A#1",
                    "predicate": "VALUE",
                    "literal_type": "currency",
                    "literal_value": "1000",
                    "confidence": 0.8,
                    "source_chunk_id": str(CHUNK_ID_1),
                }
            ]
        )
        assert len(facts) == 1
        assert facts[0].object_literal_type == "currency"
        assert facts[0].object_entity_type is None

    def test_parse_skips_invalid_uuid(self):
        facts = EnrichmentService._parse_raw_facts(
            [
                {
                    "subject_type": "PERSON",
                    "subject_value": "Alice",
                    "predicate": "KNOWS",
                    "object_type": "PERSON",
                    "object_value": "Bob",
                    "confidence": 0.9,
                    "source_chunk_id": "invalid",
                }
            ]
        )
        assert len(facts) == 0

    def test_parse_skips_missing_chunk_id(self):
        facts = EnrichmentService._parse_raw_facts(
            [
                {
                    "subject_type": "PERSON",
                    "subject_value": "Alice",
                    "predicate": "KNOWS",
                    "object_type": "PERSON",
                    "object_value": "Bob",
                    "confidence": 0.9,
                }
            ]
        )
        assert len(facts) == 0
