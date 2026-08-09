"""ExtractionTemplateRevision and TemplateProposal ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base, _enum_values
from documind.models.enums import PolicyStatus


class ExtractionTemplateRevision(Base):
    """Versioned extraction template with JSON Schema."""

    __tablename__ = "extraction_template_revision"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    template_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    declared_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("declared_type.id"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PolicyStatus] = mapped_column(
        Enum(PolicyStatus, name="policy_status", create_type=False, values_callable=_enum_values),
        nullable=False,
    )
    json_schema: Mapped[dict] = mapped_column(JSONB, nullable=False)
    field_dictionary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    schema_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (UniqueConstraint("template_id", "revision", name="uq_template_revision"),)


class TemplateProposal(Base):
    """Model-suggested extraction template awaiting review."""

    __tablename__ = "template_proposal"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_version.id"),
        nullable=False,
    )
    proposed_declared_type_key: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_json_schema: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    sample_source_spans: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model_route_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_route_revision.id", use_alter=True, name="proposal_model_route_fk"),
        nullable=False,
    )
    state: Mapped[PolicyStatus] = mapped_column(
        Enum(PolicyStatus, name="policy_status", create_type=False, values_callable=_enum_values),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    reviewed_by_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (UniqueConstraint("version_id", name="uq_template_proposal_version"),)
