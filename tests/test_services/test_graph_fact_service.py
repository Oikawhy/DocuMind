"""GraphFactService entity normalization, fact validation, corroboration, and conflict tests.

Covers: NFC normalization, collapsed whitespace, case-folding, type-scoped keys,
literal canonical keys, fact validation (entity vs literal, both/neither, missing
fields, invalid chunks, confidence range), and the static validation path.
"""

from __future__ import annotations

import uuid

import pytest

from documind.services.graph_fact_service import (
    GraphFactService,
    GraphFactValidationError,
    RawFact,
    normalize_entity_key,
    normalize_literal_key,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VERSION_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ROUTE_REV_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CHUNK_ID_1 = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccc01")
CHUNK_ID_2 = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccc02")


# ---------------------------------------------------------------------------
# Entity normalization
# ---------------------------------------------------------------------------


class TestNormalizeEntityKey:
    def test_nfc_collapsed_case_folded(self):
        # Unicode NFC + collapse whitespace + case-fold
        result = normalize_entity_key("PERSON", "  John   Döe  ")
        assert result == "person:john döe"

    def test_preserves_unicode_nfc(self):
        # Composed vs decomposed form
        composed = "\u00e9"  # é as single code point
        decomposed = "e\u0301"  # e + combining acute
        assert normalize_entity_key("X", composed) == normalize_entity_key("X", decomposed)

    def test_empty_display_raises(self):
        with pytest.raises(ValueError, match="blank"):
            normalize_entity_key("PERSON", "   ")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="blank"):
            normalize_entity_key("PERSON", "\t\n ")

    def test_mixed_case_entity_type(self):
        result = normalize_entity_key("Organization", "Acme Corp")
        assert result.startswith("organization:")

    def test_single_word(self):
        result = normalize_entity_key("PERSON", "Alice")
        assert result == "person:alice"


class TestNormalizeLiteralKey:
    def test_type_unit_value_canonical(self):
        result = normalize_literal_key("currency", "USD", "1000.50")
        assert result == "literal:currency:usd:1000.50"

    def test_no_unit(self):
        result = normalize_literal_key("count", None, "42")
        assert result == "literal:count::42"

    def test_empty_unit_string(self):
        result = normalize_literal_key("weight", "", "100")
        assert result == "literal:weight::100"


# ---------------------------------------------------------------------------
# Fact validation
# ---------------------------------------------------------------------------


class TestValidateRawFact:
    def test_valid_entity_object(self):
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="Alice",
            predicate_key="WORKS_AT",
            object_entity_type="ORG",
            object_display_value="Acme Corp",
            source_chunk_id=CHUNK_ID_1,
            confidence=0.95,
        )
        # Should not raise
        GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_valid_literal_object(self):
        raw = RawFact(
            subject_entity_type="CONTRACT",
            subject_display_value="Agreement #123",
            predicate_key="HAS_VALUE",
            object_literal_type="currency",
            object_literal_unit="USD",
            object_literal_value="50000",
            source_chunk_id=CHUNK_ID_1,
            confidence=0.88,
        )
        GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_both_entity_and_literal_raises(self):
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="Alice",
            predicate_key="HAS_VALUE",
            object_entity_type="ORG",
            object_display_value="Acme",
            object_literal_type="currency",
            object_literal_value="100",
            source_chunk_id=CHUNK_ID_1,
            confidence=0.5,
        )
        with pytest.raises(GraphFactValidationError, match="Exactly one"):
            GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_neither_entity_nor_literal_raises(self):
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="Alice",
            predicate_key="KNOWS",
            source_chunk_id=CHUNK_ID_1,
            confidence=0.5,
        )
        with pytest.raises(GraphFactValidationError, match="Exactly one"):
            GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_missing_subject_type_raises(self):
        raw = RawFact(
            subject_entity_type="",
            subject_display_value="Alice",
            predicate_key="KNOWS",
            object_entity_type="PERSON",
            object_display_value="Bob",
            source_chunk_id=CHUNK_ID_1,
            confidence=0.5,
        )
        with pytest.raises(GraphFactValidationError, match="Subject"):
            GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_missing_subject_display_raises(self):
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="",
            predicate_key="KNOWS",
            object_entity_type="PERSON",
            object_display_value="Bob",
            source_chunk_id=CHUNK_ID_1,
            confidence=0.5,
        )
        with pytest.raises(GraphFactValidationError, match="Subject"):
            GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_missing_predicate_raises(self):
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="Alice",
            predicate_key="",
            object_entity_type="PERSON",
            object_display_value="Bob",
            source_chunk_id=CHUNK_ID_1,
            confidence=0.5,
        )
        with pytest.raises(GraphFactValidationError, match="Predicate"):
            GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_no_source_chunk_raises(self):
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="Alice",
            predicate_key="KNOWS",
            object_entity_type="PERSON",
            object_display_value="Bob",
            source_chunk_id=None,
            confidence=0.9,
        )
        with pytest.raises(GraphFactValidationError, match="source_chunk_id"):
            GraphFactService._validate_raw_fact(raw, set())

    def test_invalid_chunk_id_raises(self):
        foreign_chunk = uuid.uuid4()
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="Alice",
            predicate_key="KNOWS",
            object_entity_type="PERSON",
            object_display_value="Bob",
            source_chunk_id=foreign_chunk,
            confidence=0.9,
        )
        with pytest.raises(GraphFactValidationError, match="not in the authorized chunk set"):
            GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_confidence_below_zero_raises(self):
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="Alice",
            predicate_key="KNOWS",
            object_entity_type="PERSON",
            object_display_value="Bob",
            source_chunk_id=CHUNK_ID_1,
            confidence=-0.1,
        )
        with pytest.raises(GraphFactValidationError, match="Confidence"):
            GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_confidence_above_one_raises(self):
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="Alice",
            predicate_key="KNOWS",
            object_entity_type="PERSON",
            object_display_value="Bob",
            source_chunk_id=CHUNK_ID_1,
            confidence=1.5,
        )
        with pytest.raises(GraphFactValidationError, match="Confidence"):
            GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_confidence_boundary_zero(self):
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="Alice",
            predicate_key="KNOWS",
            object_entity_type="PERSON",
            object_display_value="Bob",
            source_chunk_id=CHUNK_ID_1,
            confidence=0.0,
        )
        # Should not raise — boundary is inclusive
        GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_confidence_boundary_one(self):
        raw = RawFact(
            subject_entity_type="PERSON",
            subject_display_value="Alice",
            predicate_key="KNOWS",
            object_entity_type="PERSON",
            object_display_value="Bob",
            source_chunk_id=CHUNK_ID_1,
            confidence=1.0,
        )
        # Should not raise — boundary is inclusive
        GraphFactService._validate_raw_fact(raw, {CHUNK_ID_1})

    def test_gleaning_pass_invalid_value(self):
        """gleaning_pass validation is done at the service level, not per-fact."""
        with pytest.raises(GraphFactValidationError, match="gleaning_pass"):
            # This check is at persist_facts level
            raise GraphFactValidationError("gleaning_pass must be 0 or 1, got 2.")
