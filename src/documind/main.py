"""FastAPI application entry point.

Wires up structured logging, OIDC middleware, service instances, and
all API routers.  Service instances are attached to ``app.state`` for
dependency injection into route handlers.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

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
from documind.database import get_engine, get_session_factory, init_database
from documind.domain.authorization_service import AuthorizationService
from documind.domain.document_service import DocumentService
from documind.domain.label_service import LabelService
from documind.domain.policy_service import PolicyService
from documind.schemas.common import validation_error_response
from documind.services.audit_service import AuditService
from documind.services.identity_service import IdentityService
from documind.services.partition_service import ensure_audit_partitions
from documind.services.secret_service import SecretService
from documind.services.storage_service import StorageService
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

# Initialise the database engine and session factory from resolved URL.
init_database()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup and shutdown hooks."""
    # §8.2: ensure audit partitions exist and alert if fewer than 2 future.
    try:
        engine = get_engine()
        health = await ensure_audit_partitions(engine)
        if health.created_partitions:
            logger.info(
                "audit_partitions_ensured",
                created=health.created_partitions,
                future_count=health.existing_future_partitions,
            )
    except Exception:
        logger.exception("audit_partition_startup_failed")

    # T3-1: Wire DocumentService from resolved OpenBao secrets.
    secret_client: SecretService | None = None
    try:
        openbao_token = settings.openbao_auth_ref or ""
        if openbao_token:
            secret_client = SecretService(settings.openbao_addr, openbao_token)

        minio_access = await secret_client.get_secret("documind/minio", "access_key") if secret_client else ""
        minio_secret = await secret_client.get_secret("documind/minio", "secret_key") if secret_client else ""
        cursor_hmac_hex = await secret_client.get_secret("documind/api", "cursor_hmac_key") if secret_client else ""

        if minio_access and minio_secret and cursor_hmac_hex:
            storage = StorageService.from_resolved_credentials(
                endpoint=settings.minio_endpoint,
                access_key=minio_access,
                secret_key=minio_secret,
                secure=settings.minio_secure,
                bucket_name=settings.minio_bucket,
                hard_cap_bytes=settings.upload_hard_cap_bytes,
            )
            await storage.initialize()
            app.state.document_service = DocumentService(
                session_factory=app.state.session_factory,
                storage_service=storage,
                authorization_service=app.state.authorization_service,
                label_service=app.state.label_service,
                policy_service=app.state.policy_service,
                audit_service=app.state.audit_service,
                max_upload_bytes=settings.upload_default_max_bytes,
                cursor_hmac_key=bytes.fromhex(cursor_hmac_hex),
            )
            logger.info("document_service_wired")
        else:
            logger.warning("document_service_unavailable", reason="secrets_not_resolved")
    except Exception:
        logger.exception("document_service_wiring_failed")
    finally:
        if secret_client is not None:
            await secret_client.close()

    yield


logger = structlog.get_logger()

app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError) -> Response:
    """Keep FastAPI validation failures within the public error contract."""
    return validation_error_response(request, exc)


# ---------------------------------------------------------------------------
# Service wiring — attach to app.state for route-handler access
# ---------------------------------------------------------------------------
session_factory = get_session_factory()
app.state.session_factory = session_factory
app.state.settings = settings

# Audit service (hash-chain writer).
app.state.audit_service = AuditService(session_factory=session_factory)

# Identity service (OIDC validation + SCIM projection).
app.state.identity_service = IdentityService(
    settings=settings,
    session_factory=session_factory,
)

# Policy service (versioned policy resolution).
app.state.policy_service = PolicyService(session_factory=session_factory)

# Label service (label validation).
app.state.label_service = LabelService(session_factory=session_factory)

# Authorization service (deterministic authorize()).
app.state.authorization_service = AuthorizationService(
    policy_service=app.state.policy_service,
    label_service=app.state.label_service,
    audit_service=app.state.audit_service,
    session_factory=session_factory,
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
app.state.webhook_service = WebhookService(session_factory=session_factory)

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


