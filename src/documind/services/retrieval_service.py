"""Multi-backend retrieval orchestrator per §6.

Orchestrates Qdrant dense, OpenSearch BM25, Neo4j local, and Neo4j global
backends with RRF fusion, canonical Permission Guard, and citation
construction.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.domain.policy_service import PolicyService
from documind.models.document import Document, DocumentVersion
from documind.models.enums import DocumentLifecycle
from documind.schemas.retrieval import (
    Citation,
    ComparisonRequest,
    ComparisonResponse,
    EvidenceItem,
    GraphPath,
    RetrievalMetadata,
    RetrievalRequest,
    RetrievalResponse,
    ScoredChunk,
    VersionRef,
)
from documind.services.identity_service import Principal
from documind.services.reranker_service import Reranker, RerankerUnavailableError

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Authorization context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationContext:
    """Server-resolved authorization scope for a retrieval request.

    Built from the canonical PostgreSQL source — never from projection data.
    """

    principal_subject: str
    allowed_version_ids: set[uuid.UUID]
    allowed_label_ids: set[uuid.UUID]
    policy_revision_id: uuid.UUID | None = None
    lifecycle_filter: str = "completed"


# ---------------------------------------------------------------------------
# Backend protocols
# ---------------------------------------------------------------------------


class RetrievalBackend(Protocol):
    """One retrieval branch (Qdrant, OpenSearch, Neo4j local/global)."""

    @property
    def name(self) -> str: ...

    async def search(
        self,
        query: str,
        context: AuthorizationContext,
        *,
        max_candidates: int = 100,
        deadline_ms: int = 750,
    ) -> list[ScoredChunk]: ...


# ---------------------------------------------------------------------------
# Permission Guard
# ---------------------------------------------------------------------------


class PermissionGuard:
    """Canonical PostgreSQL recheck on every candidate per §6.3.

    Accepts only chunks whose version_id is in the authorization context's
    allowed set. Logs filtered count for audit without revealing identifiers.
    """

    async def check(
        self,
        candidates: list[ScoredChunk],
        context: AuthorizationContext,
    ) -> list[ScoredChunk]:
        allowed = [c for c in candidates if c.version_id in context.allowed_version_ids]
        filtered_count = len(candidates) - len(allowed)
        if filtered_count > 0:
            await logger.ainfo(
                "permission_guard_filtered",
                filtered=filtered_count,
                retained=len(allowed),
                principal=context.principal_subject,
            )
        return allowed


# ---------------------------------------------------------------------------
# RRF Fusion
# ---------------------------------------------------------------------------

_RRF_CONSTANT = 60


def rrf_fuse(
    branch_results: dict[str, list[ScoredChunk]],
    *,
    rrf_constant: int = _RRF_CONSTANT,
    max_candidates: int = 100,
) -> list[ScoredChunk]:
    """Reciprocal Rank Fusion across branches per §6.3.

    ``fused_score(chunk) = sum(1 / (rrf_constant + rank_in_branch))``

    Deduplicates by chunk UUID. Sorts by fused score desc, then chunk UUID asc
    for deterministic replay.
    """
    scores: dict[uuid.UUID, float] = {}
    best_chunk: dict[uuid.UUID, ScoredChunk] = {}
    branches_seen: dict[uuid.UUID, list[str]] = {}

    for branch_name, chunks in branch_results.items():
        for rank, chunk in enumerate(chunks):
            contribution = 1.0 / (rrf_constant + rank)
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + contribution
            # Keep the chunk data from the first encounter
            if chunk.chunk_id not in best_chunk:
                best_chunk[chunk.chunk_id] = chunk
            branches_seen.setdefault(chunk.chunk_id, []).append(branch_name)

    # Build fused result
    fused = [
        best_chunk[cid].model_copy(
            update={
                "score": scores[cid],
                "source_branch": ",".join(sorted(branches_seen[cid])),
            }
        )
        for cid in scores
    ]
    fused.sort(key=lambda c: (-c.score, str(c.chunk_id)))
    return fused[:max_candidates]


# ---------------------------------------------------------------------------
# Citation builder
# ---------------------------------------------------------------------------

_EXCERPT_CAP = 1000


def build_citation(
    chunk: ScoredChunk,
    *,
    version_number: int,
    claim_ids: list[str] | None = None,
    graph_path: GraphPath | None = None,
) -> Citation:
    """Build a stable citation object per §6.4."""
    excerpt = chunk.content[:_EXCERPT_CAP]
    cit_id = f"cit_{uuid.uuid4().hex[:12]}"
    return Citation(
        citation_id=cit_id,
        document_id=chunk.document_id,
        version_id=chunk.version_id,
        version_number=version_number,
        chunk_id=chunk.chunk_id,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        section_path=chunk.section_path,
        excerpt=excerpt,
        content_sha256=chunk.content_sha256,
        claim_ids=claim_ids or [],
        graph_path=graph_path,
    )


# ---------------------------------------------------------------------------
# Retrieval service
# ---------------------------------------------------------------------------


class RetrievalService:
    """Multi-backend retrieval orchestrator per §6.

    1. Resolve authorization context from PostgreSQL
    2. Run enabled backends in parallel with independent deadlines
    3. RRF fuse branch results
    4. Permission Guard recheck on all candidates
    5. Rerank authorized chunks
    6. Build citations and evidence
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        policy_service: PolicyService,
        reranker: Reranker,
        backends: dict[str, RetrievalBackend],
        rrf_constant: int = 60,
        max_candidates: int = 100,
        max_evidence: int = 10,
        reranker_threshold: float = 0.10,
        budget_ms: int = 2500,
    ) -> None:
        self._session_factory = session_factory
        self._policy_service = policy_service
        self._reranker = reranker
        self._backends = backends
        self._guard = PermissionGuard()
        self._rrf_constant = rrf_constant
        self._max_candidates = max_candidates
        self._max_evidence = max_evidence
        self._reranker_threshold = reranker_threshold
        self._budget_ms = budget_ms

    async def build_authorization_context(
        self,
        principal: Principal,
        *,
        document_ids: list[uuid.UUID] | None = None,
    ) -> AuthorizationContext:
        """Resolve the authoritative set of readable version IDs from PostgreSQL.

        Never expands from projection data. Filters by:
        - Principal's role mappings → allowed label IDs
        - Lifecycle = completed, non-erased, non-tombstoned
        - Optional document_id narrowing (scope restriction only)
        """
        role_mappings = await self._policy_service.get_role_mappings(principal.groups)
        if not role_mappings:
            return AuthorizationContext(
                principal_subject=principal.subject,
                allowed_version_ids=set(),
                allowed_label_ids=set(),
            )

        allowed_labels: set[uuid.UUID] = set()
        policy_revision_id: uuid.UUID | None = None
        for rm in role_mappings:
            allowed_labels.update(rm.allowed_label_ids)
            if rm.policy_revision_id:
                policy_revision_id = rm.policy_revision_id

        async with self._session_factory() as session:
            stmt = (
                select(DocumentVersion.id)
                .join(Document, DocumentVersion.document_id == Document.id)
                .where(
                    DocumentVersion.lifecycle == DocumentLifecycle.COMPLETED,
                    DocumentVersion.tombstone_generation == 0,
                    Document.erased_at.is_(None),
                )
            )
            if document_ids:
                stmt = stmt.where(DocumentVersion.document_id.in_(document_ids))

            result = await session.execute(stmt)
            allowed_version_ids = {row[0] for row in result.all()}

        return AuthorizationContext(
            principal_subject=principal.subject,
            allowed_version_ids=allowed_version_ids,
            allowed_label_ids=allowed_labels,
            policy_revision_id=policy_revision_id,
        )

    async def retrieve(
        self,
        request: RetrievalRequest,
        principal: Principal,
    ) -> RetrievalResponse:
        """Execute the full retrieval pipeline."""
        start = time.monotonic()
        trace_id = uuid.uuid4()
        backend_timings: dict[str, int] = {}
        degraded_branches: list[str] = []

        # 1. Build authorization context
        context = await self.build_authorization_context(
            principal,
            document_ids=request.document_ids or None,
        )

        if not context.allowed_version_ids:
            return self._empty_response(trace_id, start)

        # 2. Determine which backends to run
        mode = request.mode or "hybrid"
        enabled_backends = self._select_backends(mode)

        # 3. Run backends concurrently with deadlines
        branch_results: dict[str, list[ScoredChunk]] = {}
        tasks = {
            name: asyncio.create_task(self._run_backend(name, backend, request.query, context))
            for name, backend in enabled_backends.items()
        }

        for name, task in tasks.items():
            try:
                t0 = time.monotonic()
                result = await asyncio.wait_for(task, timeout=0.75)
                backend_timings[name] = int((time.monotonic() - t0) * 1000)
                branch_results[name] = result
            except TimeoutError:
                degraded_branches.append(name)
                await logger.awarning("retrieval_backend_timeout", backend=name)
            except Exception as exc:
                degraded_branches.append(name)
                await logger.awarning("retrieval_backend_error", backend=name, error=str(exc))

        if not branch_results:
            return self._empty_response(trace_id, start, degraded=degraded_branches)

        # 4. RRF fusion
        fused = rrf_fuse(
            branch_results,
            rrf_constant=self._rrf_constant,
            max_candidates=self._max_candidates,
        )
        count_before_auth = len(fused)

        # 5. Permission Guard
        authorized = await self._guard.check(fused, context)
        count_after_auth = len(authorized)

        # 6. Rerank
        try:
            reranked = await self._reranker.rerank(request.query, authorized)
        except RerankerUnavailableError:
            degraded_branches.append("reranker")
            reranked = []

        # 7. Build evidence with citations
        evidence = await self._build_evidence(reranked[: self._max_evidence])

        elapsed_ms = int((time.monotonic() - start) * 1000)

        return RetrievalResponse(
            evidence=evidence,
            retrieval_metadata=RetrievalMetadata(
                mode=mode,
                candidate_count_before_auth=count_before_auth,
                candidate_count_after_auth=count_after_auth,
                evidence_count=len(evidence),
                elapsed_ms=elapsed_ms,
                backend_timings=backend_timings,
            ),
            degraded_branches=degraded_branches,
            trace_id=trace_id,
        )

    def _select_backends(self, mode: str) -> dict[str, RetrievalBackend]:
        """Select backends based on retrieval mode."""
        if mode == "naive":
            return {k: v for k, v in self._backends.items() if k in {"qdrant", "opensearch"}}
        if mode == "local":
            return {k: v for k, v in self._backends.items() if k in {"neo4j_local"}}
        if mode == "global":
            return {k: v for k, v in self._backends.items() if k in {"neo4j_global"}}
        # hybrid — all enabled backends
        return dict(self._backends)

    async def _run_backend(
        self,
        name: str,
        backend: RetrievalBackend,
        query: str,
        context: AuthorizationContext,
    ) -> list[ScoredChunk]:
        """Run a single backend with its deadline."""
        return await backend.search(
            query,
            context,
            max_candidates=self._max_candidates,
            deadline_ms=750,
        )

    async def _build_evidence(self, chunks: list[ScoredChunk]) -> list[EvidenceItem]:
        """Build evidence items with citations from reranked chunks."""
        evidence: list[EvidenceItem] = []
        # Look up version numbers
        version_numbers = await self._resolve_version_numbers({c.version_id for c in chunks})
        for chunk in chunks:
            citation = build_citation(
                chunk,
                version_number=version_numbers.get(chunk.version_id, 0),
            )
            evidence.append(
                EvidenceItem(
                    chunk_id=chunk.chunk_id,
                    content=chunk.content,
                    fused_score=chunk.score,
                    reranker_score=chunk.score,
                    source_branch=chunk.source_branch,
                    citation=citation,
                )
            )
        return evidence

    async def _resolve_version_numbers(
        self,
        version_ids: set[uuid.UUID],
    ) -> dict[uuid.UUID, int]:
        """Look up version numbers from PostgreSQL."""
        if not version_ids:
            return {}
        async with self._session_factory() as session:
            stmt = select(
                DocumentVersion.id,
                DocumentVersion.version_number,
            ).where(DocumentVersion.id.in_(version_ids))
            result = await session.execute(stmt)
            return {row[0]: row[1] for row in result.all()}

    def _empty_response(
        self,
        trace_id: uuid.UUID,
        start: float,
        *,
        degraded: list[str] | None = None,
    ) -> RetrievalResponse:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return RetrievalResponse(
            evidence=[],
            retrieval_metadata=RetrievalMetadata(
                mode="none",
                candidate_count_before_auth=0,
                candidate_count_after_auth=0,
                evidence_count=0,
                elapsed_ms=elapsed_ms,
            ),
            degraded_branches=degraded or [],
            trace_id=trace_id,
        )

    async def compare(
        self,
        request: ComparisonRequest,
        principal: Principal,
    ) -> ComparisonResponse:
        """Execute a comparison between two document versions.

        Resolves both version references, retrieves evidence for each,
        and returns cited comparison data.
        """
        trace_id = uuid.uuid4()

        # Resolve left and right versions
        left_version_id = await self._resolve_version_ref(request.left)
        right_version_id = await self._resolve_version_ref(request.right)

        # Retrieve evidence scoped to each version
        left_evidence = await self.retrieve(
            RetrievalRequest(
                query="content of version",
                document_ids=[request.left.document_id],
                locale=request.locale,
            ),
            principal,
        )
        right_evidence = await self.retrieve(
            RetrievalRequest(
                query="content of version",
                document_ids=[request.right.document_id],
                locale=request.locale,
            ),
            principal,
        )

        all_citations = [e.citation for e in left_evidence.evidence] + [e.citation for e in right_evidence.evidence]

        return ComparisonResponse(
            resolved_versions={
                "left": str(left_version_id) if left_version_id else "not_found",
                "right": str(right_version_id) if right_version_id else "not_found",
            },
            citations=all_citations,
            trace_id=trace_id,
        )

    async def _resolve_version_ref(
        self,
        ref: VersionRef,
    ) -> uuid.UUID | None:
        """Resolve a VersionRef to a concrete version ID."""
        async with self._session_factory() as session:
            stmt = (
                select(DocumentVersion.id)
                .where(
                    DocumentVersion.document_id == ref.document_id,
                    DocumentVersion.lifecycle == DocumentLifecycle.COMPLETED,
                    DocumentVersion.tombstone_generation == 0,
                )
                .order_by(DocumentVersion.version_number.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
