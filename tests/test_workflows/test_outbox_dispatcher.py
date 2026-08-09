"""Unit tests for the Redis Streams outbox handoff and Temporal deduplication."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from documind.workflows.maintenance.outbox_dispatcher import (
    OutboxDispatcher,
    RedisStreamWorkflowRunner,
    TemporalWorkflowConsumer,
)


@dataclass
class _OutboxEvent:
    id: uuid.UUID
    cloud_event: dict[str, Any]
    status: str = "pending"
    redis_stream_id: str | None = None
    publish_attempts: int = 0
    published_at: datetime | None = None


class _Publisher:
    def __init__(self, events: list[_OutboxEvent]) -> None:
        self._events = events

    async def claim_pending(self, *, limit: int) -> list[_OutboxEvent]:
        return self._events[:limit]


class _Redis:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, str]]] = []
        self.claimed: set[str] = set()

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self.published.append((stream, fields))
        return "1720000000000-0"

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    async def delete(self, key: str) -> None:
        self.claimed.discard(key)


class _StreamRedis(_Redis):
    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__()
        self._entries = [("1720000000000-1", {"event": json.dumps(event)})]
        self.groups: list[tuple[str, str, str, bool]] = []
        self.acknowledged: list[tuple[str, str, tuple[str, ...]]] = []

    async def xgroup_create(self, name: str, groupname: str, id: str, *, mkstream: bool) -> None:
        self.groups.append((name, groupname, id, mkstream))

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        *,
        count: int,
        block: int,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        entries, self._entries = self._entries[:count], self._entries[count:]
        return [("documind:outbox", entries)] if entries else []

    async def xack(self, name: str, groupname: str, *ids: str) -> None:
        self.acknowledged.append((name, groupname, ids))


class _Temporal:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start_workflow(self, workflow: object, payload: object, **kwargs: Any) -> None:
        self.calls.append({"workflow": workflow, "payload": payload, **kwargs})


def _cloud_event(version_id: uuid.UUID) -> dict[str, Any]:
    return {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "type": "io.documind.document-version.accepted.v1",
        "subject": f"document-version/{version_id}",
        "data": {
            "version_id": str(version_id),
            "content_sha256": "a" * 64,
            "correlation_id": str(uuid.uuid4()),
        },
    }


async def test_dispatcher_publishes_stored_cloudevent_and_records_stream_id() -> None:
    event = _OutboxEvent(id=uuid.uuid4(), cloud_event=_cloud_event(uuid.uuid4()))
    redis = _Redis()
    dispatcher = OutboxDispatcher(publisher=_Publisher([event]), redis_client=redis)

    assert await dispatcher.dispatch_once() == 1
    assert event.status == "published"
    assert event.redis_stream_id == "1720000000000-0"
    assert event.publish_attempts == 1
    assert event.published_at is not None
    assert redis.published[0][0] == "documind:outbox"


async def test_consumer_deduplicates_cloudevent_before_starting_temporal_workflow() -> None:
    version_id = uuid.uuid4()
    event = _cloud_event(version_id)
    redis = _Redis()
    temporal = _Temporal()
    consumer = TemporalWorkflowConsumer(redis_client=redis, temporal_client=temporal)

    assert await consumer.consume(event) is True
    assert await consumer.consume(event) is False
    assert len(temporal.calls) == 1
    assert temporal.calls[0]["id"] == f"document-version/{version_id}"
    assert temporal.calls[0]["task_queue"] == "ingest-cpu"


async def test_stream_runner_reads_group_entries_and_acks_after_temporal_start() -> None:
    version_id = uuid.uuid4()
    redis = _StreamRedis(_cloud_event(version_id))
    temporal = _Temporal()
    runner = RedisStreamWorkflowRunner(
        redis_client=redis,
        consumer=TemporalWorkflowConsumer(redis_client=redis, temporal_client=temporal),
        consumer_name="test-consumer",
        block_ms=1,
    )

    assert await runner.run_once() == 1
    assert redis.groups == [("documind:outbox", "documind-temporal-starters", "0-0", True)]
    assert redis.acknowledged == [("documind:outbox", "documind-temporal-starters", ("1720000000000-1",))]
    assert len(temporal.calls) == 1
