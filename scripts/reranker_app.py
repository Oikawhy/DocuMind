"""BGE-reranker-v2-m3 sidecar service.

Loads the cross-encoder model on startup with optional digest verification.
Exposes ``POST /predict`` for query/passage scoring and ``GET /health``
for liveness/readiness probes.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("reranker_sidecar")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL_NAME = os.environ.get("RERANKER_MODEL_NAME", "BAAI/bge-reranker-v2-m3")
EXPECTED_DIGEST = os.environ.get("RERANKER_MODEL_DIGEST", "")
MAX_PASSAGES = int(os.environ.get("RERANKER_MAX_PASSAGES", "200"))

# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

_model = None
_model_digest: str = ""


def _compute_model_digest(model_path: str) -> str:
    """Compute SHA256 digest over the model's ``model.safetensors`` file."""
    safetensors = Path(model_path) / "model.safetensors"
    if not safetensors.exists():
        # Fall back to pytorch_model.bin
        safetensors = Path(model_path) / "pytorch_model.bin"
    if not safetensors.exists():
        return "unknown"
    sha = hashlib.sha256()
    with open(safetensors, "rb") as f:
        while chunk := f.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


def load_model() -> None:
    """Load the CrossEncoder model and verify its digest."""
    global _model, _model_digest  # noqa: PLW0603
    from sentence_transformers import CrossEncoder

    logger.info("Loading model: %s", MODEL_NAME)
    _model = CrossEncoder(MODEL_NAME)

    # Compute digest for verification
    model_path = _model.model.config._name_or_path  # type: ignore[union-attr]
    _model_digest = _compute_model_digest(model_path)
    logger.info("Model loaded, digest: %s", _model_digest[:16])

    if EXPECTED_DIGEST and _model_digest != EXPECTED_DIGEST:
        msg = (
            f"Model digest mismatch: expected {EXPECTED_DIGEST[:16]}..., "
            f"got {_model_digest[:16]}..."
        )
        logger.error(msg)
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------


class PredictRequest(BaseModel):
    """Query/passage scoring request."""

    query: str = Field(..., min_length=1)
    passages: list[str] = Field(..., min_length=1)


class PredictResponse(BaseModel):
    """Relevance scores for each passage."""

    scores: list[float]


class HealthResponse(BaseModel):
    """Sidecar health and model identity."""

    status: str
    model_name: str
    model_digest: str


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="BGE Reranker Sidecar", version="1.0.0")


@app.on_event("startup")
async def startup() -> None:
    """Load the cross-encoder model on startup."""
    load_model()


@app.post("/predict", response_model=PredictResponse)
async def predict(body: PredictRequest) -> PredictResponse:
    """Score query/passage pairs using the BGE cross-encoder."""
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if len(body.passages) > MAX_PASSAGES:
        raise HTTPException(
            status_code=422,
            detail=f"Too many passages: {len(body.passages)} > {MAX_PASSAGES}",
        )

    pairs = [[body.query, passage] for passage in body.passages]
    scores = _model.predict(pairs)
    return PredictResponse(scores=[float(s) for s in scores])


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Return model identity and digest for observability."""
    return HealthResponse(
        status="ok" if _model is not None else "not_ready",
        model_name=MODEL_NAME,
        model_digest=_model_digest,
    )
