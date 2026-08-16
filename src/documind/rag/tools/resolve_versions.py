"""resolve_versions — deterministic version resolution tool per §7.6.

Translates explicit version numbers, latest-completed, date ranges, and
comparison selectors into canonical version IDs within an authorized
document set.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.models.document import DocumentVersion
from documind.models.enums import DocumentLifecycle
from documind.models.label import DeletionTombstone
from documind.rag.state import VersionRef

# ---------------------------------------------------------------------------
# Versioned input / output schemas
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.0.0"


class VersionSelector(BaseModel):
    """A single version selection request."""

    document_id: str
    selector: str = "latest_completed"  # "latest_completed", "v3", "2025-01-01..2025-06-30"
    schema_version: str = SCHEMA_VERSION


class ResolveVersionsInput(BaseModel):
    """Input schema for resolve_versions tool."""

    selectors: list[VersionSelector] = Field(..., max_length=20)
    principal_subject: str
    schema_version: str = SCHEMA_VERSION


class ResolveVersionsOutput(BaseModel):
    """Output schema for resolve_versions tool."""

    resolved: list[dict[str, Any]] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def resolve_versions(
    input_data: ResolveVersionsInput,
    session: AsyncSession,
    allowed_document_ids: set[str],
) -> ResolveVersionsOutput:
    """Resolve version selectors within the authorized document set.

    Returns canonical version references or safe non-disclosing results
    for missing, inaccessible, failed, or erased versions.
    """
    results: list[dict[str, Any]] = []

    for selector in input_data.selectors:
        doc_id = selector.document_id

        # Authorization check: document must be in allowed set.
        if doc_id not in allowed_document_ids:
            results.append(
                VersionRef(
                    document_id=doc_id,
                    version_id="",
                    version_number=0,
                    selector_used=selector.selector,
                    status="inaccessible",
                ).__dict__
            )
            continue

        doc_uuid = uuid.UUID(doc_id)

        # Check for deletion tombstone.
        tombstone_stmt = select(DeletionTombstone).where(
            DeletionTombstone.document_id == doc_uuid,
            DeletionTombstone.scope == "document",
        )
        tombstone_result = await session.execute(tombstone_stmt)
        if tombstone_result.scalar_one_or_none() is not None:
            results.append(
                VersionRef(
                    document_id=doc_id,
                    version_id="",
                    version_number=0,
                    selector_used=selector.selector,
                    status="erased",
                ).__dict__
            )
            continue

        if selector.selector == "latest_completed":
            version = await _resolve_latest_completed(session, doc_uuid)
        elif selector.selector.startswith("v"):
            version = await _resolve_explicit_version(session, doc_uuid, selector.selector)
        elif ".." in selector.selector:
            # Date range — resolve all completed versions in range.
            version = await _resolve_date_range(session, doc_uuid, selector.selector)
        else:
            version = await _resolve_explicit_version(session, doc_uuid, selector.selector)

        if version is not None:
            results.append(version.__dict__)
        else:
            results.append(
                VersionRef(
                    document_id=doc_id,
                    version_id="",
                    version_number=0,
                    selector_used=selector.selector,
                    status="missing",
                ).__dict__
            )

    return ResolveVersionsOutput(resolved=results)


async def _resolve_latest_completed(
    session: AsyncSession,
    doc_uuid: uuid.UUID,
) -> VersionRef | None:
    """Resolve to the latest completed, non-erased version."""
    stmt = (
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == doc_uuid,
            DocumentVersion.lifecycle == DocumentLifecycle.COMPLETED,
        )
        .order_by(DocumentVersion.version_number.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    version = result.scalar_one_or_none()
    if version is None:
        return None
    return VersionRef(
        document_id=str(doc_uuid),
        version_id=str(version.id),
        version_number=version.version_number,
        selector_used="latest_completed",
        status="resolved",
    )


async def _resolve_explicit_version(
    session: AsyncSession,
    doc_uuid: uuid.UUID,
    selector: str,
) -> VersionRef | None:
    """Resolve an explicit version number like 'v3' or '3'."""
    try:
        version_number = int(selector.lstrip("v"))
    except ValueError:
        return None

    stmt = select(DocumentVersion).where(
        DocumentVersion.document_id == doc_uuid,
        DocumentVersion.version_number == version_number,
    )
    result = await session.execute(stmt)
    version = result.scalar_one_or_none()
    if version is None:
        return None

    if version.lifecycle == DocumentLifecycle.COMPLETED:
        status: str = "resolved"
    elif version.lifecycle in {DocumentLifecycle.FAILED, DocumentLifecycle.QUARANTINED}:
        status = "failed"
    else:
        status = "missing"

    return VersionRef(
        document_id=str(doc_uuid),
        version_id=str(version.id),
        version_number=version.version_number,
        selector_used=selector,
        status=status,
    )


async def _resolve_date_range(
    session: AsyncSession,
    doc_uuid: uuid.UUID,
    selector: str,
) -> VersionRef | None:
    """Resolve versions within a date range (e.g. '2025-01-01..2025-06-30').

    Returns the latest completed version within the range.
    """
    from datetime import datetime

    parts = selector.split("..")
    if len(parts) != 2:
        return None

    try:
        start_date = datetime.fromisoformat(parts[0].strip())
        end_date = datetime.fromisoformat(parts[1].strip())
    except ValueError:
        return None

    stmt = (
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == doc_uuid,
            DocumentVersion.lifecycle == DocumentLifecycle.COMPLETED,
            DocumentVersion.created_at >= start_date,
            DocumentVersion.created_at <= end_date,
        )
        .order_by(DocumentVersion.version_number.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    version = result.scalar_one_or_none()
    if version is None:
        return None
    return VersionRef(
        document_id=str(doc_uuid),
        version_id=str(version.id),
        version_number=version.version_number,
        selector_used=selector,
        status="resolved",
    )
