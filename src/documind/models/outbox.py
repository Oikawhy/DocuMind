"""OutboxEvent and DeadLetter ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class OutboxEvent(Base):
    """Transactional outbox event with CloudEvents payload."""

    __tablename__ = "outbox_event"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    contract_version: Mapped[str] = mapped_column(Text, nullable=False)
    cloud_event: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    redis_stream_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'published', 'failed')",
            name="valid_outbox_status",
        ),
    )


class DeadLetter(Base):
    """Failed processing entry eligible for replay."""

    __tablename__ = "dead_letter"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    processing_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_run.id"),
        nullable=True,
    )
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_stage.id"),
        nullable=True,
    )
    workflow_id: Mapped[str] = mapped_column(Text, nullable=False)
    activity_name: Mapped[str] = mapped_column(Text, nullable=False)
    safe_error_class: Mapped[str] = mapped_column(Text, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    retry_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('open', 'replaying', 'resolved', 'cancelled')",
            name="valid_dead_letter_state",
        ),
    )
