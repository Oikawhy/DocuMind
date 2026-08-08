"""Authenticated document admission, metadata, version, and deletion routes."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from documind.domain.document_service import AdmissionResult, UploadSource
from documind.domain.errors import AuthenticationError, DomainError, PolicyUnavailableError
from documind.schemas.common import CursorPage, error_response
from documind.schemas.document import AdmissionResponse, DocumentResponse, DocumentVersionResponse

router = APIRouter(prefix="/v1", tags=["documents"])


def _principal(request: Request) -> object:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise AuthenticationError()
    return principal


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "document_service", None)
    if service is None:
        raise PolicyUnavailableError("Document admission service is unavailable.")
    return service


def _admission_response(result: AdmissionResult) -> AdmissionResponse:
    return AdmissionResponse(
        document_id=result.document_id,
        version_id=result.version_id,
        operation_id=result.operation_id,
        lifecycle_state=result.lifecycle_state,
        status_url=result.status_url,
    )


@router.post("/documents", status_code=202, response_model=AdmissionResponse)
async def admit_document(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    title: Annotated[str, Form(...)],
    declared_type: Annotated[str, Form(...)],
    labels: Annotated[list[uuid.UUID], Form(...)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    chunk_profile_id: Annotated[uuid.UUID | None, Form()] = None,
) -> AdmissionResponse | JSONResponse:
    """Stream a new document into private quarantine and return its operation."""
    try:
        result = await _service(request).admit_document(
            file=UploadSource(reader=file.file, filename=file.filename or "", content_type=file.content_type),
            title=title,
            labels=labels,
            declared_type=declared_type,
            principal=_principal(request),
            idempotency_key=idempotency_key,
            chunk_profile_id=chunk_profile_id,
        )
        return _admission_response(result)
    except DomainError as exc:
        return error_response(request, exc)


@router.post("/documents/{document_id}/versions", status_code=202, response_model=AdmissionResponse)
async def admit_version(
    request: Request,
    document_id: uuid.UUID,
    file: Annotated[UploadFile, File(...)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> AdmissionResponse | JSONResponse:
    """Create another immutable version; labels remain document-scoped."""
    try:
        result = await _service(request).admit_version(
            document_id=document_id,
            file=UploadSource(reader=file.file, filename=file.filename or "", content_type=file.content_type),
            principal=_principal(request),
            idempotency_key=idempotency_key,
        )
        return _admission_response(result)
    except DomainError as exc:
        return error_response(request, exc)


@router.get("/documents", response_model=CursorPage)
async def list_documents(
    request: Request,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    type: str | None = None,
    label: Annotated[list[uuid.UUID] | None, Query()] = None,
    state: str | None = None,
) -> CursorPage | JSONResponse:
    """Return authorized document metadata through opaque cursor pagination."""
    try:
        page = await _service(request).list_documents(
            filters={"type": type, "labels": label or [], "state": state},
            cursor=cursor,
            limit=limit,
            principal=_principal(request),
        )
        return CursorPage(items=page.items, next_cursor=page.next_cursor)
    except DomainError as exc:
        return error_response(request, exc)


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document(request: Request, document_id: uuid.UUID) -> DocumentResponse | JSONResponse:
    """Return one document only after deterministic read authorization."""
    try:
        document = await _service(request).get_document(document_id, _principal(request))
        versions = await _service(request).list_versions(document_id, _principal(request))
        return DocumentResponse(
            id=document.id,
            title=document.title,
            declared_type_id=document.declared_type_id,
            created_at=document.created_at,
            deletion_requested_at=document.deletion_requested_at,
            versions=[_version_response(version) for version in versions],
        )
    except DomainError as exc:
        return error_response(request, exc)


@router.get("/document-versions/{version_id}", response_model=DocumentVersionResponse)
async def get_version(request: Request, version_id: uuid.UUID) -> DocumentVersionResponse | JSONResponse:
    """Return a non-sensitive immutable version summary."""
    try:
        version = await _service(request).get_version(version_id, _principal(request))
        return _version_response(version)
    except DomainError as exc:
        return error_response(request, exc)


@router.delete("/documents/{document_id}", status_code=202, response_model=None)
async def delete_document(
    request: Request,
    document_id: uuid.UUID,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, str] | JSONResponse:
    """Start an asynchronous, retention-aware erasure operation."""
    try:
        operation = await _service(request).delete_document(
            document_id=document_id,
            principal=_principal(request),
            idempotency_key=idempotency_key,
        )
        return {
            "operation_id": str(operation.id),
            "status_url": f"/v1/operations/{operation.id}",
        }
    except DomainError as exc:
        return error_response(request, exc)


def _version_response(version: Any) -> DocumentVersionResponse:
    return DocumentVersionResponse(
        id=version.id,
        version_number=version.version_number,
        lifecycle_state=getattr(version.lifecycle, "value", version.lifecycle),
        original_filename=version.original_filename,
        byte_size=version.byte_size,
        content_sha256=version.content_sha256,
        created_at=version.created_at,
        completed_at=version.completed_at,
        failure_code=version.failure_code,
    )
