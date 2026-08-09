"""BGE-M3 dense embedding service with digest-pinned model loading.

The service loads the model eagerly at construction and verifies a SHA-256
digest of the model binary to ensure deterministic, reproducible embeddings.
All inference runs via ``asyncio.to_thread`` to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmbeddingIntegrityError(RuntimeError):
    """Model files failed SHA-256 digest verification."""


class EmbeddingServiceError(RuntimeError):
    """Inference-time failure that may be retried by the caller."""


# ---------------------------------------------------------------------------
# Protocol for callers that only need the embed contract
# ---------------------------------------------------------------------------


class DenseEmbedder(Protocol):
    """Produce dense vectors for a batch of texts."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def dimension(self) -> int: ...

    @property
    def model_digest(self) -> str: ...


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_DIMENSION = 1024
_MAX_BATCH_SIZE = 32


@dataclass(frozen=True)
class EmbeddingModelConfig:
    """Non-secret configuration for the pinned embedding model.

    Production deployments must supply a non-empty ``expected_digest``
    and use the required 1024-dimensional BGE-M3 contract.  Test and
    development environments may set ``allow_unverified=True`` to bypass
    the digest requirement while still enforcing the dimension.
    """

    model_name_or_path: str = "BAAI/bge-m3"
    expected_digest: str = ""
    dimension: int = _DEFAULT_DIMENSION
    max_batch_size: int = _MAX_BATCH_SIZE
    normalize: bool = True
    allow_unverified: bool = False

    def __post_init__(self) -> None:
        if self.dimension != _DEFAULT_DIMENSION:
            raise ValueError(
                f"BGE-M3 contract requires dimension={_DEFAULT_DIMENSION}, got {self.dimension}"
            )
        if not self.expected_digest and not self.allow_unverified:
            raise ValueError(
                "expected_digest must be a non-empty SHA-256 hex string; "
                "an unrevisioned model identity is not permitted in production. "
                "Set allow_unverified=True for test environments."
            )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class EmbeddingService:
    """BGE-M3 1024-dim dense embedding with eager loading and digest pinning.

    Satisfies both :class:`DenseEmbedder` and the existing
    ``SentenceEmbedder`` protocol from :mod:`documind.services.chunking_service`.
    """

    def __init__(self, *, config: EmbeddingModelConfig) -> None:
        self._config = config
        self._dimension = config.dimension
        self._model_digest = ""
        self._model: object | None = None
        self._encode_fn = None

    # -- lifecycle -----------------------------------------------------------

    def load(self) -> None:
        """Eagerly load the model and verify the digest.

        Called once at worker startup.  Raises ``EmbeddingIntegrityError``
        when the on-disk model does not match the expected digest.
        """
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingServiceError("sentence-transformers is required; install the 'ml' extra.") from exc

        logger.info("Loading embedding model %s …", self._config.model_name_or_path)
        model = SentenceTransformer(
            self._config.model_name_or_path,
            truncate_dim=self._config.dimension,
        )
        self._model = model
        self._encode_fn = model.encode

        # Compute digest of the model checkpoint files
        self._model_digest = self._compute_digest(model)

        if self._config.expected_digest and self._model_digest != self._config.expected_digest:
            raise EmbeddingIntegrityError(
                f"Model digest mismatch: expected {self._config.expected_digest}, got {self._model_digest}"
            )
        logger.info(
            "Embedding model loaded: dim=%d digest=%s",
            self._dimension,
            self._model_digest[:16] + "…",
        )

    @staticmethod
    def _compute_digest(model: object) -> str:
        """SHA-256 over sorted model file contents for deterministic identity."""
        model_path = Path(getattr(model, "model_card_data", None) and "" or "")
        # Attempt to locate model directory from the SentenceTransformer
        try:
            # sentence-transformers stores the path on the underlying auto model
            auto_model = getattr(model, "_modules", {}).get("0", None)
            if auto_model is not None:
                auto_model_inner = getattr(auto_model, "auto_model", auto_model)
                config = getattr(auto_model_inner, "config", None)
                if config is not None:
                    name_or_path = getattr(config, "_name_or_path", "")
                    if name_or_path and Path(name_or_path).is_dir():
                        model_path = Path(name_or_path)
        except Exception:
            pass

        if not model_path.is_dir():
            # Fallback: hash the model's state_dict parameter bytes
            try:
                state_dict = model.state_dict() if hasattr(model, "state_dict") else {}
                hasher = hashlib.sha256()
                for key in sorted(state_dict.keys()):
                    hasher.update(key.encode("utf-8"))
                    hasher.update(state_dict[key].cpu().numpy().tobytes())
                return hasher.hexdigest()
            except Exception as exc:
                raise EmbeddingIntegrityError(f"Cannot compute model digest: {exc}") from exc

        hasher = hashlib.sha256()
        for file_path in sorted(model_path.rglob("*")):
            if file_path.is_file() and file_path.suffix in {
                ".bin",
                ".safetensors",
                ".json",
                ".txt",
            }:
                hasher.update(file_path.name.encode("utf-8"))
                hasher.update(file_path.read_bytes())
        return hasher.hexdigest()

    # -- embedding -----------------------------------------------------------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Produce 1024-dim dense vectors for every input text.

        Batches internally to ``max_batch_size`` texts per encode call
        to bound peak memory.  Thread-safe via ``asyncio.to_thread``.
        """
        if not texts:
            return []
        if self._encode_fn is None:
            raise EmbeddingServiceError("Model not loaded; call load() first.")

        try:
            vectors = await asyncio.to_thread(self._encode_sync, texts)
        except Exception as exc:
            raise EmbeddingServiceError(f"Embedding inference failed: {exc}") from exc
        return vectors

    def _encode_sync(self, texts: list[str]) -> list[list[float]]:
        """Synchronous batch encode, chunked by max_batch_size."""
        assert self._encode_fn is not None
        all_vectors: list[list[float]] = []
        batch_size = self._config.max_batch_size

        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            embeddings = self._encode_fn(
                batch,
                normalize_embeddings=self._config.normalize,
                show_progress_bar=False,
            )
            # Convert numpy arrays to plain lists
            for vec in embeddings:
                all_vectors.append(vec.tolist() if hasattr(vec, "tolist") else list(vec))

        return all_vectors

    # -- SentenceEmbedder protocol compatibility -----------------------------

    def embed_sync(self, sentences: list[str]) -> list[Sequence[float]]:
        """Synchronous embed for the ``SentenceEmbedder`` protocol.

        The chunking service calls this from a synchronous context during
        the vector-based semantic splitting strategy.
        """
        return self._encode_sync(sentences)  # type: ignore[return-value]

    # -- properties ----------------------------------------------------------

    @property
    def dimension(self) -> int:
        """Embedding vector dimensionality (1024 for BGE-M3)."""
        return self._dimension

    @property
    def model_digest(self) -> str:
        """SHA-256 digest of the loaded model files."""
        return self._model_digest

    def verify_digest(self) -> None:
        """Re-verify the model digest; raise on mismatch."""
        if not self._config.expected_digest:
            return
        if self._model_digest != self._config.expected_digest:
            raise EmbeddingIntegrityError(
                f"Model digest mismatch: expected {self._config.expected_digest}, got {self._model_digest}"
            )
