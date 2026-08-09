"""Embedding service unit tests using stubs (no real BGE-M3 weights required)."""

from __future__ import annotations

from typing import Any

import pytest

from documind.services.embedding_service import (
    EmbeddingIntegrityError,
    EmbeddingModelConfig,
    EmbeddingService,
    EmbeddingServiceError,
)

# ---------------------------------------------------------------------------
# Stub model for tests
# ---------------------------------------------------------------------------


class StubEncoder:
    """Mimics SentenceTransformer.encode output."""

    def __init__(self, dimension: int = 1024) -> None:
        self._dim = dimension

    def __call__(
        self,
        texts: list[str],
        normalize_embeddings: bool = True,
        show_progress_bar: bool = False,
    ) -> list[list[float]]:
        return [[float(i + j) / max(1, len(t)) for j in range(self._dim)] for i, t in enumerate(texts)]


class StubSentenceTransformer:
    """Minimal stub replacing sentence_transformers.SentenceTransformer."""

    def __init__(self, model_name: str, truncate_dim: int | None = None) -> None:
        self._name = model_name
        self._dim = truncate_dim or 1024
        self.encode = StubEncoder(self._dim)

    def state_dict(self) -> dict[str, Any]:
        return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    expected_digest: str = "test-digest-sha256",
    allow_unverified: bool = False,
) -> EmbeddingService:
    config = EmbeddingModelConfig(
        model_name_or_path="stub-model",
        expected_digest=expected_digest,
        dimension=1024,
        allow_unverified=allow_unverified,
    )
    service = EmbeddingService(config=config)
    # Bypass actual model loading by injecting stub
    service._model = StubSentenceTransformer("stub-model", 1024)
    service._encode_fn = service._model.encode  # type: ignore[union-attr]
    service._model_digest = "abc123"
    service._dimension = 1024
    return service


# ---------------------------------------------------------------------------
# Config validation tests (T6-04)
# ---------------------------------------------------------------------------


def test_config_rejects_empty_digest() -> None:
    """Production config rejects empty expected_digest."""
    with pytest.raises(ValueError, match="expected_digest"):
        EmbeddingModelConfig(expected_digest="")


def test_config_rejects_non_1024_dimension() -> None:
    """BGE-M3 contract enforces 1024 dimensions."""
    with pytest.raises(ValueError, match="1024"):
        EmbeddingModelConfig(dimension=768, expected_digest="abc123")


def test_config_allows_unverified_for_tests() -> None:
    """Test environments can skip digest enforcement."""
    config = EmbeddingModelConfig(allow_unverified=True)
    assert config.expected_digest == ""
    assert config.dimension == 1024


def test_config_accepts_valid_production_config() -> None:
    """Valid production config passes validation."""
    config = EmbeddingModelConfig(expected_digest="sha256-digest-here")
    assert config.dimension == 1024
    assert config.expected_digest == "sha256-digest-here"


# ---------------------------------------------------------------------------
# Embedding service tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_returns_correct_dimension() -> None:
    """Verify 1024-dim output for a single text."""
    service = _make_service()
    result = await service.embed(["hello world"])
    assert len(result) == 1
    assert len(result[0]) == 1024


@pytest.mark.asyncio
async def test_embed_batch_preserves_order() -> None:
    """N inputs produce N outputs in the same order."""
    service = _make_service()
    texts = ["alpha", "beta", "gamma"]
    result = await service.embed(texts)
    assert len(result) == len(texts)
    # Different texts should produce different vectors
    assert result[0] != result[1]
    assert result[1] != result[2]


@pytest.mark.asyncio
async def test_embed_empty_input() -> None:
    """Empty list returns empty list."""
    service = _make_service()
    result = await service.embed([])
    assert result == []


def test_digest_mismatch_raises() -> None:
    """Wrong digest raises EmbeddingIntegrityError."""
    service = _make_service(expected_digest="expected_abc123")
    # model_digest is "abc123", expected is "expected_abc123"
    with pytest.raises(EmbeddingIntegrityError, match="mismatch"):
        service.verify_digest()


def test_digest_match_passes() -> None:
    """Correct digest does not raise."""
    service = _make_service(expected_digest="abc123")
    service.verify_digest()  # Should not raise


def test_no_expected_digest_skips_verification() -> None:
    """Empty expected digest skips verification entirely."""
    service = _make_service(allow_unverified=True, expected_digest="")
    service.verify_digest()  # Should not raise


@pytest.mark.asyncio
async def test_embed_deterministic() -> None:
    """Same text produces the same vector."""
    service = _make_service()
    text = "deterministic check"
    result1 = await service.embed([text])
    result2 = await service.embed([text])
    assert result1 == result2


@pytest.mark.asyncio
async def test_embed_raises_when_not_loaded() -> None:
    """Calling embed before load raises EmbeddingServiceError."""
    config = EmbeddingModelConfig(model_name_or_path="stub", allow_unverified=True)
    service = EmbeddingService(config=config)
    with pytest.raises(EmbeddingServiceError, match="not loaded"):
        await service.embed(["test"])


def test_dimension_property() -> None:
    """Dimension property returns configured value."""
    service = _make_service()
    assert service.dimension == 1024


def test_model_digest_property() -> None:
    """model_digest returns the computed digest."""
    service = _make_service()
    assert service.model_digest == "abc123"


def test_embed_sync_for_sentence_embedder_protocol() -> None:
    """embed_sync satisfies the SentenceEmbedder protocol from chunking_service."""
    service = _make_service()
    result = service.embed_sync(["sentence one", "sentence two"])
    assert len(result) == 2
    assert all(len(vec) == 1024 for vec in result)


@pytest.mark.asyncio
async def test_embed_batches_large_inputs() -> None:
    """Inputs larger than max_batch_size are batched internally."""
    config = EmbeddingModelConfig(
        model_name_or_path="stub",
        max_batch_size=2,
        allow_unverified=True,
    )
    service = EmbeddingService(config=config)
    service._model = StubSentenceTransformer("stub", 1024)
    service._encode_fn = service._model.encode  # type: ignore[union-attr]
    service._model_digest = "test"

    texts = ["a", "b", "c", "d", "e"]
    result = await service.embed(texts)
    assert len(result) == 5
