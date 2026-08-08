"""Label, DocumentLabel, LegalHold, and DeletionTombstone ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class Label(Base):
    """Hierarchical label for document classification."""

    __tablename__ = "label"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    stable_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("label.id"),
        nullable=True,
    )
    retention_class: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class DocumentLabel(Base):
    """Many-to-many association between documents and labels."""

    __tablename__ = "document_label"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id"),
        primary_key=True,
    )
    label_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("label.id"),
        primary_key=True,
    )
    assignment_policy_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "policy_revision.id",
            use_alter=True,
            name="label_assignment_policy_fk",
        ),
        nullable=False,
    )
    assigned_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class LegalHold(Base):
    """Legal hold on a document or version (mutually exclusive scope)."""

    __tablename__ = "legal_hold"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document.id"),
        nullable=True,
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_version.id"),
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    imposed_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    imposed_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    released_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(document_id IS NOT NULL) <> (version_id IS NOT NULL)",
            name="hold_scope_xor",
        ),
    )


class DeletionTombstone(Base):
    """Irreversible deletion evidence record (WORM-like)."""

    __tablename__ = "deletion_tombstone"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document.id"), nullable=False)
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_version.id"),
        nullable=True,
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    tombstone_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    sealed_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "scope IN ('document', 'version', 'chat', 'export')",
            name="valid_scope",
        ),
        UniqueConstraint(
            "document_id",
            "version_id",
            "tombstone_generation",
            name="uq_tombstone_generation",
            postgresql_nulls_not_distinct=True,
        ),
    )
