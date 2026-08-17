"""Qdrant dense-vector retrieval backend per §6.

Executes BGE-M3 dense queries against the shared Qdrant collection with
server-built payload filters for version, lifecycle, and tombstone state.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Protocol

import structlog

from documind.schemas.retrieval import ScoredChunk
from documind.services.retrieval_service import AuthorizationContext, RetrievalBackend

logger = structlog.get_logger(__name__)


class DenseEmbedder(Protocol):
    """Produce a dense vector for a single query."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def dimension(self) -> int: ...


class QdrantAsyncClient(Protocol):
    """Subset of qdrant-client async API used for retrieval search."""

    async def search(
        self,
        collection_name: str,
        query_vector: list[float] | None = None,
        query_filter: Any = None,
        limit: int = 100,
        with_payload: bool = True,
        **kwargs: Any,
    ) -> list[Any]: ...

    async def collection_exists(self, collection_name: str) -> bool: ...


class QdrantRetrievalBackend:
    """Qdrant dense-vector search implementing :class:`RetrievalBackend`.

    Uses the shared Qdrant collection and builds payload filters from the
    authorization context to ensure only authorized, completed, non-tombstoned
    chunks are returned.
    """

    def __init__(
        self,
        *,
        client: Any,
        collection: str = "documind_chunks",
        embedding_dim: int = 1024,
        embedder: Any | None = None,
    ) -> None:
        self._client = client
        self._collection = collection
        self._embedding_dim = embedding_dim
        self._embedder = embedder

    @property
    def name(self) -> str:
        return "qdrant"

    async def search(
        self,
        query: str,
        context: AuthorizationContext,
        *,
        max_candidates: int = 100,
        deadline_ms: int = 750,
    ) -> list[ScoredChunk]:
        """Execute dense vector search against Qdrant.

        The query is expected to arrive pre-embedded in production (the
        embedding step happens at the orchestrator level).  This backend
        falls back to text-based search if available.
        """
        try:
            from qdrant_client.models import (
                FieldCondition,
                Filter,
                MatchAny,
                MatchValue,
            )
        except ImportError as exc:
            await logger.awarning("qdrant_models_unavailable", error=str(exc))
            return []

        if not context.allowed_version_ids:
            return []

        # Build Qdrant payload filter
        version_id_strs = [str(vid) for vid in context.allowed_version_ids]
        must_conditions = [
            FieldCondition(
                key="version_id",
                match=MatchAny(any=version_id_strs),
            ),
            FieldCondition(
                key="lifecycle",
                match=MatchValue(value="completed"),
            ),
            FieldCondition(
                key="tombstone_generation",
                match=MatchValue(value=0),
            ),
        ]
        # T7-04: Enforce allowed-label constraint from authorization context
        if context.allowed_label_ids:
            label_id_strs = [str(lid) for lid in context.allowed_label_ids]
            must_conditions.append(
                FieldCondition(
                    key="label_ids",
                    match=MatchAny(any=label_id_strs),
                )
            )
        query_filter = Filter(must=must_conditions)

        async def _do_search() -> list[ScoredChunk]:
            # Use dense vector search when an embedder is available
            if self._embedder is not None:
                try:
                    vectors = await self._embedder.embed([query])
                    query_vector = vectors[0]
                except Exception as exc:
                    await logger.awarning("qdrant_embedding_failed", error=str(exc))
                    query_vector = None
            else:
                query_vector = None

            results = await self._client.search(
                collection_name=self._collection,
                query_vector=query_vector,
                query_filter=query_filter,
                limit=max_candidates,
                with_payload=True,
            )
            return self._convert_results(results)

        try:
            return await asyncio.wait_for(
                _do_search(),
                timeout=deadline_ms / 1000.0,
            )
        except TimeoutError:
            await logger.awarning("qdrant_search_timeout", deadline_ms=deadline_ms)
            raise

    def _convert_results(self, results: list[Any]) -> list[ScoredChunk]:
        """Convert Qdrant search results to ScoredChunk."""
        chunks: list[ScoredChunk] = []
        for result in results:
            payload = getattr(result, "payload", {}) or {}
            try:
                content_sha256 = str(
                    payload.get("content_sha256")
                    or payload.get("content_hash", "")
                )
                chunk = ScoredChunk(
                    chunk_id=uuid.UUID(str(payload.get("chunk_id", result.id))),
                    version_id=uuid.UUID(str(payload["version_id"])),
                    document_id=uuid.UUID(str(payload["document_id"])),
                    content=str(payload.get("content", "")),
                    content_sha256=content_sha256,
                    page_start=payload.get("page_start"),
                    page_end=payload.get("page_end"),
                    section_path=payload.get("section_path", []),
                    score=float(getattr(result, "score", 0.0)),
                    source_branch="qdrant",
                )
                # Validate required fields
                if not chunk.content_sha256:
                    logger.warning(
                        "qdrant_chunk_missing_hash",
                        chunk_id=str(chunk.chunk_id),
                    )
                    continue
                chunks.append(chunk)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "qdrant_chunk_conversion_failed",
                    result_id=getattr(result, "id", "unknown"),
                    error=str(exc),
                )
        return chunks
