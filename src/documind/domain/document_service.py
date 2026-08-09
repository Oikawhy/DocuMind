"""Document admission, immutable versioning, and lifecycle read operations."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import BinaryIO, Protocol

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.domain.authorization_service import AuthorizationDecision
from documind.domain.errors import (
    AuthorizationDeniedError,
    ChunkProfileValidationError,
    InvalidRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
    TemplateResolutionError,
    UploadValidationError,
)
from documind.domain.label_service import LabelService
from documind.domain.policy_service import PolicyService
from documind.models.document import Document, DocumentVersion
from documind.models.enums import DocumentLifecycle, OperationStatus, PolicyStatus, StageStatus
from documind.models.label import DocumentLabel
from documind.models.policy import ChunkProfileRevision, DeclaredType, PolicyRevision
from documind.models.processing import Operation, ProcessingRun, ProcessingStage
from documind.models.template import ExtractionTemplateRevision
from documind.services.audit_service import AuditEntry, AuditService
from documind.services.identity_service import Principal
from documind.services.outbox_service import OutboxService

_IDEMPOTENCY_WINDOW = timedelta(hours=24)
_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{32,128}$")

# The browser-provided content type is only a candidate: the inspect activity
# verifies it against libmagic before the version can progress.  Persisting a
# candidate from the admitted set lets the worker distinguish an Office/ODF
# ZIP container from a generic ZIP upload, while an unknown candidate fails
# closed during inspection.
_DECLARED_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "text/x-markdown": "text/markdown",
}
_ADMITTED_DECLARED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "application/xml",
        "text/xml",
        "image/png",
        "image/jpeg",
        "image/tiff",
    }
)


class StorageProtocol(Protocol):
    """Storage methods DocumentService needs, kept narrow for testability."""

    async def stream_upload(self, reader: BinaryIO, quarantine_key: str) -> tuple[str, int]: ...

    async def remove_object(self, object_key: str, *, ignore_missing: bool = False) -> None: ...

    @staticmethod
    def quarantine_key(version_id: uuid.UUID) -> str: ...


@dataclass(frozen=True)
class UploadSource:
    """Untrusted multipart file data passed from the API boundary."""

    reader: BinaryIO
    filename: str
    content_type: str | None = None


@dataclass(frozen=True)
class AdmissionResult:
    """Stable response payload for an accepted immutable version."""

    document_id: uuid.UUID
    version_id: uuid.UUID
    operation_id: uuid.UUID
    lifecycle_state: str = "accepted"

    @property
    def status_url(self) -> str:
        return f"/v1/operations/{self.operation_id}"

    def as_json(self) -> dict[str, str]:
        return {
            "document_id": str(self.document_id),
            "version_id": str(self.version_id),
            "operation_id": str(self.operation_id),
            "lifecycle_state": self.lifecycle_state,
            "status_url": self.status_url,
        }


@dataclass(frozen=True)
class DocumentPage:
    """Opaque-cursor page of authorization-filtered document metadata."""

    items: list[dict[str, object]]
    next_cursor: str | None


class DocumentService:
    """Coordinate safe admission without allowing storage to dictate lifecycle."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage_service: StorageProtocol,
        authorization_service: object,
        label_service: LabelService,
        policy_service: PolicyService,
        audit_service: AuditService,
        max_upload_bytes: int,
        cursor_hmac_key: bytes,
    ) -> None:
        self._session_factory = session_factory
        self._storage_service = storage_service
        self._authorization_service = authorization_service
        self._label_service = label_service
        self._policy_service = policy_service
        self._audit_service = audit_service
        self._max_upload_bytes = max_upload_bytes
        if len(cursor_hmac_key) < 32:
            raise ValueError("cursor_hmac_key must contain at least 32 bytes")
        self._cursor_hmac_key = cursor_hmac_key

    async def admit_document(
        self,
        *,
        file: UploadSource,
        title: str,
        labels: list[uuid.UUID],
        declared_type: str,
        principal: Principal,
        idempotency_key: str,
        chunk_profile_id: uuid.UUID | None = None,
    ) -> AdmissionResult:
        """Admit a new document and first immutable version.

        Bytes are streamed to a deterministic quarantine object before the
        metadata transaction. If that transaction does not commit, those
        bytes are removed.
        """
        self._validate_admission_request(file=file, title=title, idempotency_key=idempotency_key)
        declared = await self._get_active_declared_type(declared_type)
        profile = await self._resolve_chunk_profile(declared, chunk_profile_id)
        template_revision_id = await self._resolve_template_revision(declared)
        allowed_labels, assignment_policy_id = await self._allowed_labels_and_policy(principal)
        validated_labels = await self._label_service.validate_labels(labels, allowed_labels)
        await self._require_authorized(
            principal,
            "upload",
        )

        document_id = uuid.uuid4()
        version_id = uuid.uuid4()
        quarantine_key = self._storage_service.quarantine_key(version_id)
        content_sha256, byte_size = await self._storage_service.stream_upload(file.reader, quarantine_key)
        if byte_size > self._max_upload_bytes:
            await self._storage_service.remove_object(quarantine_key, ignore_missing=True)
            from documind.domain.errors import UploadTooLargeError

            raise UploadTooLargeError()

        request_hash = self._request_hash(
            target_document_id=None,
            title=title,
            label_ids=[label.id for label in validated_labels],
            declared_type=declared.stable_key,
            profile_id=profile.id,
            content_sha256=content_sha256,
            byte_size=byte_size,
        )
        try:
            async with self._session_factory() as session, session.begin():
                replay = await self._find_idempotent_operation(
                    session,
                    principal.subject,
                    idempotency_key,
                    request_hash,
                )
                if replay is not None:
                    await self._storage_service.remove_object(quarantine_key, ignore_missing=True)
                    return self._result_from_operation(replay)
                return await self._create_admission(
                    session=session,
                    document=Document(
                        id=document_id,
                        title=title,
                        declared_type_id=declared.id,
                        created_by_subject=principal.subject,
                    ),
                    version_id=version_id,
                    version_number=1,
                    file=file,
                    quarantine_key=quarantine_key,
                    content_sha256=content_sha256,
                    byte_size=byte_size,
                    profile_id=profile.id,
                    template_revision_id=template_revision_id,
                    label_ids=[label.id for label in validated_labels],
                    assignment_policy_id=assignment_policy_id,
                    assign_labels=True,
                    principal=principal,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
        except IntegrityError as exc:
            await self._storage_service.remove_object(quarantine_key, ignore_missing=True)
            # T3-3: Re-read the winning operation for idempotency-key conflicts
            # before raising a generic conflict error.
            try:
                async with self._session_factory() as retry_session:
                    winner = await self._find_idempotent_operation(
                        retry_session,
                        principal.subject,
                        idempotency_key,
                        request_hash,
                    )
                    if winner is not None:
                        return self._result_from_operation(winner)
            except Exception:
                pass
            raise ResourceConflictError(
                "A document version with the same content already exists.",
                code="VERSION_CONFLICT",
            ) from exc
        except Exception:
            await self._storage_service.remove_object(quarantine_key, ignore_missing=True)
            raise

    async def admit_version(
        self,
        *,
        document_id: uuid.UUID,
        file: UploadSource,
        principal: Principal,
        idempotency_key: str,
    ) -> AdmissionResult:
        """Add an immutable version while inheriting document labels and policy."""
        self._validate_admission_request(file=file, title="version", idempotency_key=idempotency_key)
        async with self._session_factory() as session:
            document = await session.get(Document, document_id)
            if document is None or document.erased_at is not None:
                raise ResourceNotFoundError()
            label_ids = list(
                (
                    await session.execute(
                        select(DocumentLabel.label_id).where(DocumentLabel.document_id == document_id),
                    )
                )
                .scalars()
                .all(),
            )
            assignment_policy_id = (
                await session.execute(
                    select(DocumentLabel.assignment_policy_revision_id)
                    .where(DocumentLabel.document_id == document_id)
                    .limit(1),
                )
            ).scalar_one_or_none()
            latest = (
                await session.execute(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == document_id)
                    .order_by(desc(DocumentVersion.version_number))
                    .limit(1),
                )
            ).scalar_one_or_none()
            if latest is None:
                raise ResourceConflictError("The document has no immutable version.")
            profile_id = latest.selected_chunk_profile_revision_id
            template_revision_id = latest.selected_template_revision_id
        await self._require_authorized(
            principal,
            "version_create",
            resource_id=document_id,
        )

        version_id = uuid.uuid4()
        quarantine_key = self._storage_service.quarantine_key(version_id)
        content_sha256, byte_size = await self._storage_service.stream_upload(file.reader, quarantine_key)
        if byte_size > self._max_upload_bytes:
            await self._storage_service.remove_object(quarantine_key, ignore_missing=True)
            from documind.domain.errors import UploadTooLargeError

            raise UploadTooLargeError()
        request_hash = self._request_hash(
            target_document_id=document_id,
            title=document.title,
            label_ids=label_ids,
            declared_type=str(document.declared_type_id),
            profile_id=profile_id,
            content_sha256=content_sha256,
            byte_size=byte_size,
        )
        try:
            async with self._session_factory() as session, session.begin():
                document = (
                    await session.execute(
                        select(Document).where(Document.id == document_id).with_for_update(),
                    )
                ).scalar_one_or_none()
                if document is None or document.erased_at is not None:
                    raise ResourceNotFoundError()
                replay = await self._find_idempotent_operation(
                    session,
                    principal.subject,
                    idempotency_key,
                    request_hash,
                )
                if replay is not None:
                    await self._storage_service.remove_object(quarantine_key, ignore_missing=True)
                    return self._result_from_operation(replay)
                last_version = (
                    await session.execute(
                        select(DocumentVersion)
                        .where(DocumentVersion.document_id == document_id)
                        .order_by(desc(DocumentVersion.version_number))
                        .limit(1),
                    )
                ).scalar_one()
                version_number = last_version.version_number + 1
                return await self._create_admission(
                    session=session,
                    document=document,
                    version_id=version_id,
                    version_number=version_number,
                    file=file,
                    quarantine_key=quarantine_key,
                    content_sha256=content_sha256,
                    byte_size=byte_size,
                    profile_id=profile_id,
                    template_revision_id=template_revision_id,
                    label_ids=label_ids,
                    assignment_policy_id=assignment_policy_id or document.declared_type_id,
                    assign_labels=False,
                    principal=principal,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
        except IntegrityError as exc:
            await self._storage_service.remove_object(quarantine_key, ignore_missing=True)
            raise ResourceConflictError(
                "A document version with the same content already exists.",
                code="VERSION_CONFLICT",
            ) from exc
        except Exception:
            await self._storage_service.remove_object(quarantine_key, ignore_missing=True)
            raise

    async def get_document(self, document_id: uuid.UUID, principal: Principal) -> Document:
        """Read a document only after deterministic access evaluation."""
        async with self._session_factory() as session:
            document = await session.get(Document, document_id)
            if document is None or document.erased_at is not None:
                raise ResourceNotFoundError()
            labels = list(
                (
                    await session.execute(
                        select(DocumentLabel.label_id).where(DocumentLabel.document_id == document_id),
                    )
                )
                .scalars()
                .all()
            )
        await self._require_authorized(principal, "read", resource_id=document_id)
        return document

    async def list_documents(
        self,
        *,
        filters: dict[str, object],
        cursor: str | None,
        limit: int,
        principal: Principal,
    ) -> DocumentPage:
        """List only metadata records the caller is deterministically allowed to read."""
        if limit < 1 or limit > 100:
            raise InvalidRequestError("The list limit must be between 1 and 100.")
        cursor_value = self._decode_cursor(cursor) if cursor else None
        async with self._session_factory() as session:
            statement = select(Document).where(Document.erased_at.is_(None))
            declared_type = filters.get("type")
            if declared_type:
                statement = statement.join(DeclaredType).where(DeclaredType.stable_key == declared_type)
            # T3-2: Apply cursor predicate in SQL before ORDER BY/LIMIT.
            if cursor_value is not None:
                cursor_created_at, cursor_id = cursor_value
                statement = statement.where(
                    (Document.created_at < cursor_created_at)
                    | (
                        (Document.created_at == cursor_created_at)
                        & (Document.id < uuid.UUID(cursor_id))
                    )
                )
            statement = statement.order_by(desc(Document.created_at), desc(Document.id)).limit(limit + 1)
            documents = list((await session.execute(statement)).scalars().all())

            requested_labels = {uuid.UUID(str(label)) for label in filters.get("labels", [])}
            required_state = filters.get("state")
            if required_state is not None and required_state not in {"completed", "processing", "failed"}:
                raise InvalidRequestError("The requested lifecycle filter is invalid.")
            page_items: list[dict[str, object]] = []
            page_documents: list[Document] = []
            for document in documents:
                labels = list(
                    (
                        await session.execute(
                            select(DocumentLabel.label_id).where(DocumentLabel.document_id == document.id),
                        )
                    )
                    .scalars()
                    .all()
                )
                if requested_labels and not requested_labels.issubset(set(labels)):
                    continue
                latest = (
                    await session.execute(
                        select(DocumentVersion)
                        .where(DocumentVersion.document_id == document.id)
                        .order_by(desc(DocumentVersion.version_number))
                        .limit(1),
                    )
                ).scalar_one_or_none()
                lifecycle = latest.lifecycle.value if latest else None
                if required_state is not None and lifecycle != required_state:
                    continue
                try:
                    await self._require_authorized(
                        principal,
                        "read",
                        resource_id=document.id,
                    )
                except AuthorizationDeniedError:
                    continue
                page_documents.append(document)
                page_items.append(
                    {
                        "id": str(document.id),
                        "title": document.title,
                        "declared_type_id": str(document.declared_type_id),
                        "created_at": document.created_at.isoformat(),
                        "lifecycle_state": lifecycle,
                    }
                )
                if len(page_items) == limit:
                    break
        next_cursor = None
        if len(documents) > len(page_documents) and page_documents:
            final = page_documents[-1]
            next_cursor = self._encode_cursor(final.created_at.isoformat(), str(final.id))
        return DocumentPage(items=page_items, next_cursor=next_cursor)

    async def list_versions(self, document_id: uuid.UUID, principal: Principal) -> list[DocumentVersion]:
        """Return immutable version summaries after document-level read authorization."""
        await self.get_document(document_id, principal)
        async with self._session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(DocumentVersion)
                        .where(DocumentVersion.document_id == document_id)
                        .order_by(desc(DocumentVersion.version_number)),
                    )
                )
                .scalars()
                .all()
            )

    async def get_version(self, version_id: uuid.UUID, principal: Principal) -> DocumentVersion:
        """Return an immutable version without disclosing inaccessible records."""
        async with self._session_factory() as session:
            version = await session.get(DocumentVersion, version_id)
            if version is None:
                raise ResourceNotFoundError("The requested version is not available.", code="VERSION_NOT_FOUND")
            document = await session.get(Document, version.document_id)
            if document is None or document.erased_at is not None:
                raise ResourceNotFoundError("The requested version is not available.", code="VERSION_NOT_FOUND")
            labels = list(
                (
                    await session.execute(
                        select(DocumentLabel.label_id).where(DocumentLabel.document_id == document.id),
                    )
                )
                .scalars()
                .all()
            )
        await self._require_authorized(
            principal,
            "read_version",
            resource_id=document.id,
        )
        return version

    async def get_operation(
        self,
        operation_id: uuid.UUID,
        principal: Principal,
    ) -> tuple[Operation, list[ProcessingStage]]:
        """Return safe stage progress to the operation owner or authorized reader."""
        async with self._session_factory() as session:
            operation = await session.get(Operation, operation_id)
            if operation is None:
                raise ResourceNotFoundError("The requested operation is not available.", code="OPERATION_NOT_FOUND")
            if operation.requested_by_subject != principal.subject:
                if operation.document_id is None:
                    raise ResourceNotFoundError("The requested operation is not available.", code="OPERATION_NOT_FOUND")
                # get_document creates its own session, so defer authorization
                # until this read-only session is closed.
                document_id = operation.document_id
            else:
                document_id = None
            stages: list[ProcessingStage] = []
            if operation.version_id is not None:
                stages = list(
                    (
                        await session.execute(
                            select(ProcessingStage)
                            .join(ProcessingRun, ProcessingStage.processing_run_id == ProcessingRun.id)
                            .where(ProcessingRun.version_id == operation.version_id)
                            .order_by(ProcessingStage.stage_order),
                        )
                    )
                    .scalars()
                    .all()
                )
        if document_id is not None:
            await self.get_document(document_id, principal)
        return operation, stages

    async def delete_document(
        self,
        *,
        document_id: uuid.UUID,
        principal: Principal,
        idempotency_key: str,
    ) -> Operation:
        """Start retention-aware erasure; irreversible deletion is Task 12."""
        self._validate_idempotency_key(idempotency_key)
        await self.get_document(document_id, principal)
        async with self._session_factory() as session, session.begin():
            locked = (
                await session.execute(
                    select(Document).where(Document.id == document_id).with_for_update(),
                )
            ).scalar_one_or_none()
            if locked is None or locked.erased_at is not None:
                raise ResourceNotFoundError()
            labels = list(
                (
                    await session.execute(
                        select(DocumentLabel.label_id).where(DocumentLabel.document_id == document_id),
                    )
                )
                .scalars()
                .all()
            )
            await self._require_authorized(principal, "delete", resource_id=document_id)
            request_hash = self._request_hash(
                target_document_id=document_id,
                title=locked.title,
                label_ids=labels,
                declared_type=str(locked.declared_type_id),
                profile_id=None,
                content_sha256="",
                byte_size=0,
            )
            replay = await self._find_idempotent_operation(session, principal.subject, idempotency_key, request_hash)
            if replay is not None:
                return replay
            operation = Operation(
                id=uuid.uuid4(),
                operation_type="document_erasure",
                document_id=document_id,
                requested_by_subject=principal.subject,
                idempotency_key_hash=self._idempotency_hash(idempotency_key),
                request_hash=request_hash,
                status=OperationStatus.ACCEPTED,
            )
            locked.deletion_requested_at = datetime.now(UTC)
            session.add(operation)
            outbox = OutboxService(session)
            await outbox.publish_event(
                aggregate_type="document",
                aggregate_id=document_id,
                event_type="io.documind.document.erasure-requested.v1",
                subject=f"document/{document_id}",
                correlation_id=operation.id,
                data={
                    "document_id": str(document_id),
                    "operation_id": str(operation.id),
                    "contract_version": "1.0.0",
                },
            )
            return operation

    async def _create_admission(
        self,
        *,
        session: AsyncSession,
        document: Document,
        version_id: uuid.UUID,
        version_number: int,
        file: UploadSource,
        quarantine_key: str,
        content_sha256: str,
        byte_size: int,
        profile_id: uuid.UUID,
        template_revision_id: uuid.UUID | None,
        label_ids: list[uuid.UUID],
        assignment_policy_id: uuid.UUID,
        assign_labels: bool,
        principal: Principal,
        idempotency_key: str,
        request_hash: str,
    ) -> AdmissionResult:
        session.add(document)
        # The ORM models deliberately avoid broad write relationships. Flush
        # parents explicitly so PostgreSQL sees each FK target in this one
        # transaction before its dependent immutable record.
        await session.flush()
        version = DocumentVersion(
            id=version_id,
            document_id=document.id,
            version_number=version_number,
            original_filename=file.filename,
            declared_mime_family=self._declared_mime_family(file.content_type),
            byte_size=byte_size,
            content_sha256=content_sha256,
            quarantine_object_key=quarantine_key,
            lifecycle=DocumentLifecycle.ACCEPTED,
            selected_chunk_profile_revision_id=profile_id,
            selected_template_revision_id=template_revision_id,
            created_by_subject=principal.subject,
        )
        operation = Operation(
            id=uuid.uuid4(),
            operation_type="document_version_processing",
            document_id=document.id,
            version_id=version_id,
            requested_by_subject=principal.subject,
            idempotency_key_hash=self._idempotency_hash(idempotency_key),
            request_hash=request_hash,
            status=OperationStatus.ACCEPTED,
            temporal_workflow_id=f"document-version/{version_id}",
        )
        session.add(version)
        await session.flush()
        session.add(operation)
        await session.flush()
        if assign_labels:
            for label_id in label_ids:
                session.add(
                    DocumentLabel(
                        document_id=document.id,
                        label_id=label_id,
                        assignment_policy_revision_id=assignment_policy_id,
                        assigned_by_subject=principal.subject,
                    )
                )
            await session.flush()
        result = AdmissionResult(document.id, version_id, operation.id)
        operation.result_json = result.as_json()
        outbox_event = await OutboxService(session).publish_event(
            aggregate_type="document_version",
            aggregate_id=version_id,
            event_type="io.documind.document-version.accepted.v1",
            subject=f"document-version/{version_id}",
            correlation_id=operation.id,
            data={
                "document_id": str(document.id),
                "version_id": str(version_id),
                "version_number": version_number,
                "content_sha256": content_sha256,
                "lifecycle_state": "accepted",
                "contract_version": "1.0.0",
                "correlation_id": str(operation.id),
            },
        )
        run = ProcessingRun(
            id=uuid.uuid4(),
            version_id=version_id,
            temporal_workflow_id=operation.temporal_workflow_id,
            temporal_run_id="pending",
            trigger_event_id=outbox_event.id,
            state="accepted",
        )
        session.add(run)
        await session.flush()
        stage_input = hashlib.sha256(f"{version_id}:{content_sha256}".encode()).hexdigest()
        session.add(
            ProcessingStage(
                id=uuid.uuid4(),
                processing_run_id=run.id,
                stage_name="admit",
                stage_order=0,
                status=StageStatus.SUCCEEDED,
                idempotency_key=hashlib.sha256(f"admit:{stage_input}".encode()).hexdigest(),
                input_sha256=stage_input,
                output_sha256=content_sha256,
                policy_revision_json={
                    "chunk_profile_revision_id": str(profile_id),
                    "template_revision_id": str(template_revision_id) if template_revision_id else None,
                },
                trace_id=operation.id,
            )
        )
        await session.flush()
        await self._write_admission_audit(
            session,
            principal,
            document.id,
            version_id,
            content_sha256,
            byte_size,
            template_revision_id,
        )
        return result

    async def _write_admission_audit(
        self,
        session: AsyncSession,
        principal: Principal,
        document_id: uuid.UUID,
        version_id: uuid.UUID,
        content_sha256: str,
        byte_size: int,
        template_revision_id: uuid.UUID | None = None,
    ) -> None:
        entry = AuditEntry(
            actor_subject=principal.subject,
            action="document_version.accepted",
            resource_type="document_version",
            resource_id=str(version_id),
            details={
                "document_id": str(document_id),
                "content_sha256": content_sha256,
                "byte_size": byte_size,
                "template_revision_id": str(template_revision_id) if template_revision_id else None,
            },
        )
        await self._audit_service.write_event_in_session(session, entry)

    async def _allowed_labels_and_policy(self, principal: Principal) -> tuple[set[uuid.UUID], uuid.UUID]:
        mappings = await self._policy_service.get_role_mappings(principal.groups)
        allowed = {label_id for mapping in mappings for label_id in mapping.allowed_label_ids}
        assignment_policy_id = next(
            (
                mapping.policy_revision_id
                for mapping in mappings
                if "upload" in mapping.permitted_actions and mapping.policy_revision_id is not None
            ),
            None,
        )
        if assignment_policy_id is not None:
            return allowed, assignment_policy_id
        # Compatibility fallback for pre-Task-3 policy projections. New
        # mappings always carry their immutable revision ID above.
        policy = await self._policy_service.get_active_policy("authorization", "admission")
        if policy is None:
            raise InvalidRequestError("No active admission policy is available.", code="AUTHORIZATION_UNAVAILABLE")
        return allowed, policy.id

    async def _get_active_declared_type(self, stable_key: str) -> DeclaredType:
        async with self._session_factory() as session:
            declared = (
                await session.execute(
                    select(DeclaredType).where(
                        DeclaredType.stable_key == stable_key,
                        DeclaredType.active.is_(True),
                    ),
                )
            ).scalar_one_or_none()
        if declared is None:
            raise InvalidRequestError("The declared type is not active.", code="DECLARED_TYPE_INVALID")
        return declared

    async def _resolve_chunk_profile(
        self,
        declared_type: DeclaredType,
        selected_profile_id: uuid.UUID | None,
    ) -> ChunkProfileRevision:
        async with self._session_factory() as session:
            if selected_profile_id is not None:
                profile = await session.get(ChunkProfileRevision, selected_profile_id)
                if profile is not None and profile.status == PolicyStatus.ACTIVE:
                    return profile
                raise ChunkProfileValidationError()
            policy = await session.get(PolicyRevision, declared_type.active_policy_revision_id)
            configured_id = policy.body.get("chunk_profile_revision_id") if policy else None
            if configured_id:
                try:
                    profile = await session.get(ChunkProfileRevision, uuid.UUID(str(configured_id)))
                except ValueError:
                    profile = None
                if profile is not None and profile.status == PolicyStatus.ACTIVE:
                    return profile
            profiles = list(
                (
                    await session.execute(
                        select(ChunkProfileRevision).where(ChunkProfileRevision.status == PolicyStatus.ACTIVE),
                    )
                )
                .scalars()
                .all()
            )
        if len(profiles) == 1:
            return profiles[0]
        raise ChunkProfileValidationError()

    async def _resolve_template_revision(
        self,
        declared_type: DeclaredType,
    ) -> uuid.UUID | None:
        """Resolve the extraction template revision from the declared-type policy.

        Returns ``None`` when the policy intentionally has no template mapping.
        Raises ``TemplateResolutionError`` when a configured revision exists but
        is inactive, missing, or belongs to a different declared type.
        """
        async with self._session_factory() as session:
            policy = await session.get(PolicyRevision, declared_type.active_policy_revision_id)
            configured_id = policy.body.get("extraction_template_revision_id") if policy else None
            if not configured_id:
                return None
            try:
                template = await session.get(ExtractionTemplateRevision, uuid.UUID(str(configured_id)))
            except ValueError:
                raise TemplateResolutionError(
                    "The policy references an invalid extraction template revision identifier."
                ) from None
            if template is None:
                raise TemplateResolutionError("The configured extraction template revision does not exist.")
            if template.status != PolicyStatus.ACTIVE:
                raise TemplateResolutionError("The configured extraction template revision is not active.")
            if template.declared_type_id != declared_type.id:
                raise TemplateResolutionError(
                    "The configured extraction template belongs to a different declared type."
                )
            return template.id

    async def _find_idempotent_operation(
        self,
        session: AsyncSession,
        subject: str,
        raw_key: str,
        request_hash: str,
    ) -> Operation | None:
        key_hash = self._idempotency_hash(raw_key)
        operation = (
            await session.execute(
                select(Operation)
                .where(
                    Operation.requested_by_subject == subject,
                    Operation.idempotency_key_hash == key_hash,
                )
                .with_for_update(),
            )
        ).scalar_one_or_none()
        if operation is None:
            return None
        if operation.created_at < datetime.now(UTC) - _IDEMPOTENCY_WINDOW:
            operation.idempotency_key_hash = None
            await session.flush()
            return None
        if operation.request_hash != request_hash:
            raise ResourceConflictError(
                "The idempotency key was used for a different request.",
                code="IDEMPOTENCY_CONFLICT",
            )
        return operation

    async def _require_authorized(
        self,
        principal: Principal,
        action: str,
        *,
        resource_id: uuid.UUID | None = None,
    ) -> None:
        result = await self._authorization_service.authorize(
            principal=principal,
            action=action,
            resource_type="document",
            resource_id=resource_id,
        )
        decision = getattr(result.decision, "value", result.decision)
        if decision != AuthorizationDecision.ALLOW.value:
            raise AuthorizationDeniedError(use_404=action == "read")

    @staticmethod
    def _validate_admission_request(*, file: UploadSource, title: str, idempotency_key: str) -> None:
        if not title or len(title) > 1024:
            raise UploadValidationError("The title must be between 1 and 1024 characters.")
        if not file.filename or len(file.filename) > 255 or PurePath(file.filename).name != file.filename:
            raise UploadValidationError("The upload filename is unsafe.")
        if any(ord(char) < 32 for char in file.filename):
            raise UploadValidationError("The upload filename is unsafe.")
        DocumentService._validate_idempotency_key(idempotency_key)

    @staticmethod
    def _declared_mime_family(content_type: str | None) -> str:
        """Canonicalize the untrusted multipart MIME candidate.

        This intentionally does not grant trust to the client header.  The
        scanner subsequently requires an allowed magic-byte MIME that matches
        this value (including the safe ZIP/XML aliases).  Unsupported or
        missing headers are retained as an explicit non-admitted value so the
        inspection stage rejects them rather than treating them as a generic
        successful upload.
        """
        candidate = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
        candidate = _DECLARED_MIME_ALIASES.get(candidate, candidate)
        return candidate if candidate in _ADMITTED_DECLARED_MIME_TYPES else "application/octet-stream"

    @staticmethod
    def _validate_idempotency_key(key: str) -> None:
        if not _IDEMPOTENCY_KEY.fullmatch(key):
            raise InvalidRequestError("Idempotency-Key must contain 32 to 128 printable characters.")

    @staticmethod
    def _idempotency_hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @staticmethod
    def _request_hash(
        *,
        target_document_id: uuid.UUID | None,
        title: str,
        label_ids: list[uuid.UUID],
        declared_type: str,
        profile_id: uuid.UUID | None,
        content_sha256: str,
        byte_size: int,
    ) -> str:
        canonical = {
            "target_document_id": str(target_document_id) if target_document_id else None,
            "title": title,
            "label_ids": sorted(str(label_id) for label_id in label_ids),
            "declared_type": declared_type,
            "profile_id": str(profile_id) if profile_id else None,
            "content_sha256": content_sha256,
            "byte_size": byte_size,
        }
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _result_from_operation(operation: Operation) -> AdmissionResult:
        if not operation.document_id or not operation.version_id:
            raise ResourceConflictError("The previous operation has no document-version result.")
        return AdmissionResult(
            document_id=operation.document_id,
            version_id=operation.version_id,
            operation_id=operation.id,
        )

    def _encode_cursor(self, created_at: str, document_id: str) -> str:
        payload = json.dumps([created_at, document_id], separators=(",", ":")).encode()
        signature = hmac.new(self._cursor_hmac_key, payload, hashlib.sha256).digest()
        encoded_payload = urlsafe_b64encode(payload).decode().rstrip("=")
        encoded_signature = urlsafe_b64encode(signature).decode().rstrip("=")
        return f"{encoded_payload}.{encoded_signature}"

    def _decode_cursor(self, cursor: str) -> tuple[str, str]:
        try:
            encoded_payload, encoded_signature = cursor.split(".", 1)
            payload = urlsafe_b64decode((encoded_payload + "=" * (-len(encoded_payload) % 4)).encode())
            signature = urlsafe_b64decode((encoded_signature + "=" * (-len(encoded_signature) % 4)).encode())
            expected = hmac.new(self._cursor_hmac_key, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            value = json.loads(payload)
            created_at, document_id = value
            uuid.UUID(document_id)
            if not isinstance(created_at, str):
                raise ValueError
            return created_at, document_id
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidRequestError("The document cursor is invalid.", code="INVALID_CURSOR") from exc
