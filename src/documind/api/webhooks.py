"""Webhook management endpoints per §9.5.

Registration, listing, and deactivation of webhook subscriptions.
SSRF validation is delegated to ``WebhookService``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from documind.domain.errors import (
    AuthenticationError,
    DomainError,
    PolicyUnavailableError,
    ResourceNotFoundError,
)
from documind.schemas.common import error_response
from documind.schemas.webhook import WebhookCreateRequest, WebhookResponse

router = APIRouter(prefix="/v1", tags=["webhooks"])


def _principal(request: Request) -> object:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise AuthenticationError()
    return principal


def _webhook_service(request: Request) -> object:
    service = getattr(request.app.state, "webhook_service", None)
    if service is None:
        raise PolicyUnavailableError("Webhook service is unavailable.")
    return service


def _to_response(webhook: object) -> WebhookResponse:
    return WebhookResponse(
        id=webhook.id,
        target_url=webhook.target_url,
        event_type_glob=webhook.event_type_glob,
        active=webhook.active,
        failure_streak=webhook.failure_streak,
        created_at=webhook.created_at,
    )


@router.post("/webhooks", status_code=201, response_model=WebhookResponse)
async def register_webhook(
    request: Request,
    body: WebhookCreateRequest,
) -> WebhookResponse | JSONResponse:
    """Register a new webhook subscription with SSRF validation."""
    try:
        principal = _principal(request)
        service = _webhook_service(request)
        webhook = await service.register_webhook(
            target_url=body.target_url,
            event_type_glob=body.event_type_glob,
            secret=body.secret,
            created_by_subject=principal.subject,
        )
        return _to_response(webhook)
    except DomainError as exc:
        return error_response(request, exc)


@router.get("/webhooks", response_model=None)
async def list_webhooks(
    request: Request,
) -> list[WebhookResponse] | JSONResponse:
    """List webhooks owned by the authenticated caller."""
    try:
        principal = _principal(request)
        service = _webhook_service(request)
        webhooks = await service.list_webhooks(principal.subject)
        return [_to_response(w) for w in webhooks]
    except DomainError as exc:
        return error_response(request, exc)


@router.delete("/webhooks/{webhook_id}", status_code=204, response_model=None)
async def deactivate_webhook(
    request: Request,
    webhook_id: uuid.UUID,
) -> JSONResponse | None:
    """Deactivate a webhook subscription."""
    try:
        principal = _principal(request)
        service = _webhook_service(request)
        found = await service.deactivate_webhook(webhook_id, principal.subject)
        if not found:
            raise ResourceNotFoundError("Webhook not found.", code="WEBHOOK_NOT_FOUND")
        return None
    except DomainError as exc:
        return error_response(request, exc)
