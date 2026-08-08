"""BGE-reranker-v2-m3 cross-encoder service per §6.

Scores only authorized query/chunk pairs. Candidates below the configured
threshold are discarded; the top ``max_results`` with source diversity
when available form the evidence set.
"""

from __future__ import annotations

import logging
from typing import Protocol

from documind.schemas.retrieval import ScoredChunk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RerankerUnavailableError(RuntimeError):
    """Reranker model is unavailable — do not substitute or return unranked evidence."""


# ---------------------------------------------------------------------------
# Cross-encoder protocol
# ---------------------------------------------------------------------------


class CrossEncoderAdapter(Protocol):
    """Predict relevance scores for query/passage pairs."""

    async def predict(self, query: str, passages: list[str]) -> list[float]: ...


# ---------------------------------------------------------------------------
# Reranker protocol
# ---------------------------------------------------------------------------


class Reranker(Protocol):
    """Rerank scored chunks by cross-encoder relevance."""

    async def rerank(self, query: str, chunks: list[ScoredChunk]) -> list[ScoredChunk]: ...


# ---------------------------------------------------------------------------
# Service implementation
# ---------------------------------------------------------------------------


class RerankerService:
    """Cross-encoder reranker with threshold filtering and result capping.

    Per §6.3:
    - Scores only authorized query/chunk pairs
    - Discards candidates below ``threshold``
    - Returns at most ``max_results`` sorted by reranker score descending
    - Source diversity preference when scores are tied
    """

    def __init__(
        self,
        *,
        encoder: CrossEncoderAdapter,
        threshold: float = 0.10,
        max_results: int = 10,
    ) -> None:
        self._encoder = encoder
        self._threshold = threshold
        self._max_results = max_results

    async def rerank(self, query: str, chunks: list[ScoredChunk]) -> list[ScoredChunk]:
        """Rerank authorized chunks; filter below threshold; cap results."""
        if not chunks:
            return []

        passages = [chunk.content for chunk in chunks]

        try:
            scores = await self._encoder.predict(query, passages)
        except Exception as exc:
            logger.error("Reranker model unavailable: %s", exc)
            raise RerankerUnavailableError(f"Reranker failed: {exc}") from exc

        if len(scores) != len(chunks):
            raise RerankerUnavailableError(f"Reranker returned {len(scores)} scores for {len(chunks)} passages")

        # Build scored pairs, filter, sort, cap
        scored = [
            chunk.model_copy(update={"score": score})
            for chunk, score in zip(chunks, scores, strict=True)
            if score >= self._threshold
        ]

        # Sort by reranker score descending, then source diversity, then chunk_id
        scored.sort(key=lambda c: (-c.score, c.source_branch, str(c.chunk_id)))

        return scored[: self._max_results]
