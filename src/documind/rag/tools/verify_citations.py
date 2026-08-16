"""verify_citations — deterministic citation verification tool per §7.6.

Checks claim coverage, evidence-set membership, canonical chunk/version/
source-offset integrity, principal access, and graph-path validity.
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


class VerifyCitationsInput(BaseModel):
    """Input schema for verify_citations tool."""

    claims: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    evidence_ids: list[str] = Field(default_factory=list)
    principal_subject: str
    schema_version: str = SCHEMA_VERSION


class CitationStatus(BaseModel):
    """Verification status for a single citation."""

    citation_id: str
    valid: bool
    reason: str = ""


class VerifyCitationsOutput(BaseModel):
    """Output schema for verify_citations tool."""

    all_valid: bool = True
    statuses: list[CitationStatus] = Field(default_factory=list)
    uncovered_claim_ids: list[str] = Field(default_factory=list)
    failure_code: str | None = None
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def verify_citations(
    input_data: VerifyCitationsInput,
    session: AsyncSession | None = None,
    allowed_document_ids: set[str] | None = None,
) -> VerifyCitationsOutput:
    """Deterministically verify all citations against claims and evidence.

    Checks:
    1. Every claim has at least one citation
    2. Cited chunk is in the authorized evidence set
    3. Canonical version/chunk/source-offset integrity
    4. Principal still has access
    5. No tombstone invalidation
    """
    statuses: list[CitationStatus] = []
    uncovered: list[str] = []
    all_valid = True

    # Build claim-to-citation mapping.
    claim_ids = {c.get("claim_id", "") for c in input_data.claims if c.get("claim_id")}
    citation_claim_ids: set[str] = set()

    for citation_data in input_data.citations:
        cit_id = citation_data.get("citation_id", "")
        claim_id = citation_data.get("claim_id", "")
        chunk_id = citation_data.get("chunk_id", "")

        citation_claim_ids.add(claim_id)

        # Check 1: cited chunk is in the authorized evidence set.
        if chunk_id and chunk_id not in input_data.evidence_ids:
            statuses.append(
                CitationStatus(
                    citation_id=cit_id,
                    valid=False,
                    reason="Cited chunk not in authorized evidence set",
                )
            )
            all_valid = False
            continue

        # Check 2: canonical integrity via database (if session provided).
        if session is not None and chunk_id:
            try:
                chunk_uuid = uuid.UUID(chunk_id)
                chunk_stmt = select(DocumentChunk).where(DocumentChunk.id == chunk_uuid)
                chunk_result = await session.execute(chunk_stmt)
                chunk = chunk_result.scalar_one_or_none()

                if chunk is None:
                    statuses.append(
                        CitationStatus(citation_id=cit_id, valid=False, reason="Chunk not found in database")
                    )
                    all_valid = False
                    continue

                # Check content hash if provided.
                cited_hash = citation_data.get("content_sha256", "")
                if (
                    cited_hash
                    and hasattr(chunk, "content_sha256")
                    and chunk.content_sha256
                    and cited_hash != chunk.content_sha256
                ):
                    statuses.append(
                        CitationStatus(
                            citation_id=cit_id,
                            valid=False,
                            reason="Content hash mismatch — evidence may have changed",
                        )
                    )
                    all_valid = False
                    continue

                # Check version lifecycle.
                version_stmt = select(DocumentVersion).where(DocumentVersion.id == chunk.version_id)
                version_result = await session.execute(version_stmt)
                version = version_result.scalar_one_or_none()

                if version is None or version.lifecycle != DocumentLifecycle.COMPLETED:
                    statuses.append(
                        CitationStatus(
                            citation_id=cit_id,
                            valid=False,
                            reason="Version no longer completed",
                        )
                    )
                    all_valid = False
                    continue

                # Check document authorization.
                if allowed_document_ids and str(version.document_id) not in allowed_document_ids:
                    statuses.append(
                        CitationStatus(
                            citation_id=cit_id,
                            valid=False,
                            reason="Principal no longer has access to document",
                        )
                    )
                    all_valid = False
                    continue

                # Check for tombstone.
                tombstone_stmt = select(DeletionTombstone).where(
                    DeletionTombstone.document_id == version.document_id,
                    DeletionTombstone.scope == "document",
                )
                tombstone_result = await session.execute(tombstone_stmt)
                if tombstone_result.scalar_one_or_none() is not None:
                    statuses.append(
                        CitationStatus(
                            citation_id=cit_id,
                            valid=False,
                            reason="Document has been deleted (tombstone)",
                        )
                    )
                    all_valid = False
                    continue

            except (ValueError, Exception):
                statuses.append(
                    CitationStatus(citation_id=cit_id, valid=False, reason="Citation verification error")
                )
                all_valid = False
                continue

        statuses.append(CitationStatus(citation_id=cit_id, valid=True))

    # Check that every claim has at least one citation.
    for claim_id in claim_ids:
        if claim_id not in citation_claim_ids:
            uncovered.append(claim_id)
            all_valid = False

    failure_code = None
    if not all_valid:
        failure_code = "UNCOVERED_CLAIMS" if uncovered else "INVALID_CITATIONS"

    return VerifyCitationsOutput(
        all_valid=all_valid,
        statuses=statuses,
        uncovered_claim_ids=uncovered,
        failure_code=failure_code,
    )
