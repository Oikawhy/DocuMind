"""Shared test fixtures for DocuMind."""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from documind.main import app


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Provide an in-process HTTP client for the API."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
