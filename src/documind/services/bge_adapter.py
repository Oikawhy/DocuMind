"""HTTP adapter for the BGE-reranker-v2-m3 sidecar per §6.

Implements :class:`CrossEncoderAdapter` by calling the reranker sidecar's
``POST /predict`` endpoint.  Enforces the configured timeout (default
1 000 ms) and converts any failure into :class:`RerankerUnavailableError`.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

from documind.services.reranker_service import CrossEncoderAdapter, RerankerUnavailableError

logger = structlog.get_logger(__name__)


class BGECrossEncoderAdapter:
    """HTTP cross-encoder adapter targeting the BGE sidecar.

    Satisfies :class:`CrossEncoderAdapter` protocol.

    Parameters
    ----------
    sidecar_url:
        Base URL of the reranker sidecar (e.g. ``http://reranker:8501``).
    timeout_ms:
        Maximum time to wait for a prediction response.  The §6 spec
        requires a 1 000 ms reranker deadline.
    """

    def __init__(
        self,
        *,
        sidecar_url: str = "http://localhost:8501",
        timeout_ms: int = 1000,
    ) -> None:
        self._url = sidecar_url.rstrip("/") + "/predict"
        self._timeout_s = timeout_ms / 1000.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_s, connect=5.0),
        )

    async def predict(self, query: str, passages: list[str]) -> list[float]:
        """Score query/passage pairs via the BGE sidecar.

        Raises
        ------
        RerankerUnavailableError
            On HTTP error, timeout, or malformed response.
        """
        try:
            response = await asyncio.wait_for(
                self._client.post(
                    self._url,
                    json={"query": query, "passages": passages},
                ),
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            data = response.json()
            scores = data.get("scores")
            if not isinstance(scores, list):
                raise RerankerUnavailableError(
                    f"Sidecar returned unexpected payload: {data!r}"
                )
            return [float(s) for s in scores]
        except RerankerUnavailableError:
            raise
        except TimeoutError:
            await logger.awarning("bge_adapter_timeout", timeout_ms=self._timeout_s * 1000)
            raise RerankerUnavailableError("Reranker sidecar timed out") from None
        except httpx.HTTPStatusError as exc:
            raise RerankerUnavailableError(
                f"Reranker sidecar HTTP {exc.response.status_code}"
            ) from exc
        except Exception as exc:
            raise RerankerUnavailableError(f"Reranker sidecar error: {exc}") from exc

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
