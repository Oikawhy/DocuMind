"""load_evidence — bounded evidence loader tool per §7.6.

Loads bounded source excerpts and provenance for allowed evidence IDs
into the ``EvidenceCache``.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.models.chunk import DocumentChunk
from documind.models.document import DocumentVersion
from documind.rag.state import MAX_EVIDENCE_CHUNKS, EvidenceCache

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Versioned input / output schemas
# ---------------------------------------------------------------------------


class LoadEvidenceInput(BaseModel):
    """Input schema for load_evidence tool."""

    allowed_evidence_ids: list[str]
    excerpt_budget_tokens: int = 500
    schema_version: str = SCHEMA_VERSION


class EvidenceProvenance(BaseModel):
    """Provenance metadata for a loaded evidence item."""

    evidence_id: str
    document_id: str
    version_id: str
    version_number: int
    page_start: int | None = None
    page_end: int | None = None
    section_path: list[str] = Field(default_factory=list)
    content_sha256: str = ""
    token_count: int = 0


class LoadEvidenceOutput(BaseModel):
    """Output schema for load_evidence tool."""

    loaded_ids: list[str] = Field(default_factory=list)
    provenance: list[EvidenceProvenance] = Field(default_factory=list)
    truncated_count: int = 0
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def load_evidence(
    input_data: LoadEvidenceInput,
    session: AsyncSession,
    evidence_cache: EvidenceCache,
) -> LoadEvidenceOutput:
    """Load bounded source excerpts into the evidence cache.

    Respects the ``MAX_EVIDENCE_CHUNKS`` cap and per-item excerpt budget.
    """
    loaded: list[str] = []
    provenance: list[EvidenceProvenance] = []
    truncated = 0

    # Cap at max evidence chunks.
    ids_to_load = input_data.allowed_evidence_ids[:MAX_EVIDENCE_CHUNKS]

    for eid in ids_to_load:
        try:
            chunk_uuid = uuid.UUID(eid)
        except ValueError:
            continue

        # Load chunk from database.
        chunk_stmt = select(DocumentChunk).where(DocumentChunk.id == chunk_uuid)
        chunk_result = await session.execute(chunk_stmt)
        chunk = chunk_result.scalar_one_or_none()
        if chunk is None:
            continue

        # Load version for provenance.
        version_stmt = select(DocumentVersion).where(DocumentVersion.id == chunk.version_id)
        version_result = await session.execute(version_stmt)
        version = version_result.scalar_one_or_none()

        # Truncate content to excerpt budget.
        content = chunk.content or ""
        tokens = content.split()
        if len(tokens) > input_data.excerpt_budget_tokens:
            content = " ".join(tokens[: input_data.excerpt_budget_tokens])
            truncated += 1
            token_count = input_data.excerpt_budget_tokens
        else:
            token_count = len(tokens)

        # Store in evidence cache.
        evidence_cache.put(eid, content)
        loaded.append(eid)

        prov = EvidenceProvenance(
            evidence_id=eid,
            document_id=str(version.document_id) if version else "",
            version_id=str(chunk.version_id),
            version_number=version.version_number if version else 0,
            page_start=chunk.page_start if hasattr(chunk, "page_start") else None,
            page_end=chunk.page_end if hasattr(chunk, "page_end") else None,
            section_path=chunk.section_path if hasattr(chunk, "section_path") else [],
            content_sha256=chunk.content_sha256 if hasattr(chunk, "content_sha256") else "",
            token_count=token_count,
        )
        provenance.append(prov)

    return LoadEvidenceOutput(
        loaded_ids=loaded,
        provenance=provenance,
        truncated_count=truncated,
    )
