"""BGE-M3 dense embeddings from a digest-pinned local model artifact.

The service deliberately accepts only a local directory and verifies its
complete contents before the model loader is invoked.  Inference runs via
``asyncio.to_thread`` so callers do not block the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

_DEFAULT_DIMENSION = 1024
_MAX_BATCH_SIZE = 32
_INFERENCE_MAX_WORKERS = 1
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class EmbeddingIntegrityError(RuntimeError):
    """Model files failed SHA-256 digest verification."""


class EmbeddingServiceError(RuntimeError):
    """Embedding model loading or inference failed."""


class DenseEmbedder(Protocol):
    """Produce dense vectors for a batch of texts."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def dimension(self) -> int: ...

    @property
    def model_digest(self) -> str: ...


def compute_artifact_digest(root: Path) -> str:
    """Return the digest of every regular file in a local model artifact.

    File names form part of the digest, preventing an artifact from being
    substituted with an equivalent byte sequence arranged under other paths.
    Links are forbidden because their target may change after validation.
    """
    if root.is_symlink():
        raise ValueError(f"Embedding model artifact root may not be a symlink: {root}")
    try:
        resolved_root = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"Embedding model artifact does not exist: {root}") from exc

    if not resolved_root.is_dir():
        raise ValueError(f"Embedding model artifact must be an existing directory: {root}")

    entries = sorted(
        resolved_root.rglob("*"),
        key=lambda path: path.relative_to(resolved_root).as_posix(),
    )
    for entry in entries:
        if entry.is_symlink():
            raise ValueError(f"Embedding model artifact may not contain symlinks: {entry}")

    regular_files = [entry for entry in entries if entry.is_file()]
    if not regular_files:
        raise ValueError(f"Embedding model artifact contains no regular files: {resolved_root}")

    hasher = hashlib.sha256()
    for file_path in regular_files:
        relative_path = file_path.relative_to(resolved_root).as_posix()
        hasher.update(relative_path.encode("utf-8"))
        hasher.update(b"\0")
        with file_path.open("rb") as artifact_file:
            for block in iter(lambda: artifact_file.read(1024 * 1024), b""):
                hasher.update(block)
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


@dataclass(frozen=True)
class EmbeddingModelConfig:
    """Validated, immutable identity for the local BGE-M3 artifact."""

    model_path: Path
    expected_digest: str
    dimension: int = _DEFAULT_DIMENSION
    max_batch_size: int = _MAX_BATCH_SIZE
    normalize: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.model_path, Path):
            raise ValueError("model_path must be a pathlib.Path pointing to a local artifact directory")
        if not self.model_path.is_absolute():
            raise ValueError("model_path must be an absolute path to a local model artifact directory")
        if self.model_path.is_symlink():
            raise ValueError(f"model_path may not be a symlink: {self.model_path}")
        try:
            resolved_path = self.model_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(
                f"model_path must be an existing directory; not found: {self.model_path}"
            ) from exc
        if not resolved_path.is_dir():
            raise ValueError(f"model_path must be an existing directory: {self.model_path}")
        if not _DIGEST_PATTERN.fullmatch(self.expected_digest):
            raise ValueError(
                "expected_digest must be 'sha256:' followed by 64 lowercase hexadecimal characters"
            )
        if self.dimension != _DEFAULT_DIMENSION:
            raise ValueError(
                f"BGE-M3 contract requires dimension={_DEFAULT_DIMENSION}, got {self.dimension}"
            )
        if self.max_batch_size < 1:
            raise ValueError("max_batch_size must be at least 1")
        object.__setattr__(self, "model_path", resolved_path)


def _default_model_loader(model_path: Path) -> object:
    """Load only the pinned local artifact; never resolve a remote model name."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise EmbeddingServiceError(
            "sentence-transformers is required; install the 'ml' extra."
        ) from exc
    return SentenceTransformer(str(model_path), local_files_only=True)


class EmbeddingService:
    """BGE-M3 1024-dimensional embeddings from a verified local artifact."""

    def __init__(
        self,
        *,
        config: EmbeddingModelConfig,
        model_loader: Callable[[Path], object] | None = None,
    ) -> None:
        self._config = config
        self._dimension = _DEFAULT_DIMENSION
        self._model_digest = ""
        self._model: object | None = None
        self._encode_fn: Callable[..., object] | None = None
        self._model_loader = model_loader or _default_model_loader
        self._inference_executor = ThreadPoolExecutor(
            max_workers=_INFERENCE_MAX_WORKERS,
            thread_name_prefix="documind-embedding",
        )
        self._closed = False

    def load(self) -> None:
        """Validate the artifact, then load and validate its native dimension."""
        actual_digest = compute_artifact_digest(self._config.model_path)
        if actual_digest != self._config.expected_digest:
            raise EmbeddingIntegrityError(
                "Model digest mismatch: "
                f"expected {self._config.expected_digest}, got {actual_digest}"
            )

        logger.info("Loading verified embedding model from %s …", self._config.model_path)
        model = self._model_loader(self._config.model_path)
        get_dimension = getattr(model, "get_sentence_embedding_dimension", None)
        native_dimension = get_dimension() if callable(get_dimension) else None
        if native_dimension != _DEFAULT_DIMENSION:
            raise EmbeddingIntegrityError(
                "Model native embedding dimension must be "
                f"{_DEFAULT_DIMENSION}, got {native_dimension!r}"
            )
        encode_fn = getattr(model, "encode", None)
        if not callable(encode_fn):
            raise EmbeddingServiceError("Loaded embedding model does not provide a callable encode method")

        # State is exposed only after every identity and compatibility check succeeds.
        self._model = model
        self._encode_fn = encode_fn
        self._model_digest = actual_digest
        logger.info(
            "Embedding model loaded: dim=%d digest=%s",
            self._dimension,
            self._model_digest[:16] + "…",
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Produce one 1024-dimensional dense vector for every input text."""
        if not texts:
            return []
        if self._encode_fn is None:
            raise EmbeddingServiceError("Model not loaded; call load() first.")

        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._inference_executor, self._encode_sync, texts)
        except EmbeddingServiceError:
            raise
        except Exception as exc:
            raise EmbeddingServiceError(f"Embedding inference failed: {exc}") from exc

    def close(self) -> None:
        """Release the service-owned inference executor exactly once."""
        if self._closed:
            return
        self._closed = True
        self._inference_executor.shutdown(wait=True, cancel_futures=True)

    def _encode_sync(self, texts: list[str]) -> list[list[float]]:
        """Synchronously encode bounded batches with the verified model."""
        if self._encode_fn is None:
            raise EmbeddingServiceError("Model not loaded; call load() first.")

        all_vectors: list[list[float]] = []
        for start in range(0, len(texts), self._config.max_batch_size):
            batch = texts[start : start + self._config.max_batch_size]
            embeddings = self._encode_fn(
                batch,
                normalize_embeddings=self._config.normalize,
                show_progress_bar=False,
            )
            for vector in embeddings:  # type: ignore[union-attr]
                all_vectors.append(vector.tolist() if hasattr(vector, "tolist") else list(vector))
        return all_vectors

    def embed_sync(self, sentences: list[str]) -> list[Sequence[float]]:
        """Synchronous compatibility method for ``SentenceEmbedder`` callers."""
        return self._encode_sync(sentences)

    @property
    def dimension(self) -> int:
        """Embedding vector dimensionality (1024 for BGE-M3)."""
        return self._dimension

    @property
    def model_digest(self) -> str:
        """Digest of the model artifact last successfully loaded."""
        return self._model_digest

    def verify_digest(self) -> None:
        """Re-verify that the configured artifact still matches its pinned digest."""
        actual_digest = compute_artifact_digest(self._config.model_path)
        if actual_digest != self._config.expected_digest:
            raise EmbeddingIntegrityError(
                "Model digest mismatch: "
                f"expected {self._config.expected_digest}, got {actual_digest}"
            )
        if self._model_digest and actual_digest != self._model_digest:
            raise EmbeddingIntegrityError(
                "Loaded model digest changed after startup: "
                f"expected {self._model_digest}, got {actual_digest}"
            )
