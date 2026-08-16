"""ProjectionState and ActiveProjectionGeneration ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class ProjectionState(Base):
    """State tracking for search index and graph projections per §8.1.

    ``projection_kind`` is one of ``qdrant``, ``opensearch``, or ``neo4j``.
    ``scope_key`` is the version UUID string for vector/search projections
    or the literal ``global`` for Neo4j.
    """

    __tablename__ = "projection_state"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    projection_kind: Mapped[str] = mapped_column(Text, nullable=False)
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_version.id"),
        nullable=True,
    )
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    state_changed_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "projection_kind IN ('qdrant', 'opensearch', 'neo4j')",
            name="valid_projection_type",
        ),
        CheckConstraint(
            "state IN ('pending', 'writing', 'projected', 'verified', 'unhealthy', 'erased')",
            name="valid_projection_state",
        ),
        CheckConstraint(
            "(projection_kind = 'neo4j' AND scope_key = 'global' AND version_id IS NULL) "
            "OR (projection_kind IN ('qdrant', 'opensearch') "
            "AND version_id IS NOT NULL AND scope_key = version_id::text)",
            name="projection_scope_consistency",
        ),
        UniqueConstraint(
            "projection_kind",
            "scope_key",
            "generation",
            name="uq_projection_version_gen",
        ),
    )


class ActiveProjectionGeneration(Base):
    """Current active generation per (projection_kind, scope_key) pair.

    References the verified ``projection_state`` row via a composite
    foreign key as required by §8.1.
    """

    __tablename__ = "active_projection_generation"

    projection_kind: Mapped[str] = mapped_column(
        Text,
        CheckConstraint(
            "projection_kind IN ('qdrant', 'opensearch', 'neo4j')",
            name="apg_valid_projection_kind",
        ),
        primary_key=True,
    )
    scope_key: Mapped[str] = mapped_column(Text, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    activated_by_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operation.id"),
        nullable=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["projection_kind", "scope_key", "generation"],
            ["projection_state.projection_kind", "projection_state.scope_key", "projection_state.generation"],
            name="fk_apg_projection_state",
        ),
    )
