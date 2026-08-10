"""Contracts for enriching canonical chunk payloads with dense embeddings."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

import pytest

import documind.services.embedding_payload_resolver as resolver_module
from documind.services.embedding_payload_resolver import EmbeddingChunkPayloadResolver
from documind.services.embedding_service import EmbeddingServiceError
from documind.services.indexing_service import ChunkProjectionPayload
from documind.services.projection_service import ProjectionSnapshot, SnapshotRecord


@dataclass
class CanonicalResolver:
    payloads: list[ChunkProjectionPayload]
    snapshots: list[ProjectionSnapshot] = field(default_factory=list)

    async def resolve_chunk_payloads(self, snapshot: ProjectionSnapshot) -> list[ChunkProjectionPayload]:
        self.snapshots.append(snapshot)
        return self.payloads


@dataclass
class Embedder:
    vectors: list[list[float]]
    calls: list[list[str]] = field(default_factory=list)

    @property
    def dimension(self) -> int:
        return 1024

    @property
    def model_digest(self) -> str:
        return "sha256:" + "0" * 64

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


@pytest.fixture
def snapshot() -> ProjectionSnapshot:
    return ProjectionSnapshot(
        snapshot_id="snapshot-1",
        run_id="run-1",
        version_id="version-1",
        generation=7,
        tombstone_generation=3,
        records=(
            SnapshotRecord(
                deterministic_id="chunk-1",
                canonical_payload_hash="a" * 64,
                projection_type="chunk",
            ),
        ),
    )


def _payload(
    chunk_id: str,
    content: str,
    *,
    embedding: list[float] | None = None,
) -> ChunkProjectionPayload:
    return ChunkProjectionPayload(
        chunk_id=chunk_id,
        version_id="version-1",
        document_id="document-1",
        content=content,
        content_hash=f"hash-{chunk_id}",
        profile_revision="profile-1",
        label_ids=["label-a", "label-b"],
        lifecycle="active",
        tombstone_generation=3,
        locale="en",
        declared_type="pdf",
        page_start=2,
        page_end=3,
        section_path=["Chapter 1", "Section 2"],
        embedding=embedding,
    )


@pytest.mark.asyncio
async def test_resolve_embeds_canonical_content_in_order_and_replaces_vectors(
    snapshot: ProjectionSnapshot,
) -> None:
    canonical = [_payload("chunk-1", "first"), _payload("chunk-2", "second")]
    first_vector = [1.0] * 1024
    second_vector = [2.0] * 1024
    canonical_resolver = CanonicalResolver(canonical)
    embedder = Embedder([first_vector, second_vector])

    resolved = await EmbeddingChunkPayloadResolver(canonical_resolver, embedder).resolve_chunk_payloads(snapshot)

    assert canonical_resolver.snapshots == [snapshot]
    assert embedder.calls == [["first", "second"]]
    assert [payload.chunk_id for payload in resolved] == ["chunk-1", "chunk-2"]
    assert [payload.embedding for payload in resolved] == [first_vector, second_vector]


@pytest.mark.asyncio
async def test_resolve_preserves_canonical_payload_metadata_without_mutating_source(
    snapshot: ProjectionSnapshot,
) -> None:
    prior_embedding = [0.5] * 1024
    canonical = _payload("chunk-1", "canonical content", embedding=prior_embedding)
    canonical_payloads = [canonical]
    original_label_ids = list(canonical.label_ids)
    original_section_path = list(canonical.section_path or [])
    replacement_embedding = [1.0] * 1024

    resolved = await EmbeddingChunkPayloadResolver(
        CanonicalResolver(canonical_payloads),
        Embedder([replacement_embedding]),
    ).resolve_chunk_payloads(snapshot)

    assert resolved is not canonical_payloads
    assert resolved[0] is not canonical
    assert resolved[0].embedding == replacement_embedding
    assert canonical.embedding == prior_embedding
    assert canonical.label_ids == original_label_ids
    assert canonical.section_path == original_section_path
    assert resolved[0].label_ids == canonical.label_ids
    assert resolved[0].section_path == canonical.section_path
    assert resolved[0].content_hash == canonical.content_hash
    assert resolved[0].profile_revision == canonical.profile_revision


@pytest.mark.asyncio
async def test_resolve_rejects_embedding_response_cardinality_mismatch(snapshot: ProjectionSnapshot) -> None:
    canonical = [_payload("chunk-1", "first"), _payload("chunk-2", "second")]

    with pytest.raises(EmbeddingServiceError) as error:
        await EmbeddingChunkPayloadResolver(
            CanonicalResolver(canonical),
            Embedder([[1.0] * 1024]),
        ).resolve_chunk_payloads(snapshot)

    assert str(error.value) == "Embedding response cardinality mismatch: expected 2 vectors, received 1."


@pytest.mark.asyncio
async def test_resolve_rejects_non_contract_embedding_dimension(snapshot: ProjectionSnapshot) -> None:
    canonical = [_payload("chunk-1", "first")]

    with pytest.raises(EmbeddingServiceError) as error:
        await EmbeddingChunkPayloadResolver(
            CanonicalResolver(canonical),
            Embedder([[1.0] * 1023]),
        ).resolve_chunk_payloads(snapshot)

    assert str(error.value) == "Embedding vector at index 0 has dimension 1023; expected 1024."


@pytest.mark.asyncio
async def test_resolve_returns_empty_without_calling_embedder(snapshot: ProjectionSnapshot) -> None:
    embedder = Embedder([[1.0] * 1024])

    resolved = await EmbeddingChunkPayloadResolver(
        CanonicalResolver([]),
        embedder,
    ).resolve_chunk_payloads(snapshot)

    assert resolved == []
    assert embedder.calls == []


def test_resolver_module_has_no_qdrant_coupling() -> None:
    assert "qdrant" not in inspect.getsource(resolver_module).lower()
