"""Qdrant and OpenSearch projection writers per §6.2.

Each writer satisfies the ``ProjectionWriter`` protocol from
:mod:`projection_service` and produces idempotent, deterministic
projections keyed by chunk UUID.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from documind.services.projection_service import (
    ProjectionBackend,
    ProjectionManifest,
    ProjectionSnapshot,
    ProjectionTransientError,
    manifest_checksum,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkProjectionPayload:
    """Full payload resolved for one chunk before writing to a backend.

    ``SnapshotRecord`` only carries the deterministic identity and hash.
    The concrete projection repository resolves these into the full
    canonical payload needed by each backend.
    """

    chunk_id: str
    version_id: str
    document_id: str
    content: str
    content_hash: str
    profile_revision: str
    label_ids: list[str]
    lifecycle: str
    tombstone_generation: int
    locale: str | None = None
    declared_type: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    section_path: list[str] | None = None
    embedding: list[float] | None = None


class ChunkPayloadResolver(Protocol):
    """Resolve full chunk payloads from a projection snapshot."""

    async def resolve_chunk_payloads(self, snapshot: ProjectionSnapshot) -> list[ChunkProjectionPayload]: ...


# ---------------------------------------------------------------------------
# Qdrant writer
# ---------------------------------------------------------------------------


class QdrantClient(Protocol):
    """Subset of qdrant-client async API used by the writer."""

    async def collection_exists(self, collection_name: str) -> bool: ...

    async def create_collection(self, collection_name: str, **kwargs: Any) -> Any: ...

    async def create_payload_index(self, collection_name: str, field_name: str, **kwargs: Any) -> Any: ...

    async def upsert(self, collection_name: str, points: Any, **kwargs: Any) -> Any: ...


_QDRANT_PAYLOAD_INDEXES = (
    "document_id",
    "version_id",
    "label_ids",
    "lifecycle",
    "tombstone_generation",
)


class QdrantProjectionWriter:
    """Idempotent Qdrant chunk upsert satisfying ``ProjectionWriter``.

    Payload per §6.2: version_id, document_id, content_hash, profile_revision,
    label_ids (copied), lifecycle snapshot, tombstone_generation, text_hash.
    """

    def __init__(
        self,
        *,
        client: QdrantClient,
        collection: str,
        embedding_dimension: int = 1024,
        payload_resolver: ChunkPayloadResolver,
    ) -> None:
        self._client = client
        self._collection = collection
        self._dimension = embedding_dimension
        self._resolver = payload_resolver

    async def ensure_collection(self) -> None:
        """Create collection and payload indexes if absent.

        Raises ``ProjectionTransientError`` when qdrant-client is not
        installed or when any payload index creation fails.  Collection
        setup failures must be visible — silent suppression would let the
        worker start without the required indexes.
        """
        try:
            from qdrant_client.models import Distance, VectorParams
        except ImportError as exc:
            raise ProjectionTransientError(
                "qdrant_client.models is required for collection setup"
            ) from exc

        exists = await self._client.collection_exists(self._collection)
        if not exists:
            await self._client.create_collection(
                self._collection,
                vectors_config=VectorParams(
                    size=self._dimension,
                    distance=Distance.COSINE,
                ),
            )
            logger.info("Created Qdrant collection %s (%d-dim cosine)", self._collection, self._dimension)

        for field_name in _QDRANT_PAYLOAD_INDEXES:
            await self._client.create_payload_index(
                self._collection,
                field_name=field_name,
            )

    async def project(self, snapshot: ProjectionSnapshot) -> ProjectionManifest:
        """Upsert all chunks as Qdrant points with embeddings."""
        try:
            from qdrant_client.models import PointStruct
        except ImportError as exc:
            raise ProjectionTransientError("qdrant-client is required") from exc

        payloads = await self._resolver.resolve_chunk_payloads(snapshot)

        points: list[Any] = []
        for payload in payloads:
            if payload.embedding is None:
                raise ProjectionTransientError(f"Chunk {payload.chunk_id} has no embedding vector")
            point = PointStruct(
                id=payload.chunk_id,
                vector=payload.embedding,
                payload={
                    "version_id": payload.version_id,
                    "document_id": payload.document_id,
                    "content_hash": payload.content_hash,
                    "profile_revision": payload.profile_revision,
                    "label_ids": payload.label_ids,
                    "lifecycle": payload.lifecycle,
                    "tombstone_generation": payload.tombstone_generation,
                    "text_hash": hashlib.sha256(payload.content.encode("utf-8")).hexdigest(),
                },
            )
            points.append(point)

        if points:
            try:
                await self._client.upsert(
                    self._collection,
                    points=points,
                    wait=True,
                )
            except (ConnectionError, OSError, TimeoutError) as exc:
                raise ProjectionTransientError(f"Qdrant upsert failed: {exc}") from exc

        # T6-08: Count only chunk records for this backend's manifest
        chunk_records = tuple(r for r in snapshot.records if r.projection_type == "chunk")
        return ProjectionManifest(
            backend=ProjectionBackend.QDRANT,
            snapshot_id=snapshot.snapshot_id,
            generation=snapshot.generation,
            tombstone_generation=snapshot.tombstone_generation,
            record_count=len(chunk_records),
            checksum=manifest_checksum(chunk_records),
        )

    async def delete_by_version(self, version_id: str) -> int:
        """Delete all points for a tombstoned/erased version."""
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue
        except ImportError as exc:
            raise ProjectionTransientError(
                "qdrant_client.models is required for delete operations"
            ) from exc

        try:
            await self._client.delete(
                self._collection,
                points_selector=Filter(
                    must=[FieldCondition(key="version_id", match=MatchValue(value=version_id))]
                ),
                wait=True,
            )
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise ProjectionTransientError(f"Qdrant delete failed: {exc}") from exc
        logger.info("Deleted Qdrant points for version %s", version_id)
        return 0  # Qdrant delete doesn't return count


# ---------------------------------------------------------------------------
# OpenSearch writer
# ---------------------------------------------------------------------------


class OpenSearchClient(Protocol):
    """Subset of opensearch-py async API used by the writer."""

    async def indices_exists(self, index: str) -> bool: ...  # type: ignore[override]

    async def indices_create(self, index: str, body: dict[str, Any]) -> Any: ...

    async def bulk(self, body: str, **kwargs: Any) -> dict[str, Any]: ...


_OPENSEARCH_INDEX_SETTINGS: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "text_analyzer": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "asciifolding"],
                },
                "english_analyzer": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "english_stemmer", "english_stop"],
                },
            },
            "filter": {
                "english_stemmer": {"type": "stemmer", "language": "english"},
                "english_stop": {"type": "stop", "stopwords": "_english_"},
            },
        },
    },
    "mappings": {
        "properties": {
            "content": {
                "type": "text",
                "analyzer": "text_analyzer",
                "fields": {
                    "english": {"type": "text", "analyzer": "english_analyzer"},
                },
            },
            "document_id": {"type": "keyword"},
            "version_id": {"type": "keyword"},
            "label_ids": {"type": "keyword"},
            "lifecycle": {"type": "keyword"},
            "tombstone_generation": {"type": "long"},
            "content_hash": {"type": "keyword"},
            "profile_revision": {"type": "keyword"},
            "declared_type": {"type": "keyword"},
            "locale": {"type": "keyword"},
            "page_start": {"type": "integer"},
            "page_end": {"type": "integer"},
            "section_path": {"type": "keyword"},
        },
    },
}


class OpenSearchProjectionWriter:
    """Idempotent OpenSearch BM25 text + keyword writer satisfying ``ProjectionWriter``.

    Payload per §6.2: same canonical IDs plus normalized text, locale,
    declared type, page/section metadata.
    """

    def __init__(
        self,
        *,
        client: OpenSearchClient,
        index_name: str,
        payload_resolver: ChunkPayloadResolver,
    ) -> None:
        self._client = client
        self._index = index_name
        self._resolver = payload_resolver

    async def ensure_index(self) -> None:
        """Create OpenSearch index with BM25 text + keyword fields if absent.

        Non-absence lookup errors propagate — treating every exception as
        "index missing" would mask connectivity or permissions failures.
        """
        try:
            exists = await self._client.indices_exists(self._index)
        except Exception as exc:
            raise ProjectionTransientError(
                f"OpenSearch index existence check failed: {exc}"
            ) from exc

        if not exists:
            await self._client.indices_create(self._index, body=_OPENSEARCH_INDEX_SETTINGS)
            logger.info("Created OpenSearch index %s", self._index)

    async def project(self, snapshot: ProjectionSnapshot) -> ProjectionManifest:
        """Upsert all chunks as OpenSearch documents."""
        payloads = await self._resolver.resolve_chunk_payloads(snapshot)

        if payloads:
            bulk_body = self._build_bulk_body(payloads)
            try:
                response = await self._client.bulk(body=bulk_body, index=self._index)
            except (ConnectionError, OSError, TimeoutError) as exc:
                raise ProjectionTransientError(f"OpenSearch bulk failed: {exc}") from exc
            if response.get("errors"):
                failed_items = [
                    item for item in response.get("items", []) if "error" in item.get("index", item.get("update", {}))
                ]
                if failed_items:
                    raise ProjectionTransientError(f"OpenSearch bulk had {len(failed_items)} errors")

        # T6-08: Count only chunk records for this backend's manifest
        chunk_records = tuple(r for r in snapshot.records if r.projection_type == "chunk")
        return ProjectionManifest(
            backend=ProjectionBackend.OPENSEARCH,
            snapshot_id=snapshot.snapshot_id,
            generation=snapshot.generation,
            tombstone_generation=snapshot.tombstone_generation,
            record_count=len(chunk_records),
            checksum=manifest_checksum(chunk_records),
        )

    @staticmethod
    def _build_bulk_body(payloads: list[ChunkProjectionPayload]) -> str:
        """Build an NDJSON bulk request body for idempotent upsert."""
        lines = []
        for payload in payloads:
            action = json.dumps({"index": {"_id": payload.chunk_id}})
            doc = json.dumps(
                {
                    "content": payload.content,
                    "document_id": payload.document_id,
                    "version_id": payload.version_id,
                    "content_hash": payload.content_hash,
                    "profile_revision": payload.profile_revision,
                    "label_ids": payload.label_ids,
                    "lifecycle": payload.lifecycle,
                    "tombstone_generation": payload.tombstone_generation,
                    "declared_type": payload.declared_type or "",
                    "locale": payload.locale or "",
                    "page_start": payload.page_start,
                    "page_end": payload.page_end,
                    "section_path": payload.section_path or [],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            lines.append(action)
            lines.append(doc)
        return "\n".join(lines) + "\n"

    async def delete_by_version(self, version_id: str) -> int:
        """Delete all documents for a tombstoned/erased version."""
        try:
            result = await self._client.delete_by_query(
                index=self._index,
                body={"query": {"term": {"version_id": version_id}}},
            )
            deleted = result.get("deleted", 0)
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise ProjectionTransientError(f"OpenSearch delete failed: {exc}") from exc
        logger.info("Deleted %d OpenSearch documents for version %s", deleted, version_id)
        return deleted
