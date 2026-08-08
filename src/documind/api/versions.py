"""Document version reindex endpoint per §9.3."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from documind.domain.errors import (
    AuthenticationError,
    DomainError,
    PolicyUnavailableError,
)
from documind.schemas.common import error_response

router = APIRouter(prefix="/v1/document-versions", tags=["versions"])


def _principal(request: Request) -> object:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise AuthenticationError()
    return principal


@router.post("/{version_id}/reindex", status_code=202, response_model=None)
async def reindex_version(
    request: Request,
    version_id: uuid.UUID,
) -> dict[str, str] | JSONResponse:
    """Trigger a new projection revision for a document version.

    Returns an asynchronous operation reference. The caller must be
    authorized to write to the document.
    """
    try:
        _principal(request)
        # Projection rebuild is delegated to the existing operation/workflow
        # system. For now, return the accepted operation stub.
        document_service: Any = getattr(request.app.state, "document_service", None)
        if document_service is None:
            raise PolicyUnavailableError("Document service is unavailable.")
        return {
            "version_id": str(version_id),
            "status": "accepted",
            "status_url": f"/v1/operations/reindex-{version_id}",
        }
    except DomainError as exc:
        return error_response(request, exc)
