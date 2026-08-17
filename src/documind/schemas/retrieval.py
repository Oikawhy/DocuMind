"""Pydantic contracts for retrieval, comparison, and evidence per §6."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator


class VersionRef(BaseModel):
    """Reference to a specific document version for comparison."""

    document_id: uuid.UUID
    version_selector: str = "latest_completed"


class RetrievalRequest(BaseModel):
    """Server-validated retrieval request per §6.1.

    The server builds all projection filters from the authenticated
    principal; callers may only narrow scope via document_ids.
    """

    query: str = Field(..., min_length=1, max_length=4096)
    locale: str = "en"
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    version_selector: str = "latest_completed"
    mode: str | None = None  # advisory only; server may override

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str | None) -> str | None:
        if v is not None and v not in {"naive", "local", "global", "hybrid"}:
            msg = "mode must be one of: naive, local, global, hybrid"
            raise ValueError(msg)
        return v


class ComparisonRequest(BaseModel):
    """Comparison request between two document versions."""

    left: VersionRef
    right: VersionRef
    fields: list[str] = Field(default_factory=list)
    locale: str = "en"


class GraphPath(BaseModel):
    """Optional graph provenance attached to a citation."""

    generation: int
    fact_ids: list[str] = Field(default_factory=list)
    hop_count: int


class Citation(BaseModel):
    """Stable citation object per §6.4."""

    citation_id: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int
    chunk_id: uuid.UUID
    page_start: int | None = None
    page_end: int | None = None
    section_path: list[str] = Field(default_factory=list)
    excerpt: str = Field(default="", max_length=1000)
    content_sha256: str
    claim_ids: list[str] = Field(default_factory=list)
    graph_path: GraphPath | None = None


class ScoredChunk(BaseModel):
    """An authorized, scored chunk candidate before reranking."""

    chunk_id: uuid.UUID
    version_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    content_sha256: str
    page_start: int | None = None
    page_end: int | None = None
    section_path: list[str] = Field(default_factory=list)
    score: float
    source_branch: str
    # T7-13: Graph provenance
    fact_ids: list[str] = Field(default_factory=list)
    generation: int | None = None
    hop_count: int | None = None


class EvidenceItem(BaseModel):
    """One piece of evidence in a retrieval response."""

    chunk_id: uuid.UUID
    content: str
    fused_score: float
    reranker_score: float | None = None
    source_branch: str
    citation: Citation


class RetrievalMetadata(BaseModel):
    """Observability metadata for a retrieval request."""

    mode: str
    candidate_count_before_auth: int
    candidate_count_after_auth: int
    evidence_count: int
    elapsed_ms: int
    backend_timings: dict[str, int] = Field(default_factory=dict)
    # T7-16: Additional metrics preserved in degraded responses
    fusion_time_ms: int | None = None
    permission_filter_count: int | None = None
    reranker_above_threshold: int | None = None
    reranker_below_threshold: int | None = None


class RetrievalResponse(BaseModel):
    """Full retrieval response per §9.3."""

    evidence: list[EvidenceItem] = Field(default_factory=list)
    retrieval_metadata: RetrievalMetadata
    degraded_branches: list[str] = Field(default_factory=list)
    trace_id: uuid.UUID


class ComparisonResponse(BaseModel):
    """Comparison result with resolved versions and citations."""

    resolved_versions: dict[str, str]
    citations: list[Citation] = Field(default_factory=list)
    trace_id: uuid.UUID
    # T7-18: Deterministic diff data
    deterministic_diff: dict[str, int] | None = None
    # T7-19: Evidence requirement errors
    evidence_errors: list[str] | None = None
    # T7-20: Resolution errors
    resolution_errors: list[str] | None = None
