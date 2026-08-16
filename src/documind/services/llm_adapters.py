"""Concrete LLM protocol implementations for production wiring.

Provides a PostgreSQL-backed route resolver, a LiteLLM async adapter,
and an OpenBao-backed credential resolver.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.enums import PolicyStatus
from documind.models.model_route import ModelRouteRevision
from documind.services.llm_service import (
    CredentialResolver,
    LLMAdapter,
    LLMRequest,
    ModelRole,
    ModelRoute,
    ModelRouteError,
    ProviderResponse,
    RouteResolver,
)

logger = structlog.get_logger()


class PostgresRouteResolver:
    """Resolve the newest active model route revision from PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def newest_active(self, role: ModelRole) -> ModelRoute | None:
        """Return the latest active route for ``role``, or None."""
        async with self._session_factory() as session:
            stmt = (
                select(ModelRouteRevision)
                .where(
                    ModelRouteRevision.role == role.value,
                    ModelRouteRevision.status == PolicyStatus.ACTIVE,
                )
                .order_by(ModelRouteRevision.created_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            rev = result.scalar_one_or_none()
            if rev is None:
                return None

            # Route configuration stores provider/model details as JSONB.
            cfg = rev.route_configuration or {}
            return ModelRoute(
                revision_id=rev.id,
                role=ModelRole(rev.role),
                provider_kind=cfg.get("provider_kind", "litellm"),
                model_digest=cfg.get("model_digest", ""),
                model_alias=cfg.get("model_alias", cfg.get("model", "")),
                timeout_seconds=cfg.get("timeout_seconds", 30),
                max_attempts=cfg.get("max_attempts", 1),
                external_consent_id=(
                    rev.external_consent_revision_id
                    if rev.external_consent_revision_id
                    else None
                ),
                secret_reference=rev.secret_reference,
            )


class LiteLLMAdapter:
    """Thin async wrapper over litellm.acompletion."""

    async def invoke(self, request: LLMRequest) -> ProviderResponse:
        """Invoke the model via LiteLLM and return a typed response."""
        import litellm

        kwargs: dict[str, Any] = {
            "model": request.model_alias,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "timeout": request.timeout_seconds,
        }
        if request.credential:
            kwargs["api_key"] = request.credential
        if request.json_schema:
            kwargs["response_format"] = {
                "type": "json_object",
                "schema": request.json_schema,
            }

        response = await litellm.acompletion(**kwargs)
        choice = response.choices[0]
        usage = response.usage
        return ProviderResponse(
            content=choice.message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )


class OpenBaoCredentialResolver:
    """Resolve short-lived credentials from OpenBao."""

    def __init__(self, secret_service: Any) -> None:
        self._secret_service = secret_service

    async def resolve(self, secret_reference: str) -> str:
        """Read the secret value from OpenBao at the given reference path."""
        parts = secret_reference.rsplit("/", 1)
        if len(parts) != 2:
            raise ModelRouteError(f"Invalid secret reference: {secret_reference}")
        path, key = parts
        return await self._secret_service.get_secret(path, key)
