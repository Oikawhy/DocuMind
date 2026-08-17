"""HTTP contracts for Task 7 retrieval and comparison endpoints."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from documind.main import app
from documind.schemas.retrieval import (
    Citation,
    ComparisonResponse,
    EvidenceItem,
    RetrievalMetadata,
    RetrievalResponse,
)
from documind.services.identity_service import Principal


def _principal() -> Principal:
    return Principal(
        subject="reader@example.test",
        display_name="Reader",
        email=None,
        groups=["viewers"],
        active=True,
        issuer="https://issuer.example.test",
    )


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_post_retrieval_returns_evidence_and_metadata(client: AsyncClient) -> None:
    chunk_id = uuid.uuid4()
    trace_id = uuid.uuid4()
    service = AsyncMock()
    service.retrieve.return_value = RetrievalResponse(
        evidence=[
            EvidenceItem(
                chunk_id=chunk_id,
                content="Evidence text",
                fused_score=0.85,
                reranker_score=0.92,
                source_branch="naive",
                citation=Citation(
                    citation_id="cit_abc",
                    document_id=uuid.uuid4(),
                    version_id=uuid.uuid4(),
                    version_number=1,
                    chunk_id=chunk_id,
                    excerpt="Evidence text",
                    content_sha256="abc123",
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
        trace_id=trace_id,
    )
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    original_service = app.state.retrieval_service
    original_identity = app.state.identity_service
    try:
        app.state.retrieval_service = service
        app.state.identity_service = identity
        response = await client.post(
            "/v1/retrieval",
            headers={"Authorization": "Bearer test"},
            json={"query": "What changed in the renewal clause?"},
        )
    finally:
        app.state.retrieval_service = original_service
        app.state.identity_service = original_identity

    assert response.status_code == 200
    data = response.json()
    assert len(data["evidence"]) == 1
    assert data["evidence"][0]["citation"]["citation_id"] == "cit_abc"
    assert data["retrieval_metadata"]["mode"] == "naive"
    assert data["trace_id"] == str(trace_id)


async def test_post_retrieval_service_unavailable_returns_503(client: AsyncClient) -> None:
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    original_service = app.state.retrieval_service
    original_identity = app.state.identity_service
    try:
        app.state.retrieval_service = None
        app.state.identity_service = identity
        response = await client.post(
            "/v1/retrieval",
            headers={"Authorization": "Bearer test"},
            json={"query": "test"},
        )
    finally:
        app.state.retrieval_service = original_service
        app.state.identity_service = original_identity

    assert response.status_code == 503


async def test_post_retrieval_invalid_mode_returns_422(client: AsyncClient) -> None:
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    service = AsyncMock()
    original_service = app.state.retrieval_service
    original_identity = app.state.identity_service
    try:
        app.state.retrieval_service = service
        app.state.identity_service = identity
        response = await client.post(
            "/v1/retrieval",
            headers={"Authorization": "Bearer test"},
            json={"query": "test", "mode": "invalid_mode"},
        )
    finally:
        app.state.retrieval_service = original_service
        app.state.identity_service = original_identity

    assert response.status_code == 422


async def test_post_comparisons_returns_resolved_versions(client: AsyncClient) -> None:
    trace_id = uuid.uuid4()
    service = AsyncMock()
    service.compare.return_value = ComparisonResponse(
        resolved_versions={"left": str(uuid.uuid4()), "right": str(uuid.uuid4())},
        citations=[],
        trace_id=trace_id,
    )
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    original_service = app.state.retrieval_service
    original_identity = app.state.identity_service
    try:
        app.state.retrieval_service = service
        app.state.identity_service = identity
        response = await client.post(
            "/v1/comparisons",
            headers={"Authorization": "Bearer test"},
            json={
                "left": {"document_id": str(uuid.uuid4())},
                "right": {"document_id": str(uuid.uuid4())},
            },
        )
    finally:
        app.state.retrieval_service = original_service
        app.state.identity_service = original_identity

    assert response.status_code == 200
    data = response.json()
    assert "left" in data["resolved_versions"]
    assert "right" in data["resolved_versions"]


async def test_post_reindex_returns_accepted(client: AsyncClient) -> None:
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    doc_service = AsyncMock()
    temporal_client = AsyncMock()
    original_doc_service = app.state.document_service
    original_identity = app.state.identity_service
    original_temporal = getattr(app.state, "temporal_client", None)
    version_id = uuid.uuid4()
    try:
        app.state.document_service = doc_service
        app.state.identity_service = identity
        app.state.temporal_client = temporal_client
        response = await client.post(
            f"/v1/document-versions/{version_id}/reindex",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.state.document_service = original_doc_service
        app.state.identity_service = original_identity
        if original_temporal is not None:
            app.state.temporal_client = original_temporal

    assert response.status_code == 202
    data = response.json()
    assert data["version_id"] == str(version_id)
    assert data["status"] == "accepted"
    # T7-21: Should have dispatched for all three backends
    assert "backends_started" in data



async def test_post_retrieval_empty_query_returns_422(client: AsyncClient) -> None:
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    service = AsyncMock()
    original_service = app.state.retrieval_service
    original_identity = app.state.identity_service
    try:
        app.state.retrieval_service = service
        app.state.identity_service = identity
        response = await client.post(
            "/v1/retrieval",
            headers={"Authorization": "Bearer test"},
            json={"query": ""},
        )
    finally:
        app.state.retrieval_service = original_service
        app.state.identity_service = original_identity

    assert response.status_code == 422
