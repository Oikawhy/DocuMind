"""Pydantic contracts for document admission, metadata, and operations."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AdmissionResponse(BaseModel):
    document_id: uuid.UUID
    version_id: uuid.UUID
    operation_id: uuid.UUID
    lifecycle_state: str = "accepted"
    status_url: str


class DocumentVersionResponse(BaseModel):
    id: uuid.UUID
    version_number: int
    lifecycle_state: str
    original_filename: str
    byte_size: int
    content_sha256: str
    created_at: datetime
    completed_at: datetime | None = None
    failure_code: str | None = None


class DocumentResponse(BaseModel):
    id: uuid.UUID
    title: str
    declared_type_id: uuid.UUID
    created_at: datetime
    deletion_requested_at: datetime | None = None
    versions: list[DocumentVersionResponse] = Field(default_factory=list)


class DocumentSummaryResponse(BaseModel):
    id: uuid.UUID
    title: str
    declared_type_id: uuid.UUID
    created_at: datetime
    lifecycle_state: str | None = None


class OperationStageResponse(BaseModel):
    name: str
    status: str
    trace_id: uuid.UUID
    started_at: datetime | None = None
    ended_at: datetime | None = None
    safe_error_code: str | None = None


class OperationResponse(BaseModel):
    id: uuid.UUID
    type: str
    status: str
    document_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    safe_error_code: str | None = None
    stages: list[OperationStageResponse] = Field(default_factory=list)
