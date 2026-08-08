"""Transactional outbox and CloudEvents contract tests."""

from __future__ import annotations

import hashlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from documind.domain.errors import InvalidRequestError
from documind.services.outbox_service import OutboxService


async def test_publish_event_adds_immutable_cloudevent_to_callers_session() -> None:
    session = MagicMock()
    version_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    service = OutboxService(session)

    event = await service.publish_event(
        aggregate_type="document_version",
        aggregate_id=version_id,
        event_type="io.documind.document-version.accepted.v1",
        data={"version_id": str(version_id), "contract_version": "1.0.0"},
        correlation_id=correlation_id,
        subject=f"document-version/{version_id}",
    )

    session.add.assert_called_once_with(event)
    assert event.status == "pending"
    assert event.contract_version == "1.0.0"
    assert event.cloud_event["specversion"] == "1.0"
    assert event.cloud_event["source"] == "urn:documind:self-hosted:document-domain"
    assert event.cloud_event["subject"] == f"document-version/{version_id}"
    canonical = json.dumps(event.cloud_event["data"], sort_keys=True, separators=(",", ":")).encode()
    assert event.payload_sha256 == hashlib.sha256(canonical).hexdigest()


async def test_publish_event_rejects_policy_or_identity_data() -> None:
    service = OutboxService(MagicMock())

    with pytest.raises(InvalidRequestError):
        await service.publish_event(
            aggregate_type="document_version",
            aggregate_id=uuid.uuid4(),
            event_type="io.documind.document-version.accepted.v1",
            data={"labels": ["secret"]},
        )


async def test_claim_pending_uses_skip_locked() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    service = OutboxService(session)

    assert await service.claim_pending(limit=20) == []

    statement = session.execute.call_args.args[0]
    rendered = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in rendered
    assert "SKIP LOCKED" in rendered
    assert "LIMIT 20" in rendered
