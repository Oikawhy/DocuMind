"""Canonical PostgreSQL graph-fact persistence with entity normalization.

Entities are upserted by type-scoped normalized key. Facts use the physical
identity constraint (subject, predicate, object_normalized_key, source_chunk,
extraction_route_revision). Corroboration increments only for the same
normalized fact from a different source. At most one gleaning_pass=1 is
allowed over the same authorized chunks.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.graph import GraphEntity, GraphFact


class GraphFactValidationError(ValueError):
    """A raw fact from the model is malformed or references invalid spans."""


@dataclass(frozen=True)
class RawFact:
    """Model-output fact before normalization and persistence.

    Exactly one of the entity-object branch or the literal-object branch
    must be populated.
    """

    subject_entity_type: str
    subject_display_value: str
    predicate_key: str
    # Entity-object branch
    object_entity_type: str | None = None
    object_display_value: str | None = None
    # Literal-object branch
    object_literal_type: str | None = None
    object_literal_unit: str | None = None
    object_literal_value: str | None = None
    # Source provenance
    source_chunk_id: uuid.UUID | None = None
    confidence: float = 0.0
    evidence_span: str | None = None
    conflict_group_key: str | None = None


@dataclass
class FactPersistenceResult:
    """Summary of what the persist_facts call accomplished."""

    entities_created: int = 0
    entities_reused: int = 0
    facts_created: int = 0
    facts_corroborated: int = 0
    facts_conflicted: int = 0
    facts_skipped: int = 0


def normalize_entity_key(entity_type: str, display_value: str) -> str:
    """NFC-normalize, collapse whitespace, case-fold, and scope by type.

    Returns ``"<entity_type_lower>:<folded_display>"``.
    Raises ``ValueError`` when the display value is blank after normalization.
    """
    nfc = unicodedata.normalize("NFC", display_value)
    collapsed = re.sub(r"\s+", " ", nfc).strip()
    if not collapsed:
        raise ValueError("Display value is blank after normalization.")
    folded = collapsed.casefold()
    return f"{entity_type.casefold()}:{folded}"


def normalize_literal_key(literal_type: str, unit: str | None, value: str) -> str:
    """Canonical key for literal-object facts: ``literal:<type>:<unit>:<value>``."""
    unit_part = (unit or "").casefold()
    return f"literal:{literal_type.casefold()}:{unit_part}:{value}"


class GraphFactService:
    """Normalize, validate, and persist graph entities and facts."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def persist_facts(
        self,
        *,
        version_id: uuid.UUID,
        route_revision_id: uuid.UUID,
        raw_facts: list[RawFact],
        chunk_ids: set[uuid.UUID],
        gleaning_pass: int = 0,
    ) -> FactPersistenceResult:
        """Validate, normalize, and persist facts in a single transaction.

        - ``chunk_ids``: the set of authorized chunk UUIDs for provenance
          validation.
        - ``gleaning_pass``: must be 0 or 1; at most one pass=1 over the same
          chunks per version+route.
        """
        if gleaning_pass not in (0, 1):
            raise GraphFactValidationError(f"gleaning_pass must be 0 or 1, got {gleaning_pass}.")

        result = FactPersistenceResult()

        async with self._session_factory() as session, session.begin():
            # Enforce one-pass gleaning limit
            if gleaning_pass == 1:
                existing_gleaning = (
                    await session.execute(
                        select(GraphFact.id)
                        .where(
                            GraphFact.source_version_id == version_id,
                            GraphFact.extraction_route_revision_id == route_revision_id,
                            GraphFact.gleaning_pass == 1,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing_gleaning is not None:
                    raise GraphFactValidationError("A gleaning_pass=1 already exists for this version and route.")

            for raw in raw_facts:
                try:
                    self._validate_raw_fact(raw, chunk_ids)
                except GraphFactValidationError:
                    result.facts_skipped += 1
                    continue

                # Upsert subject entity
                subj_key = normalize_entity_key(raw.subject_entity_type, raw.subject_display_value)
                subj_id, subj_created = await self._upsert_entity(
                    session,
                    raw.subject_entity_type,
                    subj_key,
                    raw.subject_display_value,
                )
                if subj_created:
                    result.entities_created += 1
                else:
                    result.entities_reused += 1

                # Determine object side
                obj_entity_id: uuid.UUID | None = None
                obj_literal: dict[str, object] | None = None
                obj_normalized_key: str

                if raw.object_entity_type and raw.object_display_value:
                    obj_key = normalize_entity_key(raw.object_entity_type, raw.object_display_value)
                    obj_entity_id, obj_created = await self._upsert_entity(
                        session,
                        raw.object_entity_type,
                        obj_key,
                        raw.object_display_value,
                    )
                    if obj_created:
                        result.entities_created += 1
                    else:
                        result.entities_reused += 1
                    obj_normalized_key = obj_key
                else:
                    obj_literal = {
                        "type": raw.object_literal_type,
                        "unit": raw.object_literal_unit,
                        "value": raw.object_literal_value,
                    }
                    obj_normalized_key = normalize_literal_key(
                        raw.object_literal_type or "",
                        raw.object_literal_unit,
                        raw.object_literal_value or "",
                    )

                # Check for existing fact (idempotent retry or corroboration)
                existing = await self._find_existing_fact(
                    session,
                    subj_id,
                    raw.predicate_key,
                    obj_normalized_key,
                    raw.source_chunk_id,  # type: ignore[arg-type]
                    route_revision_id,
                )

                if existing is not None:
                    if existing.source_version_id != version_id:
                        # Different source version — increment corroboration
                        existing.corroboration_count += 1
                        result.facts_corroborated += 1
                    elif existing.conflict_group_key and raw.conflict_group_key:
                        result.facts_conflicted += 1
                    else:
                        # Same version, same chunk, same route — idempotent retry
                        result.facts_skipped += 1
                    continue

                fact = GraphFact(
                    id=uuid.uuid4(),
                    subject_entity_id=subj_id,
                    predicate_key=raw.predicate_key,
                    object_entity_id=obj_entity_id,
                    object_literal=obj_literal,
                    object_normalized_key=obj_normalized_key,
                    source_chunk_id=raw.source_chunk_id,  # type: ignore[arg-type]
                    source_version_id=version_id,
                    extraction_route_revision_id=route_revision_id,
                    gleaning_pass=gleaning_pass,
                    confidence=Decimal(str(round(raw.confidence, 3))),
                    corroboration_count=1,
                    conflict_group_key=raw.conflict_group_key,
                    evidence_span=raw.evidence_span,
                )
                session.add(fact)
                result.facts_created += 1

        return result

    @staticmethod
    def _validate_raw_fact(raw: RawFact, chunk_ids: set[uuid.UUID]) -> None:
        """Validate a raw fact before normalization."""
        if not raw.subject_entity_type or not raw.subject_display_value:
            raise GraphFactValidationError("Subject entity type and display value are required.")
        if not raw.predicate_key:
            raise GraphFactValidationError("Predicate key is required.")

        has_entity = bool(raw.object_entity_type and raw.object_display_value)
        has_literal = raw.object_literal_type is not None and raw.object_literal_value is not None
        if not (has_entity ^ has_literal):
            raise GraphFactValidationError("Exactly one of object_entity or object_literal must be set.")

        if raw.source_chunk_id is None:
            raise GraphFactValidationError("source_chunk_id is required.")
        if raw.source_chunk_id not in chunk_ids:
            raise GraphFactValidationError(f"source_chunk_id {raw.source_chunk_id} is not in the authorized chunk set.")

        if not (0.0 <= raw.confidence <= 1.0):
            raise GraphFactValidationError(f"Confidence must be between 0 and 1, got {raw.confidence}.")

    async def _upsert_entity(
        self,
        session: AsyncSession,
        entity_type: str,
        normalized_key: str,
        display_value: str,
    ) -> tuple[uuid.UUID, bool]:
        """Upsert by (entity_type, normalized_key). Returns (id, created)."""
        existing = (
            await session.execute(
                select(GraphEntity).where(
                    GraphEntity.entity_type == entity_type,
                    GraphEntity.normalized_key == normalized_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id, False
        nfc_display = unicodedata.normalize("NFC", display_value)
        collapsed_display = re.sub(r"\s+", " ", nfc_display).strip()
        entity = GraphEntity(
            id=uuid.uuid4(),
            entity_type=entity_type,
            normalized_key=normalized_key,
            display_value=collapsed_display,
        )
        session.add(entity)
        await session.flush()
        return entity.id, True

    async def _find_existing_fact(
        self,
        session: AsyncSession,
        subject_entity_id: uuid.UUID,
        predicate_key: str,
        object_normalized_key: str,
        source_chunk_id: uuid.UUID,
        extraction_route_revision_id: uuid.UUID,
    ) -> GraphFact | None:
        """Find an existing fact by the unique constraint columns."""
        return (
            await session.execute(
                select(GraphFact).where(
                    GraphFact.subject_entity_id == subject_entity_id,
                    GraphFact.predicate_key == predicate_key,
                    GraphFact.object_normalized_key == object_normalized_key,
                    GraphFact.source_chunk_id == source_chunk_id,
                    GraphFact.extraction_route_revision_id == extraction_route_revision_id,
                )
            )
        ).scalar_one_or_none()
