"""Retrieval and comparison endpoints per §9.3."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from documind.domain.errors import (
    AuthenticationError,
    DomainError,
    PolicyUnavailableError,
)
from documind.schemas.common import error_response
from documind.schemas.retrieval import (
    ComparisonRequest,
    ComparisonResponse,
    RetrievalRequest,
    RetrievalResponse,
)

router = APIRouter(prefix="/v1", tags=["retrieval"])


def _principal(request: Request) -> object:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise AuthenticationError()
    return principal


def _retrieval_service(request: Request) -> Any:
    service = getattr(request.app.state, "retrieval_service", None)
    if service is None:
        raise PolicyUnavailableError("Retrieval service is unavailable.")
    return service


@router.post("/retrieval", response_model=RetrievalResponse)
async def retrieve(
    request: Request,
    body: RetrievalRequest,
) -> RetrievalResponse | JSONResponse:
    """Return evidence without generation per §9.3.

    The server builds all projection filters from the authenticated
    principal. A caller can request a mode preference only when that
    mode is already enabled; the server chooses the actual mode.
    """
    try:
        principal = _principal(request)
        service = _retrieval_service(request)
        return await service.retrieve(body, principal)
    except DomainError as exc:
        return error_response(request, exc)


@router.post("/comparisons", response_model=ComparisonResponse)
async def compare(
    request: Request,
    body: ComparisonRequest,
) -> ComparisonResponse | JSONResponse:
    """Return cited comparison data between two document versions."""
    try:
        principal = _principal(request)
        service = _retrieval_service(request)
        return await service.compare(body, principal)
    except DomainError as exc:
        return error_response(request, exc)
