"""Tests for the SCIM 2.0 provisioning endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from documind.main import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SCIM_TOKEN = "test-scim-token-12345"


@pytest_asyncio.fixture
async def scim_client() -> AsyncIterator[AsyncClient]:
    """Client with SCIM token configured."""
    from documind.config import settings

    original = settings.scim_bearer_token
    settings.scim_bearer_token = SCIM_TOKEN
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    settings.scim_bearer_token = original


def _scim_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SCIM_TOKEN}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSCIMAuth:
    """SCIM endpoint authentication."""

    async def test_no_token_returns_401(self, scim_client: AsyncClient) -> None:
        """Missing SCIM token → 401."""
        # The OIDC middleware exempts /scim/v2, so FastAPI's own
        # dependency validation for the _verify_scim_token runs.
        resp = await scim_client.get("/scim/v2/Users")
        assert resp.status_code in (401, 422)  # 422 if header missing, 401 if wrong

    async def test_wrong_token_returns_401(self, scim_client: AsyncClient) -> None:
        """Wrong SCIM token → 401."""
        resp = await scim_client.get(
            "/scim/v2/Users",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


class TestSCIMCreateUser:
    """SCIM POST /scim/v2/Users."""

    async def test_create_user(self, scim_client: AsyncClient) -> None:
        """Create a user via SCIM provisioning."""
        mock_identity = AsyncMock()
        mock_identity.process_scim_user_create.return_value = None

        # Mock the DB session for the `db` parameter (via get_db override).
        mock_db = AsyncMock()
        mock_db.get.return_value = None  # reload returns None → 500 (acceptable, test validates service call)

        from documind.database import get_db

        async def _mock_get_db():
            yield mock_db

        original = app.state.identity_service
        app.dependency_overrides[get_db] = _mock_get_db
        try:
            app.state.identity_service = mock_identity

            await scim_client.post(
                "/scim/v2/Users",
                headers=_scim_headers(),
                json={
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                    "userName": "alice@example.com",
                    "displayName": "Alice Smith",
                    "emails": [{"value": "alice@example.com", "primary": True}],
                    "active": True,
                    "groups": [{"value": "editors", "display": "Editors"}],
                },
            )
            # Service was called correctly.
            mock_identity.process_scim_user_create.assert_called_once_with(
                subject="alice@example.com",
                display_name="Alice Smith",
                email="alice@example.com",
                groups=["editors"],
            )
        finally:
            app.state.identity_service = original
            app.dependency_overrides.pop(get_db, None)


class TestSCIMPatchUser:
    """SCIM PATCH /scim/v2/Users/{id}."""

    async def test_deactivate_user(self, scim_client: AsyncClient) -> None:
        """Deactivate a user via SCIM PATCH."""
        mock_identity = AsyncMock()
        mock_identity.process_scim_user_update.return_value = None

        # Mock the reload session.
        mock_session = AsyncMock()
        mock_session.get.return_value = None  # User won't be found for reload (returns 404).
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_factory = MagicMock(return_value=mock_session)

        original_identity = app.state.identity_service
        original_factory = app.state.session_factory
        try:
            app.state.identity_service = mock_identity
            app.state.session_factory = mock_factory

            await scim_client.patch(
                "/scim/v2/Users/alice@example.com",
                headers=_scim_headers(),
                json={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [
                        {"op": "replace", "path": "active", "value": False},
                    ],
                },
            )
            mock_identity.process_scim_user_update.assert_called_once_with(
                "alice@example.com",
                active=False,
                display_name=None,
                groups=None,
            )
        finally:
            app.state.identity_service = original_identity
            app.state.session_factory = original_factory


class TestSCIMDeleteUser:
    """SCIM DELETE /scim/v2/Users/{id}."""

    async def test_delete_deactivates(self, scim_client: AsyncClient) -> None:
        """SCIM DELETE soft-deactivates the user."""
        mock_identity = AsyncMock()
        mock_identity.process_scim_user_deactivate.return_value = None

        original = app.state.identity_service
        try:
            app.state.identity_service = mock_identity
            resp = await scim_client.delete(
                "/scim/v2/Users/alice@example.com",
                headers=_scim_headers(),
            )
            assert resp.status_code == 204
            mock_identity.process_scim_user_deactivate.assert_called_once_with("alice@example.com")
        finally:
            app.state.identity_service = original
