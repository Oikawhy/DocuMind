"""Tests for the BGE cross-encoder HTTP adapter."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from documind.services.bge_adapter import BGECrossEncoderAdapter
from documind.services.reranker_service import RerankerUnavailableError


class TestBGECrossEncoderAdapter:
    """Unit tests for BGECrossEncoderAdapter."""

    @pytest.mark.asyncio
    async def test_predict_success(self) -> None:
        adapter = BGECrossEncoderAdapter(
            sidecar_url="http://localhost:8501",
            timeout_ms=1000,
        )
        # Mock the httpx client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"scores": [0.9, 0.1, 0.5]}

        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        scores = await adapter.predict("query", ["p1", "p2", "p3"])
        assert scores == [0.9, 0.1, 0.5]
        adapter._client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_predict_timeout_raises_unavailable(self) -> None:
        adapter = BGECrossEncoderAdapter(
            sidecar_url="http://localhost:8501",
            timeout_ms=1,  # Very short timeout
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(side_effect=TimeoutError())

        with pytest.raises(RerankerUnavailableError, match="timed out"):
            await adapter.predict("query", ["passage"])

    @pytest.mark.asyncio
    async def test_predict_http_error_raises_unavailable(self) -> None:
        import httpx

        adapter = BGECrossEncoderAdapter(
            sidecar_url="http://localhost:8501",
            timeout_ms=1000,
        )
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Service Unavailable",
            request=MagicMock(),
            response=mock_response,
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(RerankerUnavailableError, match="HTTP 503"):
            await adapter.predict("query", ["passage"])

    @pytest.mark.asyncio
    async def test_predict_malformed_response_raises_unavailable(self) -> None:
        adapter = BGECrossEncoderAdapter(
            sidecar_url="http://localhost:8501",
            timeout_ms=1000,
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"unexpected": "data"}

        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(RerankerUnavailableError, match="unexpected payload"):
            await adapter.predict("query", ["passage"])

    @pytest.mark.asyncio
    async def test_predict_connection_error_raises_unavailable(self) -> None:
        adapter = BGECrossEncoderAdapter(
            sidecar_url="http://localhost:8501",
            timeout_ms=1000,
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(side_effect=ConnectionError("refused"))

        with pytest.raises(RerankerUnavailableError, match="Reranker sidecar error"):
            await adapter.predict("query", ["passage"])

    @pytest.mark.asyncio
    async def test_close_closes_client(self) -> None:
        adapter = BGECrossEncoderAdapter(
            sidecar_url="http://localhost:8501",
            timeout_ms=1000,
        )
        adapter._client = AsyncMock()
        await adapter.close()
        adapter._client.aclose.assert_awaited_once()

    def test_url_construction(self) -> None:
        adapter = BGECrossEncoderAdapter(sidecar_url="http://reranker:8501")
        assert adapter._url == "http://reranker:8501/predict"

    def test_url_strips_trailing_slash(self) -> None:
        adapter = BGECrossEncoderAdapter(sidecar_url="http://reranker:8501/")
        assert adapter._url == "http://reranker:8501/predict"

    def test_timeout_conversion(self) -> None:
        adapter = BGECrossEncoderAdapter(timeout_ms=500)
        assert adapter._timeout_s == 0.5
