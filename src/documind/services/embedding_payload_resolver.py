"""Embed canonical chunk payloads without coupling to projection backends."""

from __future__ import annotations

from dataclasses import replace

from documind.services.embedding_service import DenseEmbedder, EmbeddingServiceError
from documind.services.indexing_service import ChunkPayloadResolver, ChunkProjectionPayload
from documind.services.projection_service import ProjectionSnapshot

_EMBEDDING_DIMENSION = 1024


class EmbeddingChunkPayloadResolver:
    """Decorate canonical chunk resolution with validated dense embeddings."""

    def __init__(self, resolver: ChunkPayloadResolver, embedder: DenseEmbedder) -> None:
        self._resolver = resolver
        self._embedder = embedder

    async def resolve_chunk_payloads(self, snapshot: ProjectionSnapshot) -> list[ChunkProjectionPayload]:
        """Resolve canonical payloads and attach one validated vector to each."""
        payloads = await self._resolver.resolve_chunk_payloads(snapshot)
        if not payloads:
            return []

        vectors = await self._embedder.embed([payload.content for payload in payloads])
        if len(vectors) != len(payloads):
            raise EmbeddingServiceError(
                "Embedding response cardinality mismatch: "
                f"expected {len(payloads)} vectors, received {len(vectors)}."
            )

        for index, vector in enumerate(vectors):
            try:
                dimension = len(vector)
            except TypeError as exc:
                raise EmbeddingServiceError(
                    f"Embedding vector at index {index} has no dimension; expected {_EMBEDDING_DIMENSION}."
                ) from exc
            if dimension != _EMBEDDING_DIMENSION:
                raise EmbeddingServiceError(
                    f"Embedding vector at index {index} has dimension {dimension}; "
                    f"expected {_EMBEDDING_DIMENSION}."
                )

        return [replace(payload, embedding=vector) for payload, vector in zip(payloads, vectors, strict=True)]
