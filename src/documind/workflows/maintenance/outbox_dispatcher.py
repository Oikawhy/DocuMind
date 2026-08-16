"""Transactional-outbox publication and CloudEvents-to-Temporal consumer."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from temporalio.exceptions import WorkflowAlreadyStartedError

from documind.services.outbox_service import OutboxService
from documind.workflows.document_version import (
    DocumentVersionWorkflow,
    DocumentVersionWorkflowInput,
    workflow_id_for,
)

_DOCUMENT_VERSION_ACCEPTED = "io.documind.document-version.accepted.v1"
_DEFAULT_STREAM = "documind:outbox"


class OutboxPublisher(Protocol):
    """Outbox claim boundary; OutboxService performs the SKIP LOCKED query."""

    async def claim_pending(self, *, limit: int) -> list[Any]:
        """Return rows claimed with ``FOR UPDATE SKIP LOCKED``."""


class RedisStreamsClient(Protocol):
    """Subset of redis-py's asynchronous Streams and dedupe API."""

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        """Append a CloudEvents envelope to a Redis Stream."""

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool | None:
        """Create a TTL-backed deduplication key only when absent."""

    async def delete(self, key: str) -> Any:
        """Remove a reservation when a workflow could not be started."""

    async def xgroup_create(self, name: str, groupname: str, id: str, *, mkstream: bool) -> Any:
        """Create a consumer group, raising when it already exists."""

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        *,
        count: int,
        block: int,
    ) -> list[Any]:
        """Read pending work for one named consumer."""

    async def xack(self, name: str, groupname: str, *ids: str) -> Any:
        """Acknowledge a message only after its workflow start outcome is durable."""


class TemporalStarter(Protocol):
    """Subset of Temporal client required by the consumer."""

    async def start_workflow(self, workflow: object, payload: object, **kwargs: Any) -> object:
        """Start the one workflow associated with an immutable version."""


class WorkflowRunRecorder(Protocol):
    """Persist the concrete Temporal run ID after a consumer starts it."""

    async def record_workflow_start(self, version_id: str, temporal_run_id: str) -> None:
        """Link the admitted processing run to Temporal's concrete execution."""


class OutboxDispatcher:
    """Publish stored CloudEvents and record the Redis Stream acknowledgement."""

    def __init__(
        self,
        *,
        redis_client: RedisStreamsClient,
        publisher: OutboxPublisher | None = None,
        session_factory: Any | None = None,
        stream_name: str = _DEFAULT_STREAM,
    ) -> None:
        if publisher is None and session_factory is None:
            raise ValueError("publisher or session_factory is required")
        self._publisher = publisher
        self._session_factory = session_factory
        self._redis = redis_client
        self._stream_name = stream_name

    async def dispatch_once(self, *, limit: int = 100) -> int:
        """Publish a bounded set of locked pending rows and retain failures for retry."""
        if self._publisher is not None:
            return await self._dispatch_from(self._publisher, limit=limit)
        async with self._session_factory() as session, session.begin():
            return await self._dispatch_from(OutboxService(session), limit=limit)

    async def _dispatch_from(self, publisher: OutboxPublisher, *, limit: int) -> int:
        published = 0
        for event in await publisher.claim_pending(limit=limit):
            event.publish_attempts += 1
            try:
                stream_id = await self._redis.xadd(
                    self._stream_name,
                    {"event": json.dumps(event.cloud_event, sort_keys=True, separators=(",", ":"))},
                )
            except (ConnectionError, OSError, TimeoutError):
                # Keep pending so the locked-row poll retries until Redis has
                # acknowledged a stream ID.  The downstream consumer dedupes.
                event.status = "pending"
                continue
            event.redis_stream_id = _as_text(stream_id)
            event.status = "published"
            event.published_at = datetime.now(UTC)
            published += 1
        return published


class LifecycleChecker(Protocol):
    """Check whether a version is still eligible for processing."""

    async def assert_active(self, version_id: str) -> None:
        """Raise when the version was tombstoned or otherwise stopped."""


class TemporalWorkflowConsumer:
    """Deduplicate CloudEvents IDs and start one deterministic Temporal workflow."""

    def __init__(
        self,
        *,
        redis_client: RedisStreamsClient,
        temporal_client: TemporalStarter,
        run_recorder: WorkflowRunRecorder | None = None,
        lifecycle_checker: LifecycleChecker | None = None,
        workflow_task_queue: str = "ingest-cpu",
        dedupe_ttl_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        self._redis = redis_client
        self._temporal = temporal_client
        self._workflow_task_queue = workflow_task_queue
        self._dedupe_ttl_seconds = dedupe_ttl_seconds
        self._run_recorder = run_recorder
        self._lifecycle_checker = lifecycle_checker

    async def consume(self, cloud_event: dict[str, Any]) -> bool:
        """Start the workflow once; duplicate Redis delivery is a successful no-op."""
        # T4-4: Validate CloudEvent envelope integrity.
        if cloud_event.get("specversion") != "1.0":
            return False
        if cloud_event.get("type") != _DOCUMENT_VERSION_ACCEPTED:
            return False
        event_id = str(cloud_event.get("id", ""))
        subject = cloud_event.get("subject", "")
        data = cloud_event.get("data")
        if not event_id or not isinstance(data, dict) or not subject:
            return False
        try:
            version_id = uuid.UUID(str(data["version_id"]))
        except (KeyError, ValueError, TypeError):
            return False

        # G-08: Check the dedupe key first to fast-path duplicates without
        # hitting Temporal, but do NOT reserve the key yet.  A crash between
        # a reservation and a successful workflow start would permanently
        # block the version.
        dedupe_key = f"documind:outbox:consumed:{event_id}"
        existing = await self._redis.set(
            dedupe_key,
            "1",
            nx=True,
            ex=self._dedupe_ttl_seconds,
        )
        if not existing:
            return False

        # G-20: Skip workflow start for versions that were tombstoned between
        # outbox publication and consumption.
        if self._lifecycle_checker is not None:
            try:
                await self._lifecycle_checker.assert_active(str(version_id))
            except Exception:
                return False

        workflow_input = DocumentVersionWorkflowInput(
            version_id=str(version_id),
            content_sha256=str(data.get("content_sha256", "")),
            event_id=event_id,
            correlation_id=_optional_string(data.get("correlation_id")),
        )
        try:
            handle = await self._temporal.start_workflow(
                DocumentVersionWorkflow.run,
                workflow_input,
                id=workflow_id_for(version_id),
                task_queue=self._workflow_task_queue,
            )
            # G-09: Record the Temporal run ID inside the success path.
            # Errors in recording must not prevent the workflow from running
            # or block the consumer — the stage store's claim path also
            # catches a missing run ID at first activity execution.
            if self._run_recorder is not None:
                run_id = _workflow_run_id(handle)
                if run_id is not None:
                    try:
                        await self._run_recorder.record_workflow_start(str(version_id), run_id)
                    except Exception:
                        pass  # Non-fatal: the stage store handles orphaned runs.
        except WorkflowAlreadyStartedError:
            # A concurrent consumer reached Temporal first; the workflow ID is
            # the authoritative idempotency boundary for a version.
            return True
        except Exception:
            # G-08: Release the dedupe key so a retry can re-attempt the start.
            await self._redis.delete(dedupe_key)
            raise
        return True

    async def consume_stream_fields(self, fields: dict[str | bytes, str | bytes]) -> bool:
        """Decode the dispatcher field payload before normal event handling."""
        raw_event = fields.get("event", fields.get(b"event"))
        if raw_event is None:
            return False
        if isinstance(raw_event, bytes):
            raw_event = raw_event.decode("utf-8")
        try:
            event = json.loads(raw_event)
        except (TypeError, ValueError):
            return False
        return await self.consume(event)


class RedisStreamWorkflowRunner:
    """Run the Redis Streams consumer group that starts Temporal workflows.

    The acknowledgement occurs only after ``TemporalWorkflowConsumer`` has
    either started the workflow or established that a duplicate already did.
    An exception leaves the entry pending for Redis redelivery.
    """

    def __init__(
        self,
        *,
        redis_client: RedisStreamsClient,
        consumer: TemporalWorkflowConsumer,
        stream_name: str = _DEFAULT_STREAM,
        group_name: str = "documind-temporal-starters",
        consumer_name: str | None = None,
        block_ms: int = 1_000,
    ) -> None:
        self._redis = redis_client
        self._consumer = consumer
        self._stream_name = stream_name
        self._group_name = group_name
        self._consumer_name = consumer_name or f"worker-{uuid.uuid4()}"
        self._block_ms = block_ms
        self._group_ready = False

    async def ensure_group(self) -> None:
        """Create the durable group once; an existing group is expected."""
        if self._group_ready:
            return
        try:
            await self._redis.xgroup_create(self._stream_name, self._group_name, "0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc).upper():
                raise
        self._group_ready = True

    async def run_once(self, *, count: int = 100) -> int:
        """Process at most one Redis batch and acknowledge completed entries."""
        await self.ensure_group()
        # T4-5: Reclaim idle pending entries before reading new work.
        reclaimed = await self._reclaim_pending(count=count)
        response = await self._redis.xreadgroup(
            self._group_name,
            self._consumer_name,
            {self._stream_name: ">"},
            count=count,
            block=self._block_ms,
        )
        processed = reclaimed
        for _, entries in response or []:
            for entry_id, fields in entries:
                await self._consumer.consume_stream_fields(_decode_fields(fields))
                await self._redis.xack(self._stream_name, self._group_name, _as_text(entry_id))
                processed += 1
        return processed

    async def _reclaim_pending(self, *, count: int = 100, min_idle_ms: int = 60_000) -> int:
        """Reclaim entries idle longer than min_idle_ms from other consumers."""
        reclaimed_count = 0
        try:
            # XAUTOCLAIM returns [new_start_id, [[id, fields], ...], [deleted_ids]]
            xautoclaim = getattr(self._redis, "xautoclaim", None)
            if xautoclaim is None:
                return 0
            result = await xautoclaim(
                self._stream_name,
                self._group_name,
                self._consumer_name,
                min_idle_time=min_idle_ms,
                start_id="0-0",
                count=count,
            )
            if not result or len(result) < 2:
                return 0
            entries = result[1] or []
            for entry_id, fields in entries:
                await self._consumer.consume_stream_fields(_decode_fields(fields))
                await self._redis.xack(self._stream_name, self._group_name, _as_text(entry_id))
                reclaimed_count += 1
        except Exception:
            # Reclamation is best-effort; failures are retried next cycle.
            pass
        return reclaimed_count

    async def run(self, shutdown: asyncio.Event) -> None:
        """Continuously consume until the worker receives a shutdown signal."""
        await self.ensure_group()
        while not shutdown.is_set():
            await self.run_once()


def _as_text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _workflow_run_id(handle: object) -> str | None:
    value = getattr(handle, "run_id", None)
    return str(value) if value else None


def _decode_fields(fields: dict[Any, Any]) -> dict[str | bytes, str | bytes]:
    """Keep redis-py's bytes/str variants inside the adapter boundary."""
    decoded: dict[str | bytes, str | bytes] = {}
    for key, value in fields.items():
        decoded[key] = value
    return decoded
