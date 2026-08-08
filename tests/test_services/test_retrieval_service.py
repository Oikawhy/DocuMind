"""Retrieval schemas and service tests."""

from __future__ import annotations

import uuid

import pytest

from documind.schemas.retrieval import (
    Citation,
    ComparisonRequest,
    EvidenceItem,
    RetrievalMetadata,
    RetrievalRequest,
    RetrievalResponse,
    ScoredChunk,
    VersionRef,
)


class TestRetrievalRequest:
    def test_valid_request_minimal(self) -> None:
        req = RetrievalRequest(query="What changed?")
        assert req.query == "What changed?"
        assert req.locale == "en"
        assert req.document_ids == []
        assert req.mode is None

    def test_valid_request_with_mode(self) -> None:
        req = RetrievalRequest(query="Search", mode="hybrid")
        assert req.mode == "hybrid"

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode must be one of"):
            RetrievalRequest(query="q", mode="invalid")

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ValueError):
            RetrievalRequest(query="")

    def test_document_ids_max_20(self) -> None:
        ids = [uuid.uuid4() for _ in range(21)]
        with pytest.raises(ValueError):
            RetrievalRequest(query="q", document_ids=ids)

    def test_all_valid_modes_accepted(self) -> None:
        for mode in ("naive", "local", "global", "hybrid"):
            req = RetrievalRequest(query="q", mode=mode)
            assert req.mode == mode

    def test_none_mode_accepted(self) -> None:
        req = RetrievalRequest(query="q", mode=None)
        assert req.mode is None


class TestCitation:
    def test_excerpt_capped_at_1000_chars(self) -> None:
        cit = Citation(
            citation_id="cit_01",
            document_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            version_number=1,
            chunk_id=uuid.uuid4(),
            excerpt="x" * 1000,
            content_sha256="abc123",
        )
        assert len(cit.excerpt) == 1000

    def test_excerpt_exceeding_1000_rejected(self) -> None:
        with pytest.raises(ValueError):
            Citation(
                citation_id="cit_01",
                document_id=uuid.uuid4(),
                version_id=uuid.uuid4(),
                version_number=1,
                chunk_id=uuid.uuid4(),
                excerpt="x" * 1001,
                content_sha256="abc123",
            )

    def test_citation_with_graph_path(self) -> None:
        from documind.schemas.retrieval import GraphPath

        cit = Citation(
            citation_id="cit_02",
            document_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            version_number=2,
            chunk_id=uuid.uuid4(),
            excerpt="some text",
            content_sha256="def456",
            graph_path=GraphPath(generation=42, fact_ids=["f1"], hop_count=1),
        )
        assert cit.graph_path is not None
        assert cit.graph_path.generation == 42
        assert cit.graph_path.hop_count == 1


class TestScoredChunk:
    def test_scored_chunk_creation(self) -> None:
        chunk = ScoredChunk(
            chunk_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            content="text",
            content_sha256="abc",
            score=0.85,
            source_branch="naive",
        )
        assert chunk.score == 0.85
        assert chunk.source_branch == "naive"


class TestComparisonRequest:
    def test_comparison_request_creation(self) -> None:
        req = ComparisonRequest(
            left=VersionRef(document_id=uuid.uuid4()),
            right=VersionRef(document_id=uuid.uuid4()),
        )
        assert req.left.version_selector == "latest_completed"
        assert req.right.version_selector == "latest_completed"
        assert req.locale == "en"


class TestRetrievalResponse:
    def test_response_with_evidence(self) -> None:
        chunk_id = uuid.uuid4()
        resp = RetrievalResponse(
            evidence=[
                EvidenceItem(
                    chunk_id=chunk_id,
                    content="text",
                    fused_score=0.85,
                    source_branch="naive",
                    citation=Citation(
                        citation_id="cit_01",
                        document_id=uuid.uuid4(),
                        version_id=uuid.uuid4(),
                        version_number=1,
                        chunk_id=chunk_id,
                        content_sha256="abc",
                    ),
                ),
            ],
            retrieval_metadata=RetrievalMetadata(
                mode="naive",
                candidate_count_before_auth=50,
                candidate_count_after_auth=30,
                evidence_count=1,
                elapsed_ms=450,
            ),
            degraded_branches=[],
            trace_id=uuid.uuid4(),
        )
        assert len(resp.evidence) == 1
        assert resp.retrieval_metadata.mode == "naive"

    def test_empty_response(self) -> None:
        resp = RetrievalResponse(
            evidence=[],
            retrieval_metadata=RetrievalMetadata(
                mode="none",
                candidate_count_before_auth=0,
                candidate_count_after_auth=0,
                evidence_count=0,
                elapsed_ms=10,
            ),
            degraded_branches=["qdrant"],
            trace_id=uuid.uuid4(),
        )
        assert resp.evidence == []
        assert resp.degraded_branches == ["qdrant"]


# ---------------------------------------------------------------------------
# RRF Fusion tests
# ---------------------------------------------------------------------------

from documind.services.retrieval_service import (  # noqa: E402
    AuthorizationContext,
    PermissionGuard,
    build_citation,
    rrf_fuse,
)


def _scored_chunk(
    *,
    chunk_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    score: float = 0.5,
    branch: str = "naive",
    content: str = "Test content",
) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=chunk_id or uuid.uuid4(),
        version_id=version_id or uuid.uuid4(),
        document_id=uuid.uuid4(),
        content=content,
        content_sha256="abc",
        score=score,
        source_branch=branch,
    )


_UNSET_VERSIONS = object()


def _auth_context(
    *,
    allowed_versions: set[uuid.UUID] | object = _UNSET_VERSIONS,
    allowed_labels: set[uuid.UUID] | None = None,
) -> AuthorizationContext:
    return AuthorizationContext(
        principal_subject="user@test",
        allowed_version_ids=(
            {uuid.uuid4()} if allowed_versions is _UNSET_VERSIONS else allowed_versions  # type: ignore[arg-type]
        ),
        allowed_label_ids=allowed_labels or set(),
        policy_revision_id=uuid.uuid4(),
    )


class TestRRFFusion:
    def test_single_branch_preserves_order(self) -> None:
        c1, c2 = _scored_chunk(score=0.9), _scored_chunk(score=0.5)
        result = rrf_fuse({"naive": [c1, c2]}, rrf_constant=60)
        assert len(result) == 2
        assert result[0].score > result[1].score

    def test_multi_branch_deduplicates_by_chunk_id(self) -> None:
        cid = uuid.uuid4()
        c1 = _scored_chunk(chunk_id=cid, score=0.9)
        c1_copy = c1.model_copy()
        result = rrf_fuse(
            {"qdrant": [c1], "opensearch": [c1_copy]},
            rrf_constant=60,
        )
        assert len(result) == 1
        # Fused score = 1/(60+0) + 1/(60+0) = 2/60
        expected = 2.0 / 60.0
        assert abs(result[0].score - expected) < 0.001

    def test_caps_at_max_candidates(self) -> None:
        chunks = [_scored_chunk(score=0.5 + i * 0.001) for i in range(150)]
        result = rrf_fuse({"naive": chunks}, max_candidates=100)
        assert len(result) == 100

    def test_deterministic_tiebreak_by_chunk_uuid(self) -> None:
        cid_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
        cid_b = uuid.UUID("00000000-0000-0000-0000-000000000002")
        c1 = _scored_chunk(chunk_id=cid_a, score=0.5)
        c2 = _scored_chunk(chunk_id=cid_b, score=0.5)
        # Both at rank 0 in different branches → same RRF contribution
        result = rrf_fuse({"a": [c1], "b": [c2]}, rrf_constant=60)
        assert len(result) == 2
        # Same score → sorted by UUID ascending
        assert str(result[0].chunk_id) <= str(result[1].chunk_id)

    def test_empty_branches_returns_empty(self) -> None:
        result = rrf_fuse({})
        assert result == []

    def test_single_chunk_contribution(self) -> None:
        c = _scored_chunk(score=0.9)
        result = rrf_fuse({"naive": [c]}, rrf_constant=60)
        expected_score = 1.0 / (60 + 0)
        assert abs(result[0].score - expected_score) < 0.0001

    def test_rrf_multi_branch_sums_contributions(self) -> None:
        cid = uuid.uuid4()
        c = _scored_chunk(chunk_id=cid, score=0.9)
        result = rrf_fuse(
            {"qdrant": [c], "opensearch": [c.model_copy()]},
            rrf_constant=60,
        )
        # 1/(60+0) + 1/(60+0)
        expected_score = 2.0 / (60 + 0)
        assert abs(result[0].score - expected_score) < 0.0001


class TestPermissionGuard:
    @pytest.mark.asyncio
    async def test_filters_unauthorized_chunks(self) -> None:
        vid_allowed = uuid.uuid4()
        vid_denied = uuid.uuid4()
        ctx = _auth_context(allowed_versions={vid_allowed})
        guard = PermissionGuard()
        chunks = [
            _scored_chunk(version_id=vid_allowed, score=0.9),
            _scored_chunk(version_id=vid_denied, score=0.8),
        ]
        result = await guard.check(chunks, ctx)
        assert len(result) == 1
        assert result[0].version_id == vid_allowed

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_empty(self) -> None:
        ctx = _auth_context()
        guard = PermissionGuard()
        result = await guard.check([], ctx)
        assert result == []

    @pytest.mark.asyncio
    async def test_all_authorized_passes_all(self) -> None:
        vid = uuid.uuid4()
        ctx = _auth_context(allowed_versions={vid})
        guard = PermissionGuard()
        chunks = [
            _scored_chunk(version_id=vid, score=0.9),
            _scored_chunk(version_id=vid, score=0.8),
        ]
        result = await guard.check(chunks, ctx)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_none_authorized_returns_empty(self) -> None:
        vid_denied = uuid.uuid4()
        ctx = _auth_context(allowed_versions=set())
        guard = PermissionGuard()
        chunks = [_scored_chunk(version_id=vid_denied)]
        result = await guard.check(chunks, ctx)
        assert result == []


class TestBuildCitation:
    def test_excerpt_capped_at_1000(self) -> None:
        chunk = _scored_chunk(score=0.9, content="x" * 2000)
        cit = build_citation(chunk, version_number=3)
        assert len(cit.excerpt) == 1000

    def test_citation_has_stable_id_prefix(self) -> None:
        chunk = _scored_chunk(score=0.9)
        cit = build_citation(chunk, version_number=1)
        assert cit.citation_id.startswith("cit_")

    def test_citation_preserves_provenance(self) -> None:
        chunk = _scored_chunk(score=0.9)
        cit = build_citation(chunk, version_number=5)
        assert cit.document_id == chunk.document_id
        assert cit.version_id == chunk.version_id
        assert cit.chunk_id == chunk.chunk_id
        assert cit.version_number == 5
        assert cit.content_sha256 == chunk.content_sha256

    def test_citation_with_graph_path(self) -> None:
        from documind.schemas.retrieval import GraphPath

        chunk = _scored_chunk(score=0.9)
        gp = GraphPath(generation=42, fact_ids=["f1", "f2"], hop_count=1)
        cit = build_citation(chunk, version_number=1, graph_path=gp)
        assert cit.graph_path is not None
        assert cit.graph_path.generation == 42

    def test_citation_with_claim_ids(self) -> None:
        chunk = _scored_chunk(score=0.9)
        cit = build_citation(chunk, version_number=1, claim_ids=["c1", "c2"])
        assert cit.claim_ids == ["c1", "c2"]


class TestAuthorizationContext:
    def test_version_allowed(self) -> None:
        vid = uuid.uuid4()
        ctx = _auth_context(allowed_versions={vid})
        assert vid in ctx.allowed_version_ids

    def test_empty_versions_blocks_all(self) -> None:
        ctx = _auth_context(allowed_versions=set())
        assert len(ctx.allowed_version_ids) == 0

    def test_frozen_dataclass(self) -> None:
        ctx = _auth_context()
        with pytest.raises(AttributeError):
            ctx.principal_subject = "other"  # type: ignore[misc]
