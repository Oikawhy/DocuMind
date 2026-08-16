"""Neo4j local retrieval backend per §6.

Performs entity lookup followed by bounded 1-2 hop traversal from matching
entities through Fact nodes to source Chunk nodes.  Constrains facts to
the active verified graph generation and checks graph health.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Protocol

import structlog

from documind.schemas.retrieval import ScoredChunk
from documind.services.retrieval_service import AuthorizationContext, RetrievalBackend

logger = structlog.get_logger(__name__)


class Neo4jAsyncDriver(Protocol):
    """Subset of neo4j async driver API used for retrieval."""

    def session(self, *, database: str = "neo4j") -> Any: ...


class Neo4jLocalRetrievalBackend:
    """Local entity-based retrieval with bounded hop traversal.

    Implements :class:`RetrievalBackend` for the ``neo4j_local`` mode:
    1. Extract keywords/entities from query
    2. Look up matching Entity nodes in Neo4j
    3. Traverse 1-2 hops to reach Fact → Chunk relationships
    4. Constrain to facts with ``generation == active_verified_generation``
    5. Resolve source chunks to authorized versions
    """

    def __init__(
        self,
        *,
        driver: Any,
        database: str = "neo4j",
        max_hops: int = 2,
        generation_manager: Any | None = None,
    ) -> None:
        self._driver = driver
        self._database = database
        self._max_hops = max_hops
        self._generation_manager = generation_manager

    @property
    def name(self) -> str:
        return "neo4j_local"

    async def search(
        self,
        query: str,
        context: AuthorizationContext,
        *,
        max_candidates: int = 100,
        deadline_ms: int = 750,
    ) -> list[ScoredChunk]:
        """Execute local entity-based graph retrieval."""
        if not context.allowed_version_ids:
            return []

        async def _do_search() -> list[ScoredChunk]:
            # Check graph health first
            active_generation = await self._get_active_generation()
            if active_generation is None:
                await logger.awarning("neo4j_local_no_active_generation")
                return []

            # Extract keywords for entity lookup (simple tokenization)
            keywords = self._extract_keywords(query)
            if not keywords:
                return []

            version_id_strs = [str(vid) for vid in context.allowed_version_ids]

            # Query: find entities matching keywords → traverse to facts → chunks
            cypher = """
            UNWIND $keywords AS keyword
            MATCH (e:Entity)
            WHERE toLower(e.normalized_key) CONTAINS toLower(keyword)
            MATCH path = (e)<-[:ABOUT|MENTIONS*1..{max_hops}]-(f:Fact {{generation: $generation}})
            WHERE f.tombstone_generation = 0
            MATCH (f)-[:SOURCED_FROM]->(c:Chunk)
            WHERE c.version_id IN $allowed_versions
            RETURN DISTINCT
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
                1.0 / (1.0 + length(path)) AS score
            ORDER BY score DESC
            LIMIT $limit
            """.replace("{max_hops}", str(self._max_hops))

            async with self._driver.session(database=self._database) as session:
                result = await session.run(
                    cypher,
                    keywords=keywords,
                    generation=active_generation,
                    allowed_versions=version_id_strs,
                    limit=max_candidates,
                )
                records = await result.data()

            return self._convert_results(records)

        try:
            return await asyncio.wait_for(
                _do_search(),
                timeout=deadline_ms / 1000.0,
            )
        except TimeoutError:
            await logger.awarning("neo4j_local_timeout", deadline_ms=deadline_ms)
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
                logger.warning("neo4j_generation_manager_failed", error=str(exc))
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
            logger.warning("neo4j_generation_check_failed", error=str(exc))
            return None

    def _extract_keywords(self, query: str) -> list[str]:
        """Extract meaningful keywords from the query for entity lookup.

        Simple tokenization — strips stop words and short tokens.
        """
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
                        source_branch="neo4j_local",
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "neo4j_local_conversion_failed",
                    error=str(exc),
                )
        return chunks
