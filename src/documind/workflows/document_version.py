"""Temporal orchestration for one immutable document-version ingestion run."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from temporalio import workflow
from temporalio.common import RetryPolicy

INGEST_QUEUE = "ingest-cpu"
MODEL_QUEUE = "model-gpu"


def workflow_id_for(version_id: uuid.UUID | str) -> str:
    """Return the sole permitted Temporal workflow ID for a document version."""
    return f"document-version/{version_id}"


def stage_idempotency_key(version_id: uuid.UUID | str, stage_name: str, input_sha256: str) -> str:
    """Return a deterministic stage key shared by retries and Temporal replays."""
    value = f"{version_id}:{stage_name}:{input_sha256}".encode()
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class DocumentVersionWorkflowInput:
    """Policy-neutral event data needed to begin a version workflow."""

    version_id: str
    content_sha256: str
    event_id: str
    correlation_id: str | None = None


@dataclass(frozen=True)
class StageExecution:
    """Immutable stage metadata persisted by activities before their side effect."""

    version_id: str
    name: str
    input_sha256: str
    idempotency_key: str


@dataclass(frozen=True)
class StageOutput:
    """Canonical activity output and checksum used for safe replay validation."""

    output: dict[str, Any]
    output_sha256: str


class StageReplayStore(Protocol):
    """Durable activity idempotency boundary used by worker activities."""

    async def run(
        self,
        stage: StageExecution,
        execute: Callable[[], Awaitable[dict[str, Any]]],
        *,
        max_attempts: int,
    ) -> StageOutput:
        """Return a previously persisted output or execute and persist one."""


class InMemoryStageReplayStore:
    """Small replay store used by unit tests and local deterministic harnesses.

    Production activities persist the same idempotency key, input checksum, and
    output checksum in ``processing_stage`` under their PostgreSQL transaction.
    """

    def __init__(self) -> None:
        self._completed: dict[tuple[str, str, str, str], StageOutput] = {}

    async def run(
        self,
        stage: StageExecution,
        execute: Callable[[], Awaitable[dict[str, Any]]],
        *,
        max_attempts: int = 1,
    ) -> StageOutput:
        key = (stage.version_id, stage.name, stage.input_sha256, stage.idempotency_key)
        completed = self._completed.get(key)
        if completed is not None:
            return completed
        output = await execute()
        canonical_output = json.dumps(output, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        result = StageOutput(output=output, output_sha256=hashlib.sha256(canonical_output).hexdigest())
        self._completed[key] = result
        return result


@dataclass(frozen=True)
class StageConfiguration:
    """Temporal scheduling contract for a workflow stage."""

    name: str
    timeout_seconds: int
    heartbeat_seconds: int
    retry_attempts: int
    task_queue: str

    @property
    def activity_name(self) -> str:
        return self.name

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            initial_interval=timedelta(seconds=30),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=15),
            maximum_attempts=self.retry_attempts,
        )


@dataclass(frozen=True)
class DocumentVersionWorkflowResult:
    """Terminal state after the full ingestion pipeline including projections."""

    version_id: str
    state: str
    failed_stage: str | None = None
    safe_error_class: str | None = None
    safe_error_code: str | None = None
    normalization: dict[str, Any] | None = None
    chunk: dict[str, Any] | None = None
    enrichment: dict[str, Any] | None = None
    projection: dict[str, Any] | None = None
    completion: dict[str, Any] | None = None


@workflow.defn
class DocumentVersionWorkflow:
    """Run the full idempotent ingestion pipeline for an accepted version.

    Stages execute in order:
    inspect → parse → normalize → chunk → enrich → project → verify → complete.
    Each stage receives an immutable checksum of its predecessor rather than
    raw document text, keeping Temporal workflow history compact.
    """

    @staticmethod
    def stage_configurations() -> tuple[StageConfiguration, ...]:
        return (
            StageConfiguration("inspect", 600, 30, 3, INGEST_QUEUE),
            StageConfiguration("parse", 600, 30, 2, INGEST_QUEUE),
            StageConfiguration("normalize", 300, 30, 3, INGEST_QUEUE),
            StageConfiguration("chunk", 300, 30, 3, INGEST_QUEUE),
            StageConfiguration("enrich", 300, 30, 2, MODEL_QUEUE),
            StageConfiguration("project", 600, 30, 2, INGEST_QUEUE),
            StageConfiguration("verify", 120, 30, 2, INGEST_QUEUE),
            StageConfiguration("complete", 120, 30, 1, INGEST_QUEUE),
        )

    @workflow.run
    async def run(self, workflow_input: DocumentVersionWorkflowInput) -> DocumentVersionWorkflowResult:
        """Execute all ingestion stages in the required order.

        Each stage receives only the StageExecution metadata and an immutable
        checksum reference to its predecessor's output.  Activities resolve
        their actual data from PostgreSQL and MinIO using durable stage records,
        never from workflow history payloads.
        """
        current_checksum = workflow_input.content_sha256

        # --- Stage 1: inspect ---
        inspect_stage = _stage_execution(workflow_input.version_id, "inspect", current_checksum)
        inspection = await self._execute_stage("inspect", inspect_stage)
        if not inspection.get("safe", False):
            return DocumentVersionWorkflowResult(
                version_id=workflow_input.version_id,
                state="failed",
                failed_stage="inspect",
                safe_error_class=_optional_string(inspection.get("safe_error_class")),
                safe_error_code=_optional_string(inspection.get("safe_error_code")),
            )

        # --- Stage 2: parse ---
        current_checksum = _payload_sha256(inspection)
        parse_stage = _stage_execution(workflow_input.version_id, "parse", current_checksum)
        parsed = await self._execute_stage("parse", parse_stage)
        if not parsed.get("success", False):
            return DocumentVersionWorkflowResult(
                version_id=workflow_input.version_id,
                state="failed",
                failed_stage="parse",
                safe_error_class=_optional_string(parsed.get("safe_error_class")),
                safe_error_code=_optional_string(parsed.get("safe_error_code")),
            )

        # --- Stage 3: normalize ---
        current_checksum = _payload_sha256(parsed)
        normalize_stage = _stage_execution(workflow_input.version_id, "normalize", current_checksum)
        normalized = await self._execute_stage("normalize", normalize_stage)

        # --- Stage 4: chunk ---
        # Chunk receives only the checksum of the normalization output; the
        # activity resolves the actual normalized content from MinIO/PostgreSQL.
        current_checksum = _payload_sha256(normalized)
        chunk_stage = _stage_execution(workflow_input.version_id, "chunk", current_checksum)
        chunked = await self._execute_stage("chunk", chunk_stage)

        # --- Stage 5: enrich ---
        # Enrich receives only the checksum of the chunk output; the activity
        # resolves version metadata, chunks, and template from PostgreSQL.
        current_checksum = _payload_sha256(chunked)
        enrich_stage = _stage_execution(workflow_input.version_id, "enrich", current_checksum)
        enriched = await self._execute_stage("enrich", enrich_stage)

        # --- End of Task 5 scope ---
        # T5.6-01: Task 6 owns project, verify, and complete.
        return DocumentVersionWorkflowResult(
            version_id=workflow_input.version_id,
            state="processing",
            normalization=normalized,
            chunk=chunked,
            enrichment=enriched,
        )

    async def _execute_stage(
        self,
        stage_name: str,
        stage: StageExecution,
    ) -> dict[str, Any]:
        """Execute a single stage activity, passing only the StageExecution metadata.

        Activities receive only the immutable StageExecution dataclass.  They
        resolve their actual data dependencies from durable stores (PostgreSQL,
        MinIO) using the version ID and stage name, never from workflow history
        payloads.  This keeps the Temporal event history compact regardless of
        document size.
        """
        configuration = next(config for config in self.stage_configurations() if config.name == stage_name)
        return await workflow.execute_activity(
            configuration.activity_name,
            args=[stage],
            task_queue=configuration.task_queue,
            start_to_close_timeout=timedelta(seconds=configuration.timeout_seconds),
            heartbeat_timeout=timedelta(seconds=configuration.heartbeat_seconds),
            retry_policy=configuration.retry_policy(),
        )


def _stage_execution(version_id: str, name: str, input_sha256: str) -> StageExecution:
    return StageExecution(
        version_id=version_id,
        name=name,
        input_sha256=input_sha256,
        idempotency_key=stage_idempotency_key(version_id, name, input_sha256),
    )


def _payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
