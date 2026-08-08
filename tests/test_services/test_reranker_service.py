"""Reranker service unit tests using protocol-level mocks."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from documind.schemas.retrieval import ScoredChunk
from documind.services.reranker_service import (
    RerankerService,
    RerankerUnavailableError,
)


def _chunk(score: float = 0.5, branch: str = "naive") -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        content="Sample chunk text for reranking.",
        content_sha256="abc123",
        score=score,
        source_branch=branch,
    )


@dataclass
class FakeCrossEncoder:
    """Returns predictable scores based on configuration."""

    scores: list[float] | None = None
    should_fail: bool = False

    async def predict(self, query: str, passages: list[str]) -> list[float]:
        if self.should_fail:
            raise ConnectionError("Model unavailable")
        if self.scores is not None:
            return self.scores[: len(passages)]
        return [0.5 - i * 0.1 for i in range(len(passages))]


@pytest.mark.asyncio
async def test_rerank_sorts_by_score_descending() -> None:
    encoder = FakeCrossEncoder(scores=[0.3, 0.9, 0.6])
    service = RerankerService(encoder=encoder, threshold=0.10, max_results=10)
    chunks = [_chunk(), _chunk(), _chunk()]
    result = await service.rerank("test query", chunks)
    assert len(result) == 3
    assert result[0].score == 0.9
    assert result[1].score == 0.6
    assert result[2].score == 0.3


@pytest.mark.asyncio
async def test_rerank_filters_below_threshold() -> None:
    encoder = FakeCrossEncoder(scores=[0.05, 0.9, 0.08])
    service = RerankerService(encoder=encoder, threshold=0.10, max_results=10)
    chunks = [_chunk(), _chunk(), _chunk()]
    result = await service.rerank("test query", chunks)
    assert len(result) == 1
    assert result[0].score == 0.9


@pytest.mark.asyncio
async def test_rerank_caps_at_max_results() -> None:
    encoder = FakeCrossEncoder(scores=[0.9, 0.8, 0.7, 0.6, 0.5])
    service = RerankerService(encoder=encoder, threshold=0.10, max_results=3)
    chunks = [_chunk() for _ in range(5)]
    result = await service.rerank("test query", chunks)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_rerank_empty_input_returns_empty() -> None:
    encoder = FakeCrossEncoder()
    service = RerankerService(encoder=encoder, threshold=0.10, max_results=10)
    result = await service.rerank("query", [])
    assert result == []


@pytest.mark.asyncio
async def test_rerank_encoder_failure_raises_unavailable() -> None:
    encoder = FakeCrossEncoder(should_fail=True)
    service = RerankerService(encoder=encoder, threshold=0.10, max_results=10)
    with pytest.raises(RerankerUnavailableError):
        await service.rerank("query", [_chunk()])


@pytest.mark.asyncio
async def test_rerank_score_count_mismatch_raises_unavailable() -> None:
    encoder = FakeCrossEncoder(scores=[0.5])  # Only 1 score for 3 chunks
    service = RerankerService(encoder=encoder, threshold=0.10, max_results=10)
    with pytest.raises(RerankerUnavailableError, match="scores"):
        await service.rerank("query", [_chunk(), _chunk(), _chunk()])


@pytest.mark.asyncio
async def test_rerank_at_exact_threshold_kept() -> None:
    encoder = FakeCrossEncoder(scores=[0.10])
    service = RerankerService(encoder=encoder, threshold=0.10, max_results=10)
    result = await service.rerank("query", [_chunk()])
    assert len(result) == 1
    assert result[0].score == 0.10


@pytest.mark.asyncio
async def test_rerank_just_below_threshold_filtered() -> None:
    encoder = FakeCrossEncoder(scores=[0.099])
    service = RerankerService(encoder=encoder, threshold=0.10, max_results=10)
    result = await service.rerank("query", [_chunk()])
    assert len(result) == 0
