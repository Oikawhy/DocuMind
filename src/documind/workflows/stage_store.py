"""PostgreSQL-backed workflow-stage idempotency and evidence persistence.

Temporal retries are deliberately not the durability boundary.  Each activity
claims its immutable input in PostgreSQL before a side effect, then records the
canonical output, checksum, lifecycle consequence, and audit event in one
transaction.  A later retry therefore returns the original output instead of
repeating the side effect.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.document import Document, DocumentVersion
from documind.models.enums import DocumentLifecycle, OperationStatus, StageStatus
from documind.models.label import DeletionTombstone
from documind.models.outbox import DeadLetter
from documind.models.processing import Operation, ProcessingRun, ProcessingStage
from documind.services.audit_service import AuditEntry, AuditService
from documind.services.ocr_service import ParserAttempt, ParseResult
from documind.workflows.document_version import StageExecution, StageOutput


class TombstonedVersionError(RuntimeError):
    """The authoritative lifecycle state no longer permits processing writes."""


class StageIntegrityError(RuntimeError):
    """A replay attempted to reuse a stage name with different immutable input."""


class StageTerminalError(RuntimeError):
    """A terminal stage was invoked again instead of being operator-replayed."""


class PostgresStageStore:
    """Persist and replay stage outputs using the existing processing tables."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        audit_service: AuditService,
    ) -> None:
        self._session_factory = session_factory
        self._audit_service = audit_service

    async def assert_active(self, version_id: str) -> None:
        """Fail before an activity touches a side effect for a tombstoned version."""
        async with self._session_factory() as session:
            await self._assert_active_in_session(session, uuid.UUID(version_id))

    async def run(
        self,
        stage: StageExecution,
        execute: Callable[[], Awaitable[dict[str, Any]]],
        *,
        max_attempts: int,
    ) -> StageOutput:
        """Claim one stage, execute once, then durably finish or retry it."""
        replay = await self._claim(stage)
        if replay is not None:
            return replay

        try:
            payload = await execute()
        except Exception as exc:
            await self._record_exception(stage, max_attempts=max_attempts, exc=exc)
            raise

        result = _stage_output(payload)
        failure = _terminal_failure(stage.name, payload)
        if failure is None:
            await self._record_success(stage, result)
        else:
            error_class, error_code, error_message = failure
            await self._record_terminal_failure(
                stage,
                result,
                error_class=error_class,
                error_code=error_code,
                error_message=error_message,
                retry_eligible=False,
            )
        return result

    async def record_workflow_start(self, version_id: str, temporal_run_id: str) -> None:
        """Attach Temporal's concrete run ID to the admitted processing run."""
        async with self._session_factory() as session, session.begin():
            run = await self._latest_run(session, uuid.UUID(version_id))
            if run is None:
                raise StageIntegrityError("A workflow was started without an admitted processing run.")
            if run.temporal_run_id not in {"pending", temporal_run_id}:
                raise StageIntegrityError("The document version is already linked to another Temporal run.")
            run.temporal_run_id = temporal_run_id
            run.state = "processing"

    async def _claim(self, execution: StageExecution) -> StageOutput | None:
        version_id = uuid.UUID(execution.version_id)
        async with self._session_factory() as session, session.begin():
            version = await self._assert_active_in_session(session, version_id)
            run = await self._latest_run(session, version_id)
            if run is None:
                raise StageIntegrityError("No processing run exists for this stage.")
            stage = await self._stage_for_run(session, run.id, execution.name)
            if stage is not None:
                self._verify_stage_identity(stage, execution)
                if stage.status == StageStatus.SUCCEEDED:
                    if stage.output_json is None or stage.output_sha256 is None:
                        raise StageIntegrityError("A successful stage has no durable canonical output.")
                    return StageOutput(output=dict(stage.output_json), output_sha256=stage.output_sha256)
                if stage.status in {StageStatus.FAILED, StageStatus.CANCELLED}:
                    raise StageTerminalError(f"Stage {execution.name} is terminal and must be operator-replayed.")
                stage.status = StageStatus.RUNNING
                stage.attempt_count += 1
                stage.started_at = stage.started_at or datetime.now(UTC)
            else:
                stage = ProcessingStage(
                    id=uuid.uuid4(),
                    processing_run_id=run.id,
                    stage_name=execution.name,
                    stage_order=_stage_order(execution.name),
                    status=StageStatus.RUNNING,
                    idempotency_key=execution.idempotency_key,
                    input_sha256=execution.input_sha256,
                    policy_revision_json={},
                    attempt_count=1,
                    trace_id=uuid.uuid4(),
                    started_at=datetime.now(UTC),
                )
                session.add(stage)

            if version.lifecycle == DocumentLifecycle.ACCEPTED:
                version.lifecycle = DocumentLifecycle.PROCESSING
            run.state = "processing"
            await self._audit_service.write_event_in_session(
                session,
                AuditEntry(
                    actor_subject=None,
                    action="document_version.stage.started",
                    resource_type="document_version",
                    resource_id=str(version_id),
                    trace_id=stage.trace_id,
                    details={
                        "stage": execution.name,
                        "input_sha256": execution.input_sha256,
                        "idempotency_key": execution.idempotency_key,
                        "attempt": stage.attempt_count,
                    },
                ),
            )
            return None

    async def _record_success(self, execution: StageExecution, result: StageOutput) -> None:
        version_id = uuid.UUID(execution.version_id)
        async with self._session_factory() as session, session.begin():
            version = await self._assert_active_in_session(session, version_id)
            run, stage = await self._run_and_stage(session, version_id, execution)
            stage.status = StageStatus.SUCCEEDED
            stage.output_json = result.output
            stage.output_sha256 = result.output_sha256
            stage.safe_error_code = None
            stage.ended_at = datetime.now(UTC)
            _apply_success_metadata(version, execution.name, result.output)
            run.state = "processing"
            await self._set_operation_running(session, version_id)
            await self._audit_service.write_event_in_session(
                session,
                AuditEntry(
                    actor_subject=None,
                    action="document_version.stage.succeeded",
                    resource_type="document_version",
                    resource_id=str(version_id),
                    trace_id=stage.trace_id,
                    details={
                        "stage": execution.name,
                        "input_sha256": execution.input_sha256,
                        "output_sha256": result.output_sha256,
                    },
                ),
            )

    async def _record_terminal_failure(
        self,
        execution: StageExecution,
        result: StageOutput,
        *,
        error_class: str,
        error_code: str,
        error_message: str | None,
        retry_eligible: bool,
    ) -> None:
        version_id = uuid.UUID(execution.version_id)
        async with self._session_factory() as session, session.begin():
            version = await self._assert_active_in_session(session, version_id)
            run, stage = await self._run_and_stage(session, version_id, execution)
            stage.status = StageStatus.FAILED
            stage.output_json = result.output
            stage.output_sha256 = result.output_sha256
            stage.safe_error_code = error_code
            stage.ended_at = datetime.now(UTC)
            _apply_failure_metadata(version, error_class, error_code, error_message)
            run.state = "failed"
            run.finished_at = datetime.now(UTC)
            await self._set_operation_failed(session, version_id, error_code)
            session.add(
                DeadLetter(
                    id=uuid.uuid4(),
                    processing_run_id=run.id,
                    stage_id=stage.id,
                    workflow_id=run.temporal_workflow_id,
                    activity_name=execution.name,
                    safe_error_class=error_class,
                    input_sha256=execution.input_sha256,
                    retry_eligible=retry_eligible,
                    state="open",
                ),
            )
            await self._audit_service.write_event_in_session(
                session,
                AuditEntry(
                    actor_subject=None,
                    action="document_version.stage.failed",
                    resource_type="document_version",
                    resource_id=str(version_id),
                    trace_id=stage.trace_id,
                    details={
                        "stage": execution.name,
                        "input_sha256": execution.input_sha256,
                        "output_sha256": result.output_sha256,
                        "safe_error_class": error_class,
                        "safe_error_code": error_code,
                    },
                ),
            )

    async def _record_exception(self, execution: StageExecution, *, max_attempts: int, exc: Exception) -> None:
        """Record a retry, or terminally dead-letter the final safe failure."""
        version_id = uuid.UUID(execution.version_id)
        try:
            async with self._session_factory() as session, session.begin():
                version = await self._assert_active_in_session(session, version_id)
                run, stage = await self._run_and_stage(session, version_id, execution)
                error_class = _exception_error_class(exc)
                if stage.attempt_count < max_attempts:
                    stage.status = StageStatus.RETRYING
                    stage.safe_error_code = "ACTIVITY_RETRY"
                    await self._audit_service.write_event_in_session(
                        session,
                        AuditEntry(
                            actor_subject=None,
                            action="document_version.stage.retrying",
                            resource_type="document_version",
                            resource_id=str(version_id),
                            trace_id=stage.trace_id,
                            details={
                                "stage": execution.name,
                                "attempt": stage.attempt_count,
                                "safe_error_class": error_class,
                            },
                        ),
                    )
                    return

                stage.status = StageStatus.FAILED
                stage.safe_error_code = "ACTIVITY_RETRIES_EXHAUSTED"
                stage.ended_at = datetime.now(UTC)
                _apply_failure_metadata(
                    version,
                    error_class,
                    "ACTIVITY_RETRIES_EXHAUSTED",
                    "The processing dependency did not complete.",
                )
                run.state = "failed"
                run.finished_at = datetime.now(UTC)
                await self._set_operation_failed(session, version_id, "ACTIVITY_RETRIES_EXHAUSTED")
                session.add(
                    DeadLetter(
                        id=uuid.uuid4(),
                        processing_run_id=run.id,
                        stage_id=stage.id,
                        workflow_id=run.temporal_workflow_id,
                        activity_name=execution.name,
                        safe_error_class=error_class,
                        input_sha256=execution.input_sha256,
                        retry_eligible=True,
                        state="open",
                    ),
                )
                await self._audit_service.write_event_in_session(
                    session,
                    AuditEntry(
                        actor_subject=None,
                        action="document_version.stage.dead_lettered",
                        resource_type="document_version",
                        resource_id=str(version_id),
                        trace_id=stage.trace_id,
                        details={
                            "stage": execution.name,
                            "input_sha256": execution.input_sha256,
                            "safe_error_class": error_class,
                            "safe_error_code": "ACTIVITY_RETRIES_EXHAUSTED",
                        },
                    ),
                )
        except TombstonedVersionError:
            # Tombstoning is authoritative: never turn it into a later failed
            # stage write merely because an in-flight dependency also errored.
            return

    async def _run_and_stage(
        self,
        session: AsyncSession,
        version_id: uuid.UUID,
        execution: StageExecution,
    ) -> tuple[ProcessingRun, ProcessingStage]:
        run = await self._latest_run(session, version_id)
        if run is None:
            raise StageIntegrityError("No processing run exists for this stage.")
        stage = await self._stage_for_run(session, run.id, execution.name)
        if stage is None:
            raise StageIntegrityError("The claimed stage record disappeared.")
        self._verify_stage_identity(stage, execution)
        return run, stage

    async def _latest_run(self, session: AsyncSession, version_id: uuid.UUID) -> ProcessingRun | None:
        return (
            await session.execute(
                select(ProcessingRun)
                .where(ProcessingRun.version_id == version_id)
                .order_by(desc(ProcessingRun.started_at), desc(ProcessingRun.id))
                .limit(1)
                .with_for_update(),
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _stage_for_run(
        session: AsyncSession,
        run_id: uuid.UUID,
        stage_name: str,
    ) -> ProcessingStage | None:
        return (
            await session.execute(
                select(ProcessingStage)
                .where(
                    ProcessingStage.processing_run_id == run_id,
                    ProcessingStage.stage_name == stage_name,
                )
                .with_for_update(),
            )
        ).scalar_one_or_none()

    async def _assert_active_in_session(self, session: AsyncSession, version_id: uuid.UUID) -> DocumentVersion:
        version = await session.get(DocumentVersion, version_id, with_for_update=True)
        if version is None or version.lifecycle == DocumentLifecycle.ERASED:
            raise TombstonedVersionError("The document version is no longer active.")
        document = await session.get(Document, version.document_id, with_for_update=True)
        if document is None or document.erased_at is not None:
            raise TombstonedVersionError("The parent document is no longer active.")
        tombstone = (
            await session.execute(
                select(DeletionTombstone.id)
                .where(
                    or_(
                        DeletionTombstone.document_id == version.document_id,
                        DeletionTombstone.version_id == version.id,
                    ),
                )
                .limit(1),
            )
        ).scalar_one_or_none()
        if tombstone is not None:
            raise TombstonedVersionError("A deletion tombstone prevents further processing writes.")
        return version

    @staticmethod
    def _verify_stage_identity(stage: ProcessingStage, execution: StageExecution) -> None:
        if stage.input_sha256 != execution.input_sha256 or stage.idempotency_key != execution.idempotency_key:
            raise StageIntegrityError("Stage input checksum or idempotency key does not match its immutable record.")

    @staticmethod
    async def _set_operation_running(session: AsyncSession, version_id: uuid.UUID) -> None:
        operations = list(
            (
                await session.execute(
                    select(Operation).where(
                        Operation.version_id == version_id,
                        Operation.status == OperationStatus.ACCEPTED,
                    )
                )
            ).scalars()
        )
        for operation in operations:
            operation.status = OperationStatus.RUNNING

    @staticmethod
    async def _set_operation_failed(session: AsyncSession, version_id: uuid.UUID, error_code: str) -> None:
        operations = list(
            (await session.execute(select(Operation).where(Operation.version_id == version_id))).scalars()
        )
        for operation in operations:
            if operation.status not in {OperationStatus.SUCCEEDED, OperationStatus.CANCELLED}:
                operation.status = OperationStatus.FAILED
                operation.safe_error_code = error_code
                operation.completed_at = datetime.now(UTC)


class PostgresParseResultSource:
    """Read the canonical persisted parse output for the normalizer."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_parse_result(self, version_id: uuid.UUID) -> ParseResult:
        async with self._session_factory() as session:
            run = (
                await session.execute(
                    select(ProcessingRun)
                    .where(ProcessingRun.version_id == version_id)
                    .order_by(desc(ProcessingRun.started_at), desc(ProcessingRun.id))
                    .limit(1),
                )
            ).scalar_one_or_none()
            if run is None:
                raise StageIntegrityError("No processing run exists for the parse result.")
            stage = (
                await session.execute(
                    select(ProcessingStage).where(
                        ProcessingStage.processing_run_id == run.id,
                        ProcessingStage.stage_name == "parse",
                        ProcessingStage.status == StageStatus.SUCCEEDED,
                    )
                )
            ).scalar_one_or_none()
            if stage is None or stage.output_json is None:
                raise StageIntegrityError("No durable successful parse output exists.")
            output = stage.output_json
        attempts = [ParserAttempt(**attempt) for attempt in output.get("parser_attempts", [])]
        return ParseResult(
            version_id=str(output.get("version_id", version_id)),
            success=bool(output.get("success", False)),
            engine=_optional_string(output.get("engine")),
            text=str(output.get("text", "")),
            pages=list(output.get("pages", [])),
            confidence=float(output.get("confidence", 0.0)),
            parser_attempts=attempts,
            safe_error_class=_optional_string(output.get("safe_error_class")),
            safe_error_code=_optional_string(output.get("safe_error_code")),
        )


class PostgresVersionContentSource:
    """Resolve private quarantine bytes and their MIME candidate from PostgreSQL."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], storage_service: Any) -> None:
        self._session_factory = session_factory
        self._storage_service = storage_service

    async def read_version_bytes(self, version_id: uuid.UUID) -> bytes:
        async with self._session_factory() as session:
            version = await session.get(DocumentVersion, version_id)
            if version is None:
                raise StageIntegrityError("The version does not exist.")
            object_key = version.quarantine_object_key
        return await self._storage_service.read_bytes(object_key)

    async def declared_mime_for(self, version_id: uuid.UUID) -> str:
        async with self._session_factory() as session:
            version = await session.get(DocumentVersion, version_id)
            if version is None:
                raise StageIntegrityError("The version does not exist.")
            return version.declared_mime_family


def _stage_output(payload: dict[str, Any]) -> StageOutput:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return StageOutput(output=payload, output_sha256=hashlib.sha256(encoded).hexdigest())


def _stage_order(name: str) -> int:
    return {
        "admit": 0,
        "inspect": 1,
        "parse": 2,
        "normalize": 3,
        "chunk": 4,
        "enrich": 5,
        "project": 6,
        "verify": 7,
        "complete": 8,
    }.get(name, 100)


def _terminal_failure(name: str, output: dict[str, Any]) -> tuple[str, str, str | None] | None:
    if name == "inspect" and not output.get("safe", False):
        return (
            str(output.get("safe_error_class") or "unsafe_content"),
            str(output.get("safe_error_code") or "INSPECTION_REJECTED"),
            _optional_string(output.get("safe_message")),
        )
    if name == "parse" and not output.get("success", False):
        return (
            str(output.get("safe_error_class") or "unsupported_content"),
            str(output.get("safe_error_code") or "PARSER_EXHAUSTED"),
            None,
        )
    return None


def _apply_success_metadata(version: DocumentVersion, stage_name: str, output: dict[str, Any]) -> None:
    if stage_name == "inspect":
        version.detected_mime_type = _optional_string(output.get("detected_mime"))
    elif stage_name == "parse":
        version.parser_revision = _parser_revision(output)
    elif stage_name == "normalize":
        version.normalization_revision = _optional_string(output.get("normalization_revision"))
        version.normalized_object_key = _optional_string(output.get("normalized_object_key"))
    elif stage_name == "chunk":
        # Record chunk profile and count/checksum in version metadata
        pass  # Chunk metadata is stored in the stage output_json; no version fields are modified.
    elif stage_name == "enrich":
        # Record type suggestion and extraction status from enrichment output
        type_suggestion = output.get("type_suggestion")
        if type_suggestion is not None:
            version.type_suggestion = type_suggestion
        extraction_status = output.get("extraction_status")
        if extraction_status:
            import contextlib

            from documind.models.enums import ExtractionStatus

            with contextlib.suppress(ValueError):
                version.extraction_state = ExtractionStatus(extraction_status)
    elif stage_name in {"project", "verify"}:
        # Projection metadata is stored in stage output_json; no version fields modified.
        pass
    elif stage_name == "complete" and version.lifecycle in {DocumentLifecycle.ACCEPTED, DocumentLifecycle.PROCESSING}:
        # Authoritative lifecycle transition after all projections verified.
        version.lifecycle = DocumentLifecycle.COMPLETED


def _apply_failure_metadata(
    version: DocumentVersion,
    error_class: str,
    error_code: str,
    error_message: str | None,
) -> None:
    if version.lifecycle in {DocumentLifecycle.ACCEPTED, DocumentLifecycle.PROCESSING}:
        version.lifecycle = (
            DocumentLifecycle.QUARANTINED if error_class == "unsafe_content" else DocumentLifecycle.FAILED
        )
    version.failure_code = error_code
    version.failure_safe_message = error_message


def _parser_revision(output: dict[str, Any]) -> str | None:
    attempts = output.get("parser_attempts")
    if isinstance(attempts, list) and attempts:
        last = attempts[-1]
        if isinstance(last, dict):
            return _optional_string(last.get("version"))
    return _optional_string(output.get("engine"))


def _exception_error_class(exc: Exception) -> str:
    from documind.services.ocr_service import ParserUnavailableError
    from documind.services.scanner_service import ScannerUnavailableError

    _TRANSIENT = (ConnectionError, OSError, TimeoutError, ScannerUnavailableError, ParserUnavailableError)
    return "transient_dependency" if isinstance(exc, _TRANSIENT) else "integrity"


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
