"""Document admission domain tests against the PostgreSQL schema."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from documind.domain.document_service import DocumentService, UploadSource
from documind.domain.errors import (
    InvalidRequestError,
    LabelValidationError,
    ResourceConflictError,
    TemplateResolutionError,
)
from documind.domain.label_service import LabelService
from documind.domain.policy_service import PolicyService, RoleMapping
from documind.models.audit import AuditEvent
from documind.models.document import Document, DocumentVersion
from documind.models.enums import DocumentLifecycle, PolicyStatus, StageStatus
from documind.models.label import DocumentLabel, Label
from documind.models.outbox import DeadLetter, OutboxEvent
from documind.models.policy import ChunkProfileRevision, DeclaredType, PolicyRevision
from documind.models.processing import Operation, ProcessingStage
from documind.models.template import ExtractionTemplateRevision
from documind.services.audit_service import AuditService
from documind.services.identity_service import Principal
from documind.workflows.document_version import StageExecution, stage_idempotency_key
from documind.workflows.stage_store import PostgresStageStore

_TEST_DATABASE_URL = os.environ.get(
    "DOCUMIND_TEST_DATABASE_URL",
    "postgresql+asyncpg://documind:documind@localhost:5433/documind_test",
)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _migrate() -> None:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": _TEST_DATABASE_URL}
    subprocess.run(
        [str(_PROJECT_ROOT / ".venv/bin/alembic"), "downgrade", "base"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        [str(_PROJECT_ROOT / ".venv/bin/alembic"), "upgrade", "head"],
        cwd=_PROJECT_ROOT,
        env=env,
        check=True,
    )


_migrate()


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_TEST_DATABASE_URL, pool_size=1)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        with contextlib.suppress(Exception):
            await engine.dispose()


@pytest_asyncio.fixture
async def admission_dependencies(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[DocumentService, uuid.UUID, str]:
    policy_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    declared_type_id = uuid.uuid4()
    label_id = uuid.uuid4()
    declared_type_key = f"report-{declared_type_id}"
    async with session_factory() as session, session.begin():
        session.add(
            PolicyRevision(
                id=policy_id,
                policy_kind="authorization",
                stable_key=f"admission-{policy_id}",
                revision=1,
                status=PolicyStatus.ACTIVE,
                body={"chunk_profile_revision_id": str(profile_id)},
                body_sha256="a" * 64,
                created_by_subject="administrator",
            )
        )
        await session.flush()
        session.add(
            ChunkProfileRevision(
                id=profile_id,
                profile_id=uuid.uuid4(),
                revision=1,
                status=PolicyStatus.ACTIVE,
                configuration={"strategy": "recursive"},
                configuration_sha256="b" * 64,
            )
        )
        session.add(
            DeclaredType(
                id=declared_type_id,
                stable_key=declared_type_key,
                active_policy_revision_id=policy_id,
                active=True,
            )
        )
        session.add(
            Label(
                id=label_id,
                stable_key=f"internal-{label_id}",
                retention_class="standard",
                active=True,
            )
        )

    storage = MagicMock()
    storage.stream_upload = AsyncMock(
        side_effect=[
            ("c" * 64, 14),
            ("d" * 64, 15),
            ("e" * 64, 16),
        ]
    )
    storage.remove_object = AsyncMock()
    storage.quarantine_key.side_effect = lambda version_id: f"quarantine/{version_id}/original"
    policy_service = AsyncMock(spec=PolicyService)
    policy_service.get_role_mappings.return_value = [
        RoleMapping(
            role_key="editor",
            allowed_label_ids={label_id},
            permitted_actions={"upload", "version_create", "delete", "read"},
        )
    ]
    policy_service.get_active_policy.return_value = SimpleNamespace(id=policy_id)
    authorization = AsyncMock()
    authorization.authorize.return_value.decision = "allow"
    audit = AuditService(session_factory)
    service = DocumentService(
        session_factory=session_factory,
        storage_service=storage,
        authorization_service=authorization,
        label_service=LabelService(session_factory),
        policy_service=policy_service,
        audit_service=audit,
        max_upload_bytes=1024,
        cursor_hmac_key=b"x" * 32,
    )
    return service, label_id, declared_type_key


def _principal() -> Principal:
    return Principal(
        subject="writer@example.test",
        display_name="Writer",
        email=None,
        groups=["editors"],
        active=True,
        issuer="https://issuer.example.test",
    )


def _upload() -> UploadSource:
    return UploadSource(reader=io.BytesIO(b"example-document"), filename="report.txt", content_type="text/plain")


def _idempotency_key() -> str:
    return uuid.uuid4().hex


async def test_admission_persists_immutable_metadata_and_transactional_outbox(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, label_id, declared_type = admission_dependencies

    result = await service.admit_document(
        file=_upload(),
        title="Quarterly report",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )

    assert result.lifecycle_state == "accepted"
    async with session_factory() as session:
        document = await session.get(Document, result.document_id)
        version = await session.get(DocumentVersion, result.version_id)
        operation = await session.get(Operation, result.operation_id)
        audit_event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.resource_id == str(result.version_id)),
            )
        ).scalar_one()
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == result.version_id),
            )
        ).scalar_one()
        labels = (await session.execute(select(DocumentLabel))).scalars().all()
    assert document is not None and document.title == "Quarterly report"
    assert version is not None and version.version_number == 1
    assert version.quarantine_object_key == f"quarantine/{result.version_id}/original"
    assert version.declared_mime_family == "text/plain"
    assert operation is not None and operation.status.value == "accepted"
    assert audit_event.action == "document_version.accepted"
    assert len(labels) == 1
    assert event.cloud_event["data"] == {
        "document_id": str(result.document_id),
        "version_id": str(result.version_id),
        "version_number": 1,
        "content_sha256": "c" * 64,
        "lifecycle_state": "accepted",
        "contract_version": "1.0.0",
        "correlation_id": str(result.operation_id),
    }


async def test_stage_replay_persists_output_checksum_and_processing_lifecycle(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, label_id, declared_type = admission_dependencies
    result = await service.admit_document(
        file=_upload(),
        title="Replay evidence",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )
    input_sha256 = "c" * 64
    stage = StageExecution(
        version_id=str(result.version_id),
        name="inspect",
        input_sha256=input_sha256,
        idempotency_key=stage_idempotency_key(result.version_id, "inspect", input_sha256),
    )
    store = PostgresStageStore(session_factory=session_factory, audit_service=AuditService(session_factory))
    calls = 0

    async def execute() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"safe": True, "detected_mime": "text/plain"}

    first = await store.run(stage, execute, max_attempts=3)
    replay = await store.run(stage, execute, max_attempts=3)

    assert calls == 1
    assert replay == first
    async with session_factory() as session:
        version = await session.get(DocumentVersion, result.version_id)
        persisted = (
            await session.execute(
                select(ProcessingStage).where(ProcessingStage.stage_name == "inspect"),
            )
        ).scalar_one()
    assert version is not None and version.lifecycle == DocumentLifecycle.PROCESSING
    assert version.detected_mime_type == "text/plain"
    assert persisted.status == StageStatus.SUCCEEDED
    assert persisted.output_json == {"safe": True, "detected_mime": "text/plain"}
    assert persisted.output_sha256 == first.output_sha256


async def test_terminal_stage_failure_persists_dead_letter_and_quarantine_state(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, label_id, declared_type = admission_dependencies
    result = await service.admit_document(
        file=_upload(),
        title="Unsafe evidence",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )
    input_sha256 = "c" * 64
    stage = StageExecution(
        version_id=str(result.version_id),
        name="inspect",
        input_sha256=input_sha256,
        idempotency_key=stage_idempotency_key(result.version_id, "inspect", input_sha256),
    )
    store = PostgresStageStore(session_factory=session_factory, audit_service=AuditService(session_factory))

    await store.run(
        stage,
        lambda: _unsafe_inspection_output(),
        max_attempts=3,
    )

    async with session_factory() as session:
        version = await session.get(DocumentVersion, result.version_id)
        dead_letter = (
            await session.execute(select(DeadLetter).where(DeadLetter.activity_name == "inspect"))
        ).scalar_one()
    assert version is not None and version.lifecycle == DocumentLifecycle.QUARANTINED
    assert version.failure_code == "MALWARE_DETECTED"
    assert dead_letter.safe_error_class == "unsafe_content"
    assert dead_letter.retry_eligible is False


async def _unsafe_inspection_output() -> dict[str, object]:
    return {
        "safe": False,
        "detected_mime": "text/plain",
        "safe_error_class": "unsafe_content",
        "safe_error_code": "MALWARE_DETECTED",
        "safe_message": "The file was rejected by malware inspection.",
    }


async def test_exact_idempotency_replay_returns_original_operation_and_removes_new_quarantine_object(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
) -> None:
    service, label_id, declared_type = admission_dependencies
    service._storage_service.stream_upload.side_effect = [("c" * 64, 14), ("c" * 64, 14)]  # type: ignore[attr-defined]
    key = _idempotency_key()
    kwargs = {
        "file": _upload(),
        "title": "Quarterly report",
        "labels": [label_id],
        "declared_type": declared_type,
        "principal": _principal(),
        "idempotency_key": key,
    }
    original = await service.admit_document(**kwargs)
    replay = await service.admit_document(**{**kwargs, "file": _upload()})

    assert replay == original
    service._storage_service.remove_object.assert_awaited_once()  # type: ignore[attr-defined]


async def test_new_version_inherits_document_labels_and_increments_version_number(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, label_id, declared_type = admission_dependencies
    original = await service.admit_document(
        file=_upload(),
        title="Quarterly report",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )
    next_version = await service.admit_version(
        document_id=original.document_id,
        file=_upload(),
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )

    async with session_factory() as session:
        version = await session.get(DocumentVersion, next_version.version_id)
    assert version is not None and version.version_number == 2


async def test_invalid_label_fails_before_writing_a_quarantine_object(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, _label_id, declared_type = admission_dependencies
    forbidden_label_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            Label(
                id=forbidden_label_id,
                stable_key=f"restricted-{forbidden_label_id}",
                retention_class="restricted",
                active=True,
            )
        )

    with pytest.raises(LabelValidationError):
        await service.admit_document(
            file=_upload(),
            title="Restricted report",
            labels=[forbidden_label_id],
            declared_type=declared_type,
            principal=_principal(),
            idempotency_key=_idempotency_key(),
        )

    service._storage_service.stream_upload.assert_not_awaited()  # type: ignore[attr-defined]


async def test_duplicate_version_content_returns_safe_conflict(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
) -> None:
    service, label_id, declared_type = admission_dependencies
    service._storage_service.stream_upload.side_effect = [("c" * 64, 14), ("c" * 64, 14)]  # type: ignore[attr-defined]
    admitted = await service.admit_document(
        file=_upload(),
        title="Quarterly report",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )

    with pytest.raises(ResourceConflictError, match="already exists"):
        await service.admit_version(
            document_id=admitted.document_id,
            file=_upload(),
            principal=_principal(),
            idempotency_key=_idempotency_key(),
        )


async def test_expired_idempotency_key_can_create_a_new_operation(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, label_id, declared_type = admission_dependencies
    key = _idempotency_key()
    first = await service.admit_document(
        file=_upload(),
        title="First report",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=key,
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Operation)
            .where(Operation.id == first.operation_id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )

    second = await service.admit_document(
        file=_upload(),
        title="Second report",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=key,
    )

    assert second.operation_id != first.operation_id


async def test_document_cursor_is_hmac_signed(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
) -> None:
    service, _label_id, _declared_type = admission_dependencies
    document_id = uuid.uuid4()
    cursor = service._encode_cursor("2026-08-04T12:00:00+00:00", str(document_id))

    assert service._decode_cursor(cursor) == ("2026-08-04T12:00:00+00:00", str(document_id))
    with pytest.raises(InvalidRequestError):
        service._decode_cursor("A" + cursor[1:])


async def test_delete_starts_erasure_operation_without_erasing_document(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, label_id, declared_type = admission_dependencies
    admitted = await service.admit_document(
        file=_upload(),
        title="Quarterly report",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )

    operation = await service.delete_document(
        document_id=admitted.document_id,
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )

    async with session_factory() as session:
        document = await session.get(Document, admitted.document_id)
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == admitted.document_id),
            )
        ).scalar_one()
    assert document is not None and document.deletion_requested_at is not None
    assert document.erased_at is None
    assert operation.operation_type == "document_erasure"
    assert event.event_type == "io.documind.document.erasure-requested.v1"


async def test_list_and_operation_read_expose_only_safe_admission_metadata(
    admission_dependencies: tuple[DocumentService, uuid.UUID, str],
) -> None:
    service, label_id, declared_type = admission_dependencies
    admitted = await service.admit_document(
        file=_upload(),
        title="Quarterly report",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )

    page = await service.list_documents(
        filters={"type": declared_type, "labels": [label_id], "state": None},
        cursor=None,
        limit=50,
        principal=_principal(),
    )
    operation, stages = await service.get_operation(admitted.operation_id, _principal())

    assert page.items == [
        {
            "id": str(admitted.document_id),
            "title": "Quarterly report",
            "declared_type_id": page.items[0]["declared_type_id"],
            "created_at": page.items[0]["created_at"],
            "lifecycle_state": "accepted",
        }
    ]
    assert operation.id == admitted.operation_id
    assert [(stage.stage_name, stage.status.value) for stage in stages] == [("admit", "succeeded")]


# ---------------------------------------------------------------------------
# Task 5.1 – Template revision pinning at admission
# ---------------------------------------------------------------------------


async def _create_admission_with_template_policy(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    template_status: PolicyStatus = PolicyStatus.ACTIVE,
    template_declared_type_id: uuid.UUID | None = None,
    include_template_in_policy: bool = True,
) -> tuple[DocumentService, uuid.UUID, str, uuid.UUID]:
    """Create admission prerequisites with an extraction template in the policy body."""
    policy_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    declared_type_id = uuid.uuid4()
    template_id = uuid.uuid4()
    template_revision_id = uuid.uuid4()
    label_id = uuid.uuid4()
    declared_type_key = f"template-test-{declared_type_id}"

    policy_body: dict = {"chunk_profile_revision_id": str(profile_id)}
    if include_template_in_policy:
        policy_body["extraction_template_revision_id"] = str(template_revision_id)

    async with session_factory() as session, session.begin():
        session.add(
            PolicyRevision(
                id=policy_id,
                policy_kind="authorization",
                stable_key=f"admission-{policy_id}",
                revision=1,
                status=PolicyStatus.ACTIVE,
                body=policy_body,
                body_sha256="t" * 64,
                created_by_subject="administrator",
            )
        )
        await session.flush()
        session.add(
            ChunkProfileRevision(
                id=profile_id,
                profile_id=uuid.uuid4(),
                revision=1,
                status=PolicyStatus.ACTIVE,
                configuration={"strategy": "recursive"},
                configuration_sha256="u" * 64,
            )
        )
        session.add(
            DeclaredType(
                id=declared_type_id,
                stable_key=declared_type_key,
                active_policy_revision_id=policy_id,
                active=True,
            )
        )
        if template_declared_type_id is not None and template_declared_type_id != declared_type_id:
            session.add(
                DeclaredType(
                    id=template_declared_type_id,
                    stable_key=f"other-type-{template_declared_type_id}",
                    active_policy_revision_id=policy_id,
                    active=True,
                )
            )
        if include_template_in_policy:
            session.add(
                ExtractionTemplateRevision(
                    id=template_revision_id,
                    template_id=template_id,
                    declared_type_id=template_declared_type_id or declared_type_id,
                    revision=1,
                    status=template_status,
                    json_schema={"type": "object"},
                    field_dictionary={"fields": []},
                    schema_sha256="v" * 64,
                    created_by_subject="administrator",
                )
            )
        session.add(
            Label(
                id=label_id,
                stable_key=f"internal-{label_id}",
                retention_class="standard",
                active=True,
            )
        )

    storage = MagicMock()
    storage.stream_upload = AsyncMock(return_value=("f" * 64, 14))
    storage.remove_object = AsyncMock()
    storage.quarantine_key.side_effect = lambda version_id: f"quarantine/{version_id}/original"
    policy_service = AsyncMock(spec=PolicyService)
    policy_service.get_role_mappings.return_value = [
        RoleMapping(
            role_key="editor",
            allowed_label_ids={label_id},
            permitted_actions={"upload", "version_create", "delete", "read"},
        )
    ]
    policy_service.get_active_policy.return_value = SimpleNamespace(id=policy_id)
    authorization = AsyncMock()
    authorization.authorize.return_value.decision = "allow"
    audit = AuditService(session_factory)
    service = DocumentService(
        session_factory=session_factory,
        storage_service=storage,
        authorization_service=authorization,
        label_service=LabelService(session_factory),
        policy_service=policy_service,
        audit_service=audit,
        max_upload_bytes=1024,
        cursor_hmac_key=b"x" * 32,
    )
    return service, label_id, declared_type_key, template_revision_id


async def test_admission_pins_configured_active_template_revision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the declared-type policy maps an active template, that revision is pinned."""
    service, label_id, declared_type, template_revision_id = await _create_admission_with_template_policy(
        session_factory
    )

    result = await service.admit_document(
        file=_upload(),
        title="Template pinning test",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )

    async with session_factory() as session:
        version = await session.get(DocumentVersion, result.version_id)
        stage = (
            (
                await session.execute(
                    select(ProcessingStage).where(
                        ProcessingStage.stage_name == "admit",
                    )
                )
            )
            .scalars()
            .all()
        )
        # Find the stage for this version's run
        admit_stage = None
        for s in stage:
            from documind.models.processing import ProcessingRun

            run = await session.get(ProcessingRun, s.processing_run_id)
            if run and run.version_id == result.version_id:
                admit_stage = s
                break
    assert version is not None
    assert version.selected_template_revision_id == template_revision_id
    assert admit_stage is not None
    assert admit_stage.policy_revision_json["template_revision_id"] == str(template_revision_id)


async def test_admission_leaves_template_null_when_policy_has_no_mapping(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the policy intentionally has no template mapping, the selection is NULL."""
    service, label_id, declared_type, _ = await _create_admission_with_template_policy(
        session_factory,
        include_template_in_policy=False,
    )

    result = await service.admit_document(
        file=_upload(),
        title="No template mapping",
        labels=[label_id],
        declared_type=declared_type,
        principal=_principal(),
        idempotency_key=_idempotency_key(),
    )

    async with session_factory() as session:
        version = await session.get(DocumentVersion, result.version_id)
    assert version is not None
    assert version.selected_template_revision_id is None


async def test_admission_rejects_inactive_template_revision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An inactive template revision prevents admission."""
    service, label_id, declared_type, _ = await _create_admission_with_template_policy(
        session_factory,
        template_status=PolicyStatus.SUPERSEDED,
    )

    with pytest.raises(TemplateResolutionError, match="not active"):
        await service.admit_document(
            file=_upload(),
            title="Inactive template",
            labels=[label_id],
            declared_type=declared_type,
            principal=_principal(),
            idempotency_key=_idempotency_key(),
        )


async def test_admission_rejects_template_from_different_declared_type(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A template belonging to a different declared type prevents admission."""
    other_declared_type_id = uuid.uuid4()
    service, label_id, declared_type, _ = await _create_admission_with_template_policy(
        session_factory,
        template_declared_type_id=other_declared_type_id,
    )

    with pytest.raises(TemplateResolutionError, match="different declared type"):
        await service.admit_document(
            file=_upload(),
            title="Wrong type template",
            labels=[label_id],
            declared_type=declared_type,
            principal=_principal(),
            idempotency_key=_idempotency_key(),
        )
