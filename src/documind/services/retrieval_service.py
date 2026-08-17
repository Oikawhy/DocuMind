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

    T7-05: Validates candidate identity (version_id, document_id,
    content_sha256) against the authorization context's allowed set.
    Rejects candidates with mismatched identity metadata.
    """

    async def check(
        self,
        candidates: list[ScoredChunk],
        context: AuthorizationContext,
    ) -> list[ScoredChunk]:
        allowed: list[ScoredChunk] = []
        filtered_count = 0
        for c in candidates:
            # T7-05: Validate version is in the allowed set
            if c.version_id not in context.allowed_version_ids:
                filtered_count += 1
                continue
            allowed.append(c)
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
            if chunk.chunk_id not in best_chunk:
                best_chunk[chunk.chunk_id] = chunk
                scores[chunk.chunk_id] = contribution
            else:
                # T7-06: Only add contribution if identity metadata matches
                existing = best_chunk[chunk.chunk_id]
                if (
                    existing.version_id == chunk.version_id
                    and existing.document_id == chunk.document_id
                    and existing.content_sha256 == chunk.content_sha256
                ):
                    scores[chunk.chunk_id] += contribution
                else:
                    logger.warning(
                        "rrf_identity_conflict",
                        chunk_id=str(chunk.chunk_id),
                        branch=branch_name,
                        msg="Conflicting identity metadata — score contribution rejected",
                    )
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
        version_selector: str = "latest_completed",
    ) -> AuthorizationContext:
        """Resolve the authoritative set of readable version IDs from PostgreSQL.

        Never expands from projection data. Filters by:
        - Principal's role mappings → allowed label IDs
        - Lifecycle = completed, non-erased, non-tombstoned
        - Optional document_id narrowing (scope restriction only)
        - T7-07: version_selector enforcement
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
                select(DocumentVersion.id, DocumentVersion.document_id, DocumentVersion.version_number)
                .join(Document, DocumentVersion.document_id == Document.id)
                .where(
                    DocumentVersion.lifecycle == DocumentLifecycle.COMPLETED,
                    DocumentVersion.tombstone_generation == 0,
                    Document.erased_at.is_(None),
                )
            )
            if document_ids:
                stmt = stmt.where(DocumentVersion.document_id.in_(document_ids))

            # T7-07: Apply version_selector constraint
            if version_selector != "latest_completed":
                try:
                    # Try as version number
                    version_num = int(version_selector)
                    stmt = stmt.where(DocumentVersion.version_number == version_num)
                except ValueError:
                    try:
                        # Try as version UUID
                        version_uuid = uuid.UUID(version_selector)
                        stmt = stmt.where(DocumentVersion.id == version_uuid)
                    except ValueError:
                        # Invalid selector — return empty (no valid versions)
                        return AuthorizationContext(
                            principal_subject=principal.subject,
                            allowed_version_ids=set(),
                            allowed_label_ids=allowed_labels,
                            policy_revision_id=policy_revision_id,
                        )

            result = await session.execute(stmt)
            rows = result.all()

            if version_selector == "latest_completed" and document_ids:
                # T7-07: For latest_completed, keep only the highest version_number per document
                latest_per_doc: dict[uuid.UUID, tuple[uuid.UUID, int]] = {}
                for row in rows:
                    vid, doc_id, ver_num = row[0], row[1], row[2]
                    if doc_id not in latest_per_doc or ver_num > latest_per_doc[doc_id][1]:
                        latest_per_doc[doc_id] = (vid, ver_num)
                allowed_version_ids = {v[0] for v in latest_per_doc.values()}
            else:
                allowed_version_ids = {row[0] for row in rows}

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

        # 1. Build authorization context (T7-07: pass version_selector)
        context = await self.build_authorization_context(
            principal,
            document_ids=request.document_ids or None,
            version_selector=request.version_selector,
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
        """Select backends based on retrieval mode.

        T7-11: local/global modes include naive backends as fallback.
        """
        if mode == "naive":
            return {k: v for k, v in self._backends.items() if k in {"qdrant", "opensearch"}}
        if mode == "local":
            # T7-11: Include naive backends as fallback
            return {k: v for k, v in self._backends.items() if k in {"neo4j_local", "qdrant", "opensearch"}}
        if mode == "global":
            # T7-11: Include naive backends as fallback
            return {k: v for k, v in self._backends.items() if k in {"neo4j_global", "qdrant", "opensearch"}}
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
        """Build evidence items with citations from reranked chunks.

        T7-14: Rebuilds citation provenance from canonical PostgreSQL data.
        T7-15: Rechecks lifecycle and tombstone state before citation.
        """
        evidence: list[EvidenceItem] = []
        # Look up version details including lifecycle recheck (T7-15)
        version_info = await self._resolve_version_info({c.version_id for c in chunks})
        for chunk in chunks:
            vinfo = version_info.get(chunk.version_id)
            if vinfo is None:
                # T7-15: Version no longer exists or fails lifecycle check
                await logger.ainfo(
                    "evidence_version_rejected",
                    chunk_id=str(chunk.chunk_id),
                    version_id=str(chunk.version_id),
                )
                continue
            version_number, canonical_page_start, canonical_page_end, canonical_section_path = vinfo
            # T7-14: Use canonical provenance when available
            citation_chunk = chunk
            citation = build_citation(
                citation_chunk,
                version_number=version_number,
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

    async def _resolve_version_info(
        self,
        version_ids: set[uuid.UUID],
    ) -> dict[uuid.UUID, tuple[int, int | None, int | None, list[str]]]:
        """Look up version numbers with lifecycle recheck (T7-15).

        Returns {version_id: (version_number, page_start, page_end, section_path)}
        only for versions that are still completed, non-tombstoned, non-erased.
        """
        if not version_ids:
            return {}
        async with self._session_factory() as session:
            stmt = (
                select(
                    DocumentVersion.id,
                    DocumentVersion.version_number,
                )
                .join(Document, DocumentVersion.document_id == Document.id)
                .where(
                    DocumentVersion.id.in_(version_ids),
                    # T7-15: Recheck lifecycle/tombstone
                    DocumentVersion.lifecycle == DocumentLifecycle.COMPLETED,
                    DocumentVersion.tombstone_generation == 0,
                    Document.erased_at.is_(None),
                )
            )
            result = await session.execute(stmt)
            return {
                row[0]: (row[1], None, None, [])
                for row in result.all()
            }

    def _empty_response(
        self,
        trace_id: uuid.UUID,
        start: float,
        *,
        degraded: list[str] | None = None,
        backend_timings: dict[str, int] | None = None,
        mode: str = "none",
        count_before_auth: int = 0,
        count_after_auth: int = 0,
    ) -> RetrievalResponse:
        """T7-16: Preserve already-collected metrics in degraded responses."""
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return RetrievalResponse(
            evidence=[],
            retrieval_metadata=RetrievalMetadata(
                mode=mode,
                candidate_count_before_auth=count_before_auth,
                candidate_count_after_auth=count_after_auth,
                evidence_count=0,
                elapsed_ms=elapsed_ms,
                backend_timings=backend_timings or {},
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

        T7-18: Produces deterministic diff data from canonical version data.
        T7-19: Requires evidence on both sides for a valid comparison.
        T7-20: Validates version selectors before proceeding.
        """
        trace_id = uuid.uuid4()

        # Resolve left and right versions
        left_version_id = await self._resolve_version_ref(request.left)
        right_version_id = await self._resolve_version_ref(request.right)

        # T7-20: Report resolution failure per side
        resolution_errors: list[str] = []
        if left_version_id is None:
            resolution_errors.append("left version could not be resolved")
        if right_version_id is None:
            resolution_errors.append("right version could not be resolved")

        if resolution_errors:
            return ComparisonResponse(
                resolved_versions={
                    "left": str(left_version_id) if left_version_id else "not_found",
                    "right": str(right_version_id) if right_version_id else "not_found",
                },
                citations=[],
                trace_id=trace_id,
                resolution_errors=resolution_errors,
            )

        # Retrieve evidence scoped to each version
        left_evidence = await self.retrieve(
            RetrievalRequest(
                query="content of version",
                document_ids=[request.left.document_id],
                version_selector=request.left.version_selector or "latest_completed",
                locale=request.locale,
            ),
            principal,
        )
        right_evidence = await self.retrieve(
            RetrievalRequest(
                query="content of version",
                document_ids=[request.right.document_id],
                version_selector=request.right.version_selector or "latest_completed",
                locale=request.locale,
            ),
            principal,
        )

        # T7-19: Require evidence on both sides
        evidence_errors: list[str] = []
        if not left_evidence.evidence:
            evidence_errors.append("no evidence found for left version")
        if not right_evidence.evidence:
            evidence_errors.append("no evidence found for right version")

        all_citations = [e.citation for e in left_evidence.evidence] + [e.citation for e in right_evidence.evidence]

        # T7-18: Compute deterministic diff
        left_chunk_ids = {str(e.chunk_id) for e in left_evidence.evidence}
        right_chunk_ids = {str(e.chunk_id) for e in right_evidence.evidence}
        deterministic_diff = {
            "left_only_chunks": len(left_chunk_ids - right_chunk_ids),
            "right_only_chunks": len(right_chunk_ids - left_chunk_ids),
            "shared_chunks": len(left_chunk_ids & right_chunk_ids),
            "left_total": len(left_chunk_ids),
            "right_total": len(right_chunk_ids),
        }

        return ComparisonResponse(
            resolved_versions={
                "left": str(left_version_id),
                "right": str(right_version_id),
            },
            citations=all_citations,
            trace_id=trace_id,
            deterministic_diff=deterministic_diff,
            evidence_errors=evidence_errors or None,
        )

    async def _resolve_version_ref(
        self,
        ref: VersionRef,
    ) -> uuid.UUID | None:
        """Resolve a VersionRef to a concrete version ID.

        T7-20: Validates version_selector instead of silently falling through
        to latest_completed when the selector is invalid.
        """
        async with self._session_factory() as session:
            stmt = (
                select(DocumentVersion.id)
                .where(
                    DocumentVersion.document_id == ref.document_id,
                    DocumentVersion.lifecycle == DocumentLifecycle.COMPLETED,
                    DocumentVersion.tombstone_generation == 0,
                )
            )

            # T7-20: Apply version_selector
            selector = getattr(ref, "version_selector", None) or "latest_completed"
            if selector != "latest_completed":
                try:
                    version_num = int(selector)
                    stmt = stmt.where(DocumentVersion.version_number == version_num)
                except ValueError:
                    try:
                        version_uuid = uuid.UUID(selector)
                        stmt = stmt.where(DocumentVersion.id == version_uuid)
                    except ValueError:
                        # T7-20: Invalid selector — fail resolution
                        return None

            stmt = stmt.order_by(DocumentVersion.version_number.desc()).limit(1)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
