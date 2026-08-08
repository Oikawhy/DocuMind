"""Document and DocumentVersion ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from documind.models.base import Base, _enum_values
from documind.models.enums import DocumentLifecycle, ExtractionStatus


class Document(Base):
    """Stable logical document record."""

    __tablename__ = "document"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    declared_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("declared_type.id", use_alter=True, name="document_declared_type_fk"),
        nullable=False,
    )
    created_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    current_completed_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_version.id", use_alter=True, name="document_current_version_fk"),
        nullable=True,
    )
    legal_hold_state: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    deletion_requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    erased_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    # Relationships
    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document",
        foreign_keys="DocumentVersion.document_id",
    )

    __table_args__ = (
        CheckConstraint("length(title) >= 1 AND length(title) <= 1024", name="title_length"),
        CheckConstraint(
            "(erased_at IS NULL) OR (current_completed_version_id IS NULL)",
            name="erased_no_current_version",
        ),
    )


class DocumentVersion(Base):
    """Immutable document version with lifecycle state machine."""

    __tablename__ = "document_version"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    detected_mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_mime_family: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quarantine_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    lifecycle: Mapped[DocumentLifecycle] = mapped_column(
        Enum(DocumentLifecycle, name="document_lifecycle", create_type=False, values_callable=_enum_values),
        nullable=False,
        server_default=text("'accepted'"),
    )
    extraction_state: Mapped[ExtractionStatus] = mapped_column(
        Enum(ExtractionStatus, name="extraction_status", create_type=False, values_callable=_enum_values),
        nullable=False,
        server_default=text("'not_requested'"),
    )
    selected_chunk_profile_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "chunk_profile_revision.id",
            use_alter=True,
            name="version_chunk_profile_fk",
        ),
        nullable=False,
    )
    selected_template_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "extraction_template_revision.id",
            use_alter=True,
            name="version_template_fk",
        ),
        nullable=True,
    )
    type_suggestion: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    parser_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalization_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_projection_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tombstone_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_safe_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Relationships
    document: Mapped["Document"] = relationship(
        back_populates="versions",
        foreign_keys=[document_id],
    )

    __table_args__ = (
        CheckConstraint("version_number > 0", name="positive_version_number"),
        CheckConstraint("byte_size >= 0 AND byte_size <= 524288000", name="byte_size_range"),
        UniqueConstraint("document_id", "version_number", name="uq_document_version_number"),
        UniqueConstraint("document_id", "content_sha256", name="uq_document_content_sha256"),
    )
