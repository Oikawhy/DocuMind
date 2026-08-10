"""Embedding service tests using local artifacts and injected model loaders."""

from __future__ import annotations

from pathlib import Path

import pytest

from documind.services import embedding_service
from documind.services.embedding_service import (
    EmbeddingIntegrityError,
    EmbeddingModelConfig,
    EmbeddingService,
    EmbeddingServiceError,
)

_VALID_DIGEST = "sha256:" + "0" * 64


class StubEncoder:
    """Mimics ``SentenceTransformer.encode`` output."""

    def __init__(self, dimension: int = 1024) -> None:
        self._dimension = dimension

    def __call__(
        self,
        texts: list[str],
        normalize_embeddings: bool = True,
        show_progress_bar: bool = False,
    ) -> list[list[float]]:
        return [
            [float(index + component) / max(1, len(text)) for component in range(self._dimension)]
            for index, text in enumerate(texts)
        ]


class StubSentenceTransformer:
    """Minimal local model double with a native dimension declaration."""

    def __init__(self, dimension: int | None = 1024) -> None:
        self._dimension = dimension
        self.encode = StubEncoder(dimension or 1024)

    def get_sentence_embedding_dimension(self) -> int | None:
        return self._dimension


def _write_artifact(root: Path) -> Path:
    root.mkdir()
    (root / "config.json").write_text('{"model_type":"bge-m3"}', encoding="utf-8")
    (root / "weights.bin").write_bytes(b"deterministic test weights")
    return root


def _artifact_digest(root: Path) -> str:
    """Call the public artifact digest boundary once it has been implemented."""
    return embedding_service.compute_artifact_digest(root)  # type: ignore[attr-defined]


def _config(
    artifact: Path,
    *,
    expected_digest: str | None = None,
    max_batch_size: int = 32,
) -> EmbeddingModelConfig:
    return EmbeddingModelConfig(
        model_path=artifact,
        expected_digest=expected_digest or _artifact_digest(artifact),
        max_batch_size=max_batch_size,
    )


def _make_loaded_service(
    artifact: Path,
    *,
    dimension: int = 1024,
    max_batch_size: int = 32,
) -> tuple[EmbeddingService, list[Path]]:
    calls: list[Path] = []
    model = StubSentenceTransformer(dimension)

    def loader(model_path: Path) -> StubSentenceTransformer:
        calls.append(model_path)
        return model

    service = EmbeddingService(
        config=_config(artifact, max_batch_size=max_batch_size),
        model_loader=loader,
    )
    service.load()
    return service, calls


# ---------------------------------------------------------------------------
# Config and artifact identity
# ---------------------------------------------------------------------------


def test_config_rejects_relative_model_path(tmp_path: Path) -> None:
    """A symbolic or relative model reference cannot identify a local artifact."""
    with pytest.raises(ValueError, match="absolute"):
        EmbeddingModelConfig(model_path=Path("models/bge-m3"), expected_digest=_VALID_DIGEST)


def test_config_rejects_missing_model_directory(tmp_path: Path) -> None:
    """The pinned local artifact must exist when configuration is created."""
    with pytest.raises(ValueError, match="existing directory"):
        EmbeddingModelConfig(model_path=tmp_path / "missing", expected_digest=_VALID_DIGEST)


def test_config_rejects_root_symlink(tmp_path: Path) -> None:
    """A deployment artifact root must be a real directory, never a link."""
    artifact = _write_artifact(tmp_path / "artifact")
    symlink = tmp_path / "artifact-link"
    symlink.symlink_to(artifact, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        EmbeddingModelConfig(model_path=symlink, expected_digest=_artifact_digest(artifact))


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "0" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "0" * 63,
    ],
)
def test_config_rejects_malformed_digest(tmp_path: Path, digest: str) -> None:
    """Artifact pinning only accepts a complete lowercase SHA-256 identity."""
    artifact = _write_artifact(tmp_path / "artifact")
    with pytest.raises(ValueError, match="expected_digest"):
        EmbeddingModelConfig(model_path=artifact, expected_digest=digest)


def test_config_rejects_non_contract_dimension(tmp_path: Path) -> None:
    """BGE-M3 contract is fixed at 1024 dimensions."""
    artifact = _write_artifact(tmp_path / "artifact")
    with pytest.raises(ValueError, match="1024"):
        EmbeddingModelConfig(
            model_path=artifact,
            expected_digest=_VALID_DIGEST,
            dimension=768,
        )


def test_config_rejects_non_positive_batch_size(tmp_path: Path) -> None:
    """Batching cannot make forward progress with zero items per batch."""
    artifact = _write_artifact(tmp_path / "artifact")
    with pytest.raises(ValueError, match="max_batch_size"):
        EmbeddingModelConfig(
            model_path=artifact,
            expected_digest=_VALID_DIGEST,
            max_batch_size=0,
        )


def test_artifact_digest_changes_for_paths_and_contents(tmp_path: Path) -> None:
    """The digest commits to every artifact-relative filename and byte."""
    first = _write_artifact(tmp_path / "first")
    second = _write_artifact(tmp_path / "second")
    (second / "weights.bin").rename(second / "renamed-weights.bin")

    first_digest = _artifact_digest(first)
    assert _artifact_digest(second) != first_digest

    (first / "weights.bin").write_bytes(b"changed deterministic test weights")
    assert _artifact_digest(first) != first_digest


def test_artifact_digest_rejects_symlinked_files(tmp_path: Path) -> None:
    """Artifact pinning refuses links that could change identity after validation."""
    artifact = _write_artifact(tmp_path / "artifact")
    target = tmp_path / "target.bin"
    target.write_bytes(b"outside artifact")
    (artifact / "linked.bin").symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        _artifact_digest(artifact)


def test_artifact_digest_rejects_root_symlink(tmp_path: Path) -> None:
    """The direct digest boundary rejects a symlinked artifact root too."""
    artifact = _write_artifact(tmp_path / "artifact")
    symlink = tmp_path / "artifact-link"
    symlink.symlink_to(artifact, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        _artifact_digest(symlink)


def test_artifact_digest_rejects_empty_tree(tmp_path: Path) -> None:
    """A model identity cannot be derived from an empty artifact directory."""
    artifact = tmp_path / "empty-artifact"
    artifact.mkdir()

    with pytest.raises(ValueError, match="no regular files"):
        _artifact_digest(artifact)


def test_artifact_digest_rejects_file_path(tmp_path: Path) -> None:
    """A local model artifact boundary accepts directories only."""
    artifact_file = tmp_path / "artifact.bin"
    artifact_file.write_bytes(b"not a directory")

    with pytest.raises(ValueError, match="existing directory"):
        _artifact_digest(artifact_file)


# ---------------------------------------------------------------------------
# Loading and inference
# ---------------------------------------------------------------------------


def test_load_rejects_digest_mismatch_before_invoking_loader(tmp_path: Path) -> None:
    """No unverified artifact may be handed to a model loader."""
    artifact = _write_artifact(tmp_path / "artifact")
    calls: list[Path] = []

    def loader(model_path: Path) -> StubSentenceTransformer:
        calls.append(model_path)
        return StubSentenceTransformer()

    service = EmbeddingService(
        config=_config(artifact, expected_digest=_VALID_DIGEST),
        model_loader=loader,
    )

    with pytest.raises(EmbeddingIntegrityError, match="mismatch"):
        service.load()
    assert calls == []


def test_load_accepts_matching_local_artifact(tmp_path: Path) -> None:
    """A digest-matched 1024-dimensional local model becomes usable."""
    artifact = _write_artifact(tmp_path / "artifact")
    service, calls = _make_loaded_service(artifact)

    assert calls == [artifact]
    assert service.dimension == 1024
    assert service.model_digest == _artifact_digest(artifact)


@pytest.mark.parametrize("dimension", [None, 768])
def test_load_rejects_model_with_non_contract_native_dimension(
    tmp_path: Path,
    dimension: int | None,
) -> None:
    """A loader cannot alter BGE-M3 output dimensionality."""
    artifact = _write_artifact(tmp_path / "artifact")
    calls: list[Path] = []

    def loader(model_path: Path) -> StubSentenceTransformer:
        calls.append(model_path)
        return StubSentenceTransformer(dimension)

    service = EmbeddingService(config=_config(artifact), model_loader=loader)
    with pytest.raises(EmbeddingIntegrityError, match="1024"):
        service.load()

    assert calls == [artifact]
    with pytest.raises(EmbeddingServiceError, match="not loaded"):
        service.embed_sync(["cannot use invalid model"])


@pytest.mark.asyncio
async def test_embed_returns_correct_dimension_and_preserves_order(tmp_path: Path) -> None:
    """The loaded service returns one 1024-dimensional vector per input."""
    artifact = _write_artifact(tmp_path / "artifact")
    service, _ = _make_loaded_service(artifact)

    texts = ["alpha", "beta", "gamma"]
    result = await service.embed(texts)

    assert len(result) == len(texts)
    assert all(len(vector) == 1024 for vector in result)
    assert result[0] != result[1]
    assert result[1] != result[2]


@pytest.mark.asyncio
async def test_embed_does_not_use_the_event_loop_default_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inference is isolated from the loop-owned default executor lifecycle."""
    artifact = _write_artifact(tmp_path / "artifact")
    service, _ = _make_loaded_service(artifact)

    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("embed must not dispatch through asyncio.to_thread")

    monkeypatch.setattr(embedding_service.asyncio, "to_thread", fail_if_called)
    try:
        result = await service.embed(["alpha"])
    finally:
        service.close()

    assert len(result) == 1
    assert len(result[0]) == 1024


@pytest.mark.asyncio
async def test_close_is_idempotent_and_prevents_future_inference(tmp_path: Path) -> None:
    """Service-owned inference resources have a safe explicit shutdown boundary."""
    artifact = _write_artifact(tmp_path / "artifact")
    service, _ = _make_loaded_service(artifact)

    service.close()
    service.close()

    with pytest.raises(EmbeddingServiceError, match="Embedding inference failed"):
        await service.embed(["alpha"])


@pytest.mark.asyncio
async def test_embed_empty_input_needs_no_model_call(tmp_path: Path) -> None:
    """Embedding an empty batch remains a cheap no-op after service creation."""
    artifact = _write_artifact(tmp_path / "artifact")
    service = EmbeddingService(config=_config(artifact), model_loader=lambda _: StubSentenceTransformer())

    assert await service.embed([]) == []


@pytest.mark.asyncio
async def test_embed_raises_when_not_loaded(tmp_path: Path) -> None:
    """Loading is explicit and cannot be bypassed by an injected loader."""
    artifact = _write_artifact(tmp_path / "artifact")
    service = EmbeddingService(config=_config(artifact), model_loader=lambda _: StubSentenceTransformer())

    with pytest.raises(EmbeddingServiceError, match="not loaded"):
        await service.embed(["test"])


def test_embed_sync_supports_sentence_embedder_and_batches(tmp_path: Path) -> None:
    """The synchronous protocol path shares the public loaded service state."""
    artifact = _write_artifact(tmp_path / "artifact")
    service, _ = _make_loaded_service(artifact, max_batch_size=2)

    result = service.embed_sync(["a", "b", "c", "d", "e"])

    assert len(result) == 5
    assert all(len(vector) == 1024 for vector in result)
