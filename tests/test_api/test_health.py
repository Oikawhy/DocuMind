"""Health endpoint contract tests."""

from httpx import AsyncClient


async def test_health_returns_release_status(client: AsyncClient) -> None:
    """The unauthenticated health endpoint reports initial release status."""
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "DocuMind",
        "migration_level": "unavailable",
        "release_digest": "unavailable",
    }
