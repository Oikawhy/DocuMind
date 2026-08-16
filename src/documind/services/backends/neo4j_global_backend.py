"""Neo4j global retrieval backend per §6.

Performs bounded path traversal across the knowledge graph with a hard
100-source-chunk limit.  Retains fact IDs, generation, and hop count
for citation provenance.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog

from documind.schemas.retrieval import ScoredChunk
from documind.services.retrieval_service import AuthorizationContext, RetrievalBackend

logger = structlog.get_logger(__name__)


class Neo4jGlobalRetrievalBackend:
    """Global graph traversal implementing :class:`RetrievalBackend`.

    Traverses relationship paths across the knowledge graph, collecting
    source chunks with a hard cap of ``max_sources`` (default 100).
    Checks graph health and retains provenance metadata.
    """

    def __init__(
        self,
        *,
        driver: Any,
        database: str = "neo4j",
        max_sources: int = 100,
        max_path_length: int = 4,
        generation_manager: Any | None = None,
    ) -> None:
        self._driver = driver
        self._database = database
        self._max_sources = max_sources
        self._max_path_length = max_path_length
        self._generation_manager = generation_manager

    @property
    def name(self) -> str:
        return "neo4j_global"

    async def search(
        self,
        query: str,
        context: AuthorizationContext,
        *,
        max_candidates: int = 100,
        deadline_ms: int = 750,
    ) -> list[ScoredChunk]:
        """Execute global path traversal across the knowledge graph."""
        if not context.allowed_version_ids:
            return []

        # Apply the hard 100-source cap
        effective_limit = min(max_candidates, self._max_sources)

        async def _do_search() -> list[ScoredChunk]:
            active_generation = await self._get_active_generation()
            if active_generation is None:
                await logger.awarning("neo4j_global_no_active_generation")
                return []

            # Extract search terms
            keywords = self._extract_keywords(query)
            if not keywords:
                return []

            version_id_strs = [str(vid) for vid in context.allowed_version_ids]

            # Global traversal: find facts connected through entity
            # relationships, traverse paths up to max_path_length,
            # and collect source chunks
            cypher = """
            UNWIND $keywords AS keyword
            MATCH (e:Entity)
            WHERE toLower(e.normalized_key) CONTAINS toLower(keyword)
            MATCH path = (e)<-[:ABOUT|MENTIONS|RELATES_TO*1..{max_path}]-(f:Fact {{generation: $generation}})
            WHERE f.tombstone_generation = 0
            MATCH (f)-[:SOURCED_FROM]->(c:Chunk)
            WHERE c.version_id IN $allowed_versions
            WITH DISTINCT c, f, path,
                 1.0 / (1.0 + length(path)) AS relevance_score
            RETURN
                c.chunk_id AS chunk_id,
                c.version_id AS version_id,
                c.document_id AS document_id,
                c.content AS content,
                c.content_sha256 AS content_sha256,
                c.page_start AS page_start,
                c.page_end AS page_end,
                c.section_path AS section_path,
                f.fact_id AS fact_id,
                f.generation AS generation,
                length(path) AS hop_count,
                relevance_score AS score
            ORDER BY score DESC
            LIMIT $limit
            """.replace("{max_path}", str(self._max_path_length))

            async with self._driver.session(database=self._database) as session:
                result = await session.run(
                    cypher,
                    keywords=keywords,
                    generation=active_generation,
                    allowed_versions=version_id_strs,
                    limit=effective_limit,
                )
                records = await result.data()

            return self._convert_results(records)

        try:
            return await asyncio.wait_for(
                _do_search(),
                timeout=deadline_ms / 1000.0,
            )
        except TimeoutError:
            await logger.awarning("neo4j_global_timeout", deadline_ms=deadline_ms)
            raise

    async def _get_active_generation(self) -> int | None:
        """Query the active verified graph generation.

        T6-19: Prefers the ``ActiveGenerationManager`` registry when
        available.  Falls back to querying Neo4j ``max(f.generation)``
        only when no manager is injected.
        """
        if self._generation_manager is not None:
            try:
                return await self._generation_manager.current("neo4j", "global")
            except Exception as exc:
                logger.warning("neo4j_global_generation_manager_failed", error=str(exc))
                return None

        # Fallback: query Neo4j directly (not recommended — T6-19)
        cypher = """
        MATCH (f:Fact)
        WHERE f.tombstone_generation = 0
        RETURN max(f.generation) AS active_generation
        """
        try:
            async with self._driver.session(database=self._database) as session:
                result = await session.run(cypher)
                record = await result.single()
                if record and record["active_generation"] is not None:
                    return int(record["active_generation"])
                return None
        except Exception as exc:
            logger.warning("neo4j_global_generation_check_failed", error=str(exc))
            return None

    def _extract_keywords(self, query: str) -> list[str]:
        """Extract meaningful keywords from the query."""
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "being", "have", "has", "had", "do", "does", "did", "will",
            "would", "could", "should", "may", "might", "shall", "can",
            "and", "or", "but", "not", "no", "nor", "so", "yet", "both",
            "either", "neither", "each", "every", "all", "any", "few",
            "more", "most", "other", "some", "such", "than", "too",
            "very", "just", "about", "above", "after", "again",
            "for", "from", "in", "into", "of", "on", "out", "over",
            "to", "up", "with", "what", "which", "who", "whom",
            "this", "that", "these", "those", "it", "its", "my",
            "your", "his", "her", "our", "their", "how", "when",
            "where", "why",
        }
        tokens = query.lower().split()
        return [t for t in tokens if len(t) > 2 and t not in stop_words]

    def _convert_results(self, records: list[dict[str, Any]]) -> list[ScoredChunk]:
        """Convert Neo4j records to ScoredChunk list."""
        chunks: list[ScoredChunk] = []
        seen_chunk_ids: set[str] = set()

        for record in records:
            try:
                chunk_id_str = str(record["chunk_id"])
                if chunk_id_str in seen_chunk_ids:
                    continue
                seen_chunk_ids.add(chunk_id_str)

                chunks.append(
                    ScoredChunk(
                        chunk_id=uuid.UUID(chunk_id_str),
                        version_id=uuid.UUID(str(record["version_id"])),
                        document_id=uuid.UUID(str(record["document_id"])),
                        content=str(record.get("content", "")),
                        content_sha256=str(record.get("content_sha256", "")),
                        page_start=record.get("page_start"),
                        page_end=record.get("page_end"),
                        section_path=record.get("section_path", []),
                        score=float(record.get("score", 0.0)),
                        source_branch="neo4j_global",
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "neo4j_global_conversion_failed",
                    error=str(exc),
                )
        return chunks
