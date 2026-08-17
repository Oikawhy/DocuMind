"""permission_guard — deterministic canonical recheck tool per §7.6.

Receives candidate IDs, reads canonical metadata from PostgreSQL,
returns allowed evidence IDs and a redacted filtered count.  Writes
an audit count for filtered candidates but never places inaccessible
titles, excerpts, IDs, or labels in AgentState.

T8-06: Uses ``AuthorizationContext.authorize()`` for full §4.2
authorization decisions (principal, labels, lifecycle, holds,
tombstones, policy) instead of static document-ID set checks.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.models.chunk import DocumentChunk

if TYPE_CHECKING:
    from documind.domain.authorization_context import AuthorizationContext

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Versioned input / output schemas
# ---------------------------------------------------------------------------


class PermissionGuardInput(BaseModel):
    """Input schema for permission_guard tool."""

    candidate_ids: list[str]
    principal_subject: str
    authorization_context_id: str | None = None
    schema_version: str = SCHEMA_VERSION


class PermissionGuardOutput(BaseModel):
    """Output schema for permission_guard tool."""

    allowed_ids: list[str] = Field(default_factory=list)
    filtered_count: int = 0
    audit_event_id: str | None = None
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def permission_guard(
    input_data: PermissionGuardInput,
    session: AsyncSession,
    auth_context: AuthorizationContext | None = None,
    allowed_document_ids: set[str] | None = None,
    audit_service: Any | None = None,
    trace_id: str | None = None,
) -> PermissionGuardOutput:
    """Canonical PostgreSQL recheck on every candidate per §6.3 / §7.6.

    T8-06: When ``auth_context`` is provided, uses the full
    ``AuthorizationService.authorize()`` call per candidate for
    deterministic authorization (principal, labels, lifecycle, holds,
    tombstones, policy).  Falls back to static document-ID set check
    only when no auth_context is available.
    """
    from documind.domain.authorization_service import AuthorizationDecision

    allowed: list[str] = []
    filtered = 0

    for chunk_id_str in input_data.candidate_ids:
        try:
            chunk_uuid = uuid.UUID(chunk_id_str)
        except ValueError:
            filtered += 1
            continue

        # Look up the chunk to get its version/document context.
        chunk_stmt = select(DocumentChunk).where(DocumentChunk.id == chunk_uuid)
        chunk_result = await session.execute(chunk_stmt)
        chunk = chunk_result.scalar_one_or_none()

        if chunk is None:
            filtered += 1
            continue

        # T8-06: Full authorization check via AuthorizationContext.
        if auth_context is not None:
            try:
                auth_result = await auth_context.authorize(
                    "retrieval", "document_chunk", chunk_uuid,
                )
                if auth_result.decision != AuthorizationDecision.ALLOW:
                    filtered += 1
                    continue
            except Exception:
                # Fail-closed: authorization error → filter the candidate.
                filtered += 1
                continue
        else:
            # Legacy fallback: static document-ID set check.
            from documind.models.document import DocumentVersion
            from documind.models.enums import DocumentLifecycle
            from documind.models.label import DeletionTombstone

            version_stmt = select(DocumentVersion).where(
                DocumentVersion.id == chunk.version_id,
            )
            version_result = await session.execute(version_stmt)
            version = version_result.scalar_one_or_none()

            if version is None or version.lifecycle != DocumentLifecycle.COMPLETED:
                filtered += 1
                continue

            doc_id_str = str(version.document_id)
            effective_doc_ids = allowed_document_ids or set()
            if effective_doc_ids and doc_id_str not in effective_doc_ids:
                filtered += 1
                continue

            tombstone_stmt = select(DeletionTombstone).where(
                DeletionTombstone.document_id == version.document_id,
                DeletionTombstone.scope == "document",
            )
            tombstone_result = await session.execute(tombstone_stmt)
            if tombstone_result.scalar_one_or_none() is not None:
                filtered += 1
                continue

        allowed.append(chunk_id_str)

    # Write audit count for filtered candidates.
    audit_event_id: str | None = None
    if audit_service is not None and filtered > 0:
        from documind.services.audit_service import AuditEntry

        entry = AuditEntry(
            actor_subject=input_data.principal_subject,
            action="rag.permission_guard.filtered",
            resource_type="evidence_candidates",
            details={"filtered_count": filtered, "allowed_count": len(allowed)},
            trace_id=uuid.UUID(trace_id) if trace_id else None,
        )
        await audit_service.write_event(entry)
        audit_event_id = str(uuid.uuid4())

    return PermissionGuardOutput(
        allowed_ids=allowed,
        filtered_count=filtered,
        audit_event_id=audit_event_id,
    )
