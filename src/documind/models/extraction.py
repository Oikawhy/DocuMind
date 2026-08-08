"""StructuredExtraction ORM model matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class StructuredExtraction(Base):
    """Structured data extracted from a document version."""

    __tablename__ = "structured_extraction"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_version.id"),
        nullable=False,
    )
    template_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_template_revision.id"),
        nullable=False,
    )
    model_route_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_route_revision.id"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_spans: Mapped[dict] = mapped_column(JSONB, nullable=False)
    validation_errors: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "status IN ('validated', 'validation_failed', 'erased')",
            name="valid_extraction_status",
        ),
        UniqueConstraint("version_id", "template_revision_id", name="uq_extraction_version_template"),
    )
