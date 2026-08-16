"""OpenSearch BM25/keyword retrieval backend per §6.

Executes BM25 multi-match queries against the shared OpenSearch index
with server-built filters for version, lifecycle, and tombstone state.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Protocol

import structlog

from documind.schemas.retrieval import ScoredChunk
from documind.services.retrieval_service import AuthorizationContext, RetrievalBackend

logger = structlog.get_logger(__name__)


class OpenSearchAsyncClient(Protocol):
    """Subset of opensearch-py async API used for retrieval search."""

    async def search(self, index: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]: ...


class OpenSearchRetrievalBackend:
    """OpenSearch BM25 keyword search implementing :class:`RetrievalBackend`.

    Builds multi-match queries with server-side filters from the
    authorization context.
    """

    def __init__(
        self,
        *,
        client: Any,
        index_name: str = "documind_chunks",
    ) -> None:
        self._client = client
        self._index = index_name

    @property
    def name(self) -> str:
        return "opensearch"

    async def search(
        self,
        query: str,
        context: AuthorizationContext,
        *,
        max_candidates: int = 100,
        deadline_ms: int = 750,
    ) -> list[ScoredChunk]:
        """Execute BM25 multi-match search against OpenSearch."""
        if not context.allowed_version_ids:
            return []

        version_id_strs = [str(vid) for vid in context.allowed_version_ids]

        search_body: dict[str, Any] = {
            "size": max_candidates,
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["content", "content.english"],
                                "type": "best_fields",
                            }
                        }
                    ],
                    "filter": [
                        {"terms": {"version_id": version_id_strs}},
                        {"term": {"lifecycle": "completed"}},
                        {"term": {"tombstone_generation": 0}},
                    ],
                }
            },
            "_source": [
                "chunk_id",
                "version_id",
                "document_id",
                "content",
                "content_sha256",
                "page_start",
                "page_end",
                "section_path",
            ],
        }

        async def _do_search() -> list[ScoredChunk]:
            response = await self._client.search(
                index=self._index,
                body=search_body,
            )
            return self._convert_results(response)

        try:
            return await asyncio.wait_for(
                _do_search(),
                timeout=deadline_ms / 1000.0,
            )
        except TimeoutError:
            await logger.awarning("opensearch_timeout", deadline_ms=deadline_ms)
            raise

    def _convert_results(self, response: dict[str, Any]) -> list[ScoredChunk]:
        """Convert OpenSearch response to ScoredChunk list."""
        chunks: list[ScoredChunk] = []
        hits = response.get("hits", {}).get("hits", [])

        for hit in hits:
            source = hit.get("_source", {})
            try:
                chunk = ScoredChunk(
                    chunk_id=uuid.UUID(str(source["chunk_id"])),
                    version_id=uuid.UUID(str(source["version_id"])),
                    document_id=uuid.UUID(str(source["document_id"])),
                    content=str(source.get("content", "")),
                    content_sha256=str(source.get("content_sha256", "")),
                    page_start=source.get("page_start"),
                    page_end=source.get("page_end"),
                    section_path=source.get("section_path", []),
                    score=float(hit.get("_score", 0.0)),
                    source_branch="opensearch",
                )
                if not chunk.content_sha256:
                    logger.warning(
                        "opensearch_chunk_missing_hash",
                        chunk_id=str(chunk.chunk_id),
                    )
                    continue
                chunks.append(chunk)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "opensearch_chunk_conversion_failed",
                    hit_id=hit.get("_id", "unknown"),
                    error=str(exc),
                )
        return chunks
