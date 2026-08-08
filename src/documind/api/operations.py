"""Asynchronous operation polling endpoint."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from documind.api.documents import _principal, _service
from documind.domain.errors import DomainError
from documind.schemas.common import error_response
from documind.schemas.document import OperationResponse, OperationStageResponse

router = APIRouter(prefix="/v1/operations", tags=["operations"])


@router.get("/{operation_id}", response_model=OperationResponse)
async def get_operation(request: Request, operation_id: uuid.UUID) -> OperationResponse | JSONResponse:
    """Poll safe operation/stage status for the owner or authorized administrator."""
    try:
        operation, stages = await _service(request).get_operation(operation_id, _principal(request))
        return OperationResponse(
            id=operation.id,
            type=operation.operation_type,
            status=getattr(operation.status, "value", operation.status),
            document_id=operation.document_id,
            version_id=operation.version_id,
            safe_error_code=operation.safe_error_code,
            stages=[
                OperationStageResponse(
                    name=stage.stage_name,
                    status=getattr(stage.status, "value", stage.status),
                    trace_id=stage.trace_id,
                    started_at=stage.started_at,
                    ended_at=stage.ended_at,
                    safe_error_code=stage.safe_error_code,
                )
                for stage in stages
            ],
        )
    except DomainError as exc:
        return error_response(request, exc)
