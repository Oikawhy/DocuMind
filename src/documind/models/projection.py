"""ProjectionState and ActiveProjectionGeneration ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class ProjectionState(Base):
    """State tracking for search index and graph projections."""

    __tablename__ = "projection_state"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    projection_type: Mapped[str] = mapped_column(Text, nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_version.id"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "projection_type IN ('qdrant', 'opensearch', 'neo4j')",
            name="valid_projection_type",
        ),
        CheckConstraint(
            "state IN ('pending', 'projected', 'retracted', 'failed')",
            name="valid_projection_state",
        ),
        UniqueConstraint(
            "projection_type",
            "version_id",
            "generation",
            name="uq_projection_version_gen",
        ),
    )


class ActiveProjectionGeneration(Base):
    """Current active generation per projection type (materialised bookkeeping)."""

    __tablename__ = "active_projection_generation"

    projection_type: Mapped[str] = mapped_column(Text, primary_key=True)
    active_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
