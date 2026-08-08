"""Transactional CloudEvents outbox writer and dispatcher claim query."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.domain.errors import InvalidRequestError
from documind.models.outbox import OutboxEvent

_SOURCE = "urn:documind:self-hosted:document-domain"
_FORBIDDEN_EVENT_FIELDS = frozenset(
    {
        "labels",
        "label_ids",
        "allowed_groups",
        "groups",
        "user_identity",
        "storage_credentials",
        "model_route",
        "provider",
        "document_text",
        "raw_extracted_data",
    }
)


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _contains_forbidden_field(value: object) -> bool:
    if isinstance(value, dict):
        return bool(_FORBIDDEN_EVENT_FIELDS.intersection(value)) or any(
            _contains_forbidden_field(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_field(item) for item in value)
    return False


class OutboxService:
    """Write outbox rows inside the caller-owned PostgreSQL transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish_event(
        self,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        data: dict[str, Any],
        *,
        correlation_id: uuid.UUID | None = None,
        subject: str | None = None,
        contract_version: str = "1.0.0",
        event_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> OutboxEvent:
        """Add an immutable CloudEvents 1.0 envelope without committing it."""
        if _contains_forbidden_field(data):
            raise InvalidRequestError("The event payload contains a forbidden field.")

        outbox_id = event_id or uuid.uuid4()
        correlation = correlation_id or uuid.uuid4()
        event_time = occurred_at or datetime.now(UTC)
        cloud_event = {
            "specversion": "1.0",
            "id": str(outbox_id),
            "source": _SOURCE,
            "type": event_type,
            "subject": subject or f"{aggregate_type.replace('_', '-')}/{aggregate_id}",
            "time": event_time.isoformat().replace("+00:00", "Z"),
            "datacontenttype": "application/json",
            "data": data,
        }
        event = OutboxEvent(
            id=outbox_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            contract_version=contract_version,
            cloud_event=cloud_event,
            payload_sha256=hashlib.sha256(_canonical_json(data)).hexdigest(),
            correlation_id=correlation,
            status="pending",
        )
        self._session.add(event)
        return event

    async def claim_pending(self, *, limit: int = 100) -> list[OutboxEvent]:
        """Claim pending rows for Task 4's publisher using SKIP LOCKED."""
        statement = (
            select(OutboxEvent)
            .where(OutboxEvent.status == "pending")
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(statement)
        return list(result.scalars().all())
