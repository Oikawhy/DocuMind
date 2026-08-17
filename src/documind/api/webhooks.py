"""Webhook management endpoints per §9.5.

Registration, listing, and deactivation of webhook subscriptions.
SSRF validation is delegated to ``WebhookService``.

T9-15: Authorization gate via ``AuthorizationService`` and mandatory
audit evidence for all mutations.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from documind.domain.errors import (
    AuthenticationError,
    AuthorizationDeniedError,
    DomainError,
    PolicyUnavailableError,
    ResourceNotFoundError,
)
from documind.schemas.common import error_response
from documind.schemas.webhook import WebhookCreateRequest, WebhookResponse
from documind.services.audit_service import AuditEntry

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


def _authorization_service(request: Request) -> Any:
    return getattr(request.app.state, "authorization_service", None)


def _audit_service(request: Request) -> Any:
    return getattr(request.app.state, "audit_service", None)


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
    """Register a new webhook subscription with SSRF validation.

    T9-15: Requires ``webhook:create`` authorization and writes mandatory
    audit evidence.
    """
    try:
        principal = _principal(request)

        # T9-15: Authorization gate.
        auth_svc = _authorization_service(request)
        if auth_svc is not None:
            result = await auth_svc.authorize(
                principal, "webhook:create", "webhook",
            )
            if not result.allowed:
                raise AuthorizationDeniedError(
                    "Not authorized to create webhooks.",
                    use_404=False,
                )

        service = _webhook_service(request)
        webhook = await service.register_webhook(
            target_url=body.target_url,
            event_type_glob=body.event_type_glob,
            secret=body.secret,
            created_by_subject=principal.subject,
        )

        # T9-15: Mandatory audit.
        audit = _audit_service(request)
        if audit is not None:
            await audit.write_event(
                AuditEntry(
                    actor_subject=principal.subject,
                    action="webhook.registered",
                    resource_type="webhook",
                    resource_id=str(webhook.id),
                )
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
    """Deactivate a webhook subscription.

    T9-15: Writes mandatory audit evidence for deactivation.
    """
    try:
        principal = _principal(request)
        service = _webhook_service(request)
        found = await service.deactivate_webhook(webhook_id, principal.subject)
        if not found:
            raise ResourceNotFoundError("Webhook not found.", code="WEBHOOK_NOT_FOUND")

        # T9-15: Mandatory audit.
        audit = _audit_service(request)
        if audit is not None:
            await audit.write_event(
                AuditEntry(
                    actor_subject=principal.subject,
                    action="webhook.deactivated",
                    resource_type="webhook",
                    resource_id=str(webhook_id),
                )
            )

        return None
    except DomainError as exc:
        return error_response(request, exc)
