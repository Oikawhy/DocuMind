"""Webhook and WebhookDelivery ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class Webhook(Base):
    """Webhook subscription for document events."""

    __tablename__ = "webhook"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    event_type_glob: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    failure_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class WebhookDelivery(Base):
    """Individual delivery attempt for a webhook event."""

    __tablename__ = "webhook_delivery"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook.id"),
        nullable=False,
    )
    outbox_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outbox_event.id"),
        nullable=False,
    )
    http_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    response_body_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'delivered', 'failed', 'exhausted')",
            name="valid_delivery_state",
        ),
    )
