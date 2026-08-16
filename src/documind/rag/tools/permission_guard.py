"""permission_guard — deterministic canonical recheck tool per §7.6.

Receives candidate IDs, reads canonical metadata from PostgreSQL,
returns allowed evidence IDs and a redacted filtered count.  Writes
an audit count for filtered candidates but never places inaccessible
titles, excerpts, IDs, or labels in AgentState.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.models.chunk import DocumentChunk
from documind.models.document import DocumentVersion
from documind.models.enums import DocumentLifecycle
from documind.models.label import DeletionTombstone

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
    allowed_document_ids: set[str],
    audit_service: Any | None = None,
    trace_id: str | None = None,
) -> PermissionGuardOutput:
    """Canonical PostgreSQL recheck on every candidate per §6.3 / §7.6.

    For each candidate chunk ID:
    1. Verify chunk exists and retrieve its version_id
    2. Verify version lifecycle is still completed
    3. Verify document is in the allowed set
    4. Check for deletion tombstone
    """
    allowed: list[str] = []
    filtered = 0

    for chunk_id_str in input_data.candidate_ids:
        try:
            chunk_uuid = uuid.UUID(chunk_id_str)
        except ValueError:
            filtered += 1
            continue

        # Look up the chunk and its version.
        chunk_stmt = select(DocumentChunk).where(DocumentChunk.id == chunk_uuid)
        chunk_result = await session.execute(chunk_stmt)
        chunk = chunk_result.scalar_one_or_none()

        if chunk is None:
            filtered += 1
            continue

        # Verify version lifecycle.
        version_stmt = select(DocumentVersion).where(DocumentVersion.id == chunk.version_id)
        version_result = await session.execute(version_stmt)
        version = version_result.scalar_one_or_none()

        if version is None or version.lifecycle != DocumentLifecycle.COMPLETED:
            filtered += 1
            continue

        # Verify document is in the allowed set.
        doc_id_str = str(version.document_id)
        if doc_id_str not in allowed_document_ids:
            filtered += 1
            continue

        # Check for deletion tombstone on the document.
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
