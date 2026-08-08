"""GraphEntity and GraphFact ORM models matching §8.1."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class GraphEntity(Base):
    """Knowledge-graph entity with type-scoped unique key."""

    __tablename__ = "graph_entity"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_key: Mapped[str] = mapped_column(Text, nullable=False)
    display_value: Mapped[str] = mapped_column(Text, nullable=False)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("graph_entity.id"),
        nullable=True,
    )
    tombstone_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (UniqueConstraint("entity_type", "normalized_key", name="uq_entity_type_key"),)


class GraphFact(Base):
    """Directed triple linking entities with provenance and confidence."""

    __tablename__ = "graph_fact"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_entity.id"),
        nullable=False,
    )
    predicate_key: Mapped[str] = mapped_column(Text, nullable=False)
    object_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("graph_entity.id"),
        nullable=True,
    )
    object_literal: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    object_normalized_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_chunk.id"),
        nullable=False,
    )
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_version.id"),
        nullable=False,
    )
    extraction_route_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_route_revision.id"),
        nullable=False,
    )
    gleaning_pass: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    corroboration_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    conflict_group_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_span: Mapped[str | None] = mapped_column(Text, nullable=True)
    tombstone_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tombstoned_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="valid_confidence"),
        CheckConstraint("gleaning_pass IN (0, 1)", name="valid_gleaning_pass"),
        CheckConstraint(
            "(object_entity_id IS NOT NULL) <> (object_literal IS NOT NULL)",
            name="exactly_one_fact_object",
        ),
        UniqueConstraint(
            "subject_entity_id",
            "predicate_key",
            "object_normalized_key",
            "source_chunk_id",
            "extraction_route_revision_id",
            name="uq_fact_triple_chunk_route",
        ),
    )
