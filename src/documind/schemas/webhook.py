"""Pydantic contracts for webhook registration and delivery per §9.5."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class WebhookCreateRequest(BaseModel):
    """Register a new webhook subscription.

    ``target_url`` must be HTTPS and pass SSRF validation.
    ``event_type_glob`` uses fnmatch-style patterns
    (e.g. ``document.version.*``).
    ``secret`` is the raw HMAC key; the server stores only a SHA-256
    hash and never returns the secret.
    """

    target_url: str = Field(..., min_length=1, max_length=2048)
    event_type_glob: str = Field(..., min_length=1, max_length=256)
    secret: str = Field(..., min_length=32, max_length=256)

    @field_validator("target_url")
    @classmethod
    def must_be_https(cls, v: str) -> str:
        if not v.startswith("https://"):
            msg = "Webhook target URL must use HTTPS."
            raise ValueError(msg)
        return v


class WebhookResponse(BaseModel):
    """Public webhook representation (secret excluded)."""

    id: uuid.UUID
    target_url: str
    event_type_glob: str
    active: bool
    failure_streak: int
    created_at: datetime


class WebhookDeliveryResponse(BaseModel):
    """Delivery attempt record."""

    id: uuid.UUID
    webhook_id: uuid.UUID
    outbox_event_id: uuid.UUID
    http_status: int | None
    attempt: int
    state: str
    attempted_at: datetime
