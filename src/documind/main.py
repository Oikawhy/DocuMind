"""FastAPI application entry point.

Wires up structured logging, OIDC middleware, service instances, and
all API routers.  Service instances are attached to ``app.state`` for
dependency injection into route handlers.
"""

from __future__ import annotations

import logging

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError

from documind.api.chat import router as chat_router
from documind.api.documents import router as documents_router
from documind.api.health import router as health_router
from documind.api.identity import router as identity_router
from documind.api.middleware import oidc_middleware
from documind.api.operations import router as operations_router
from documind.api.retrieval import router as retrieval_router
from documind.api.versions import router as versions_router
from documind.api.webhooks import router as webhooks_router
from documind.api.scim import router as scim_router
from documind.config import settings
from documind.database import AsyncSessionLocal
from documind.domain.authorization_service import AuthorizationService
from documind.domain.label_service import LabelService
from documind.domain.policy_service import PolicyService
from documind.schemas.common import validation_error_response
from documind.services.audit_service import AuditService
from documind.services.identity_service import IdentityService
from documind.services.webhook_service import WebhookService


def configure_logging() -> None:
    """Configure structured logging for the bootstrap application."""
    logging.basicConfig(format="%(message)s", level=logging.DEBUG if settings.debug else logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG if settings.debug else logging.INFO),
        cache_logger_on_first_use=True,
    )


configure_logging()

app = FastAPI(title=settings.app_name, debug=settings.debug)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError) -> Response:
    """Keep FastAPI validation failures within the public error contract."""
    return validation_error_response(request, exc)


# ---------------------------------------------------------------------------
# Service wiring — attach to app.state for route-handler access
# ---------------------------------------------------------------------------
app.state.session_factory = AsyncSessionLocal
app.state.settings = settings

# Audit service (hash-chain writer).
app.state.audit_service = AuditService(session_factory=AsyncSessionLocal)

# Identity service (OIDC validation + SCIM projection).
app.state.identity_service = IdentityService(
    settings=settings,
    session_factory=AsyncSessionLocal,
)

# Policy service (versioned policy resolution).
app.state.policy_service = PolicyService(session_factory=AsyncSessionLocal)

# Label service (label validation).
app.state.label_service = LabelService(session_factory=AsyncSessionLocal)

# Authorization service (deterministic authorize()).
app.state.authorization_service = AuthorizationService(
    policy_service=app.state.policy_service,
    label_service=app.state.label_service,
    audit_service=app.state.audit_service,
    session_factory=AsyncSessionLocal,
)

# Task 3 routes remain dependency-safe while deployment wiring resolves the
# MinIO credential references through OpenBao. Tests and the application
# lifespan supply the fully configured service instance.
app.state.document_service = None

# Retrieval service is wired during startup when projection stores are ready.
app.state.retrieval_service = None

# RAG service is wired during startup when the full LangGraph pipeline is ready.
app.state.rag_service = None

# LLM service is wired during startup when model routes are configured.
app.state.llm_service = None

# Webhook service for delivery and SSRF-safe registration.
app.state.webhook_service = WebhookService(session_factory=AsyncSessionLocal)

# ---------------------------------------------------------------------------
# Middleware — OIDC fail-closed authentication
# ---------------------------------------------------------------------------


@app.middleware("http")
async def oidc_auth(request: Request, call_next: object) -> Response:
    """Delegate to the fail-closed OIDC middleware."""
    return await oidc_middleware(request, call_next)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(health_router)
app.include_router(identity_router)
app.include_router(scim_router)
app.include_router(documents_router)
app.include_router(operations_router)
app.include_router(retrieval_router)
app.include_router(versions_router)
app.include_router(chat_router)
app.include_router(webhooks_router)

