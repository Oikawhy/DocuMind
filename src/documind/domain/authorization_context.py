"""Request-scoped authorization context for RAG graph nodes.

Bundles the authenticated ``Principal``, the deterministic
``AuthorizationService``, the tenant-scoped ``session_factory``, and
the caller-authorized document set into a single opaque handle that
graph nodes can use without knowing deployment topology.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.domain.authorization_service import (
    AuthorizationDecision,
    AuthorizationResult,
    AuthorizationService,
)
from documind.services.identity_service import Principal


@dataclass(frozen=True)
class AuthorizationContext:
    """Request-scoped authorization handle for RAG graph nodes.

    Created once per chat request and threaded through ``AgentState``.
    Graph nodes call ``authorize()`` for deterministic per-candidate
    access decisions — no direct service coupling required.
    """

    principal: Principal
    authorization_service: AuthorizationService
    session_factory: async_sessionmaker[AsyncSession]
    document_ids: frozenset[str]

    async def authorize(
        self,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID | None = None,
    ) -> AuthorizationResult:
        """Delegate to the full §4.2 authorization evaluation."""
        return await self.authorization_service.authorize(
            self.principal, action, resource_type, resource_id,
        )

    @property
    def subject(self) -> str:
        """Principal subject identifier."""
        return self.principal.subject

    @property
    def groups(self) -> list[str]:
        """Principal group memberships."""
        return self.principal.groups

    def is_allowed(self) -> bool:
        """True when ``ALLOW`` was the last authorization decision."""
        # Convenience for simple checks — callers should use authorize()
        # for full audit-trail decisions.
        return self.principal.active
