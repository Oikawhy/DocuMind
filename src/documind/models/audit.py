"""AuditEvent, AuditEventIdentity, and AuditAnchor ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import PrimaryKeyConstraint, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class AuditEvent(Base):
    """Hash-chained audit event, partitioned by event_time monthly.

    PostgreSQL RANGE partitioning on event_time enables efficient time-bounded
    queries and partition-level retention management.  The composite PK
    (event_time, id) is mandatory for partition routing.
    """

    __tablename__ = "audit_event"

    id: Mapped[uuid.UUID] = mapped_column(nullable=False, default=uuid.uuid4)
    event_time: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    actor_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("event_time", "id"),
        {"postgresql_partition_by": "RANGE (event_time)"},
    )


class AuditEventIdentity(Base):
    """Cross-partition uniqueness guard for audit events."""

    __tablename__ = "audit_event_identity"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    event_time: Mapped[datetime] = mapped_column(nullable=False)


class AuditAnchor(Base):
    """WORM-sealed audit period anchor."""

    __tablename__ = "audit_anchor"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    period_start: Mapped[datetime] = mapped_column(nullable=False)
    period_end: Mapped[datetime] = mapped_column(nullable=False)
    terminal_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (UniqueConstraint("period_start", "period_end", name="uq_audit_anchor_period"),)
