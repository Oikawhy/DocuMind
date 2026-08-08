"""Shared API envelopes, cursor payloads, and safe domain-error mapping."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from documind.domain.errors import (
    AuthenticationError,
    AuthorizationDeniedError,
    ChunkProfileValidationError,
    DomainError,
    LabelValidationError,
    PolicyUnavailableError,
    ResourceConflictError,
    ResourceNotFoundError,
    UploadTooLargeError,
)


class ErrorDetail(BaseModel):
    """Safe per-field validation information."""

    field: str | None = None
    reason: str


class ErrorBody(BaseModel):
    """Architecture-standard machine-readable error payload."""

    code: str
    message: str
    trace_id: uuid.UUID
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Envelope shared by all `/v1` error responses."""

    error: ErrorBody


def request_trace_id(request: Request) -> uuid.UUID:
    """Return a caller trace UUID when valid, otherwise create one."""
    value = request.headers.get("X-Request-ID")
    try:
        return uuid.UUID(value) if value else uuid.uuid4()
    except ValueError:
        return uuid.uuid4()


def error_response(request: Request, error: DomainError) -> JSONResponse:
    """Translate known domain errors into non-leaking API envelopes."""
    status = 400
    if isinstance(error, (AuthenticationError,)):
        status = 401
    elif isinstance(error, AuthorizationDeniedError):
        status = 404 if error.use_404 else 403
    elif isinstance(error, ResourceNotFoundError):
        status = 404
    elif isinstance(error, ResourceConflictError):
        status = 409
    elif isinstance(error, UploadTooLargeError):
        status = 413
    elif isinstance(error, (LabelValidationError, ChunkProfileValidationError)):
        status = 422
    elif isinstance(error, PolicyUnavailableError) or error.code in {
        "AUTHORIZATION_UNAVAILABLE",
        "DEPENDENCY_UNAVAILABLE",
    }:
        status = 503
    trace_id = request_trace_id(request)
    payload = ErrorResponse(error=ErrorBody(code=error.code, message=error.message, trace_id=trace_id))
    return JSONResponse(
        status_code=status,
        content=payload.model_dump(mode="json"),
        headers={"X-Trace-ID": str(trace_id)},
    )


def validation_error_response(request: Request, error: RequestValidationError) -> JSONResponse:
    """Render framework validation failures through the public safe envelope."""
    trace_id = request_trace_id(request)
    details = [
        ErrorDetail(
            field=".".join(str(part) for part in item.get("loc", [])[1:]) or None,
            reason=item.get("msg", "Invalid request."),
        )
        for item in error.errors()
    ]
    payload = ErrorResponse(
        error=ErrorBody(
            code="INVALID_REQUEST",
            message="The request is invalid.",
            trace_id=trace_id,
            details=details,
        )
    )
    return JSONResponse(
        status_code=422,
        content=payload.model_dump(mode="json"),
        headers={"X-Trace-ID": str(trace_id)},
    )


class CursorPage(BaseModel):
    """Generic opaque-cursor result envelope."""

    items: list[dict[str, Any]]
    next_cursor: str | None = None
