"""PolicyRevision, DeclaredType, and ChunkProfileRevision ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base, _enum_values
from documind.models.enums import PolicyStatus


class PolicyRevision(Base):
    """Versioned, immutable policy record."""

    __tablename__ = "policy_revision"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    policy_kind: Mapped[str] = mapped_column(Text, nullable=False)
    stable_key: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PolicyStatus] = mapped_column(
        Enum(PolicyStatus, name="policy_status", create_type=False, values_callable=_enum_values),
        nullable=False,
    )
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    activated_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint("revision > 0", name="positive_revision"),
        UniqueConstraint("policy_kind", "stable_key", "revision", name="uq_policy_revision"),
    )


class DeclaredType(Base):
    """Document type declaration with active policy binding."""

    __tablename__ = "declared_type"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    stable_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    active_policy_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_revision.id"),
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class ChunkProfileRevision(Base):
    """Versioned chunk profile configuration."""

    __tablename__ = "chunk_profile_revision"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PolicyStatus] = mapped_column(
        Enum(PolicyStatus, name="policy_status", create_type=False, values_callable=_enum_values),
        nullable=False,
    )
    configuration: Mapped[dict] = mapped_column(JSONB, nullable=False)
    configuration_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (UniqueConstraint("profile_id", "revision", name="uq_chunk_profile_revision"),)
