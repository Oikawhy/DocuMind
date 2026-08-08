"""Tests for the /v1/identity/me endpoint and OIDC middleware."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from documind.domain.policy_service import RoleMapping
from documind.main import app
from documind.services.identity_service import Principal

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_principal(**overrides: object) -> Principal:
    defaults = {
        "subject": "user@example.com",
        "display_name": "Test User",
        "email": "user@example.com",
        "groups": ["editors"],
        "active": True,
        "issuer": "https://idp.example.com",
    }
    defaults.update(overrides)
    return Principal(**defaults)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthExempt:
    """Health endpoint must be accessible without authentication."""

    async def test_health_no_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "DocuMind"


class TestOIDCMiddleware:
    """OIDC middleware fail-closed behavior."""

    async def test_missing_token_returns_401(self, client: AsyncClient) -> None:
        resp = await client.get("/v1/identity/me")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"

    async def test_empty_bearer_returns_401(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/v1/identity/me",
            headers={"Authorization": "Bearer "},
        )
        assert resp.status_code == 401

    async def test_invalid_scheme_returns_401(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/v1/identity/me",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 401

    async def test_valid_token_passes_through(self, client: AsyncClient) -> None:
        """When IdentityService validates the token, the request succeeds."""
        principal = _make_principal()
        mock_identity = AsyncMock()
        mock_identity.validate_oidc_token.return_value = principal

        mock_policy = AsyncMock()
        mock_policy.get_role_mappings.return_value = [
            RoleMapping(
                role_key="editor",
                allowed_label_ids=set(),
                permitted_actions={"read"},
            ),
        ]
        mock_policy.get_active_policy.return_value = None

        original_identity = app.state.identity_service
        original_policy = app.state.policy_service
        try:
            app.state.identity_service = mock_identity
            app.state.policy_service = mock_policy

            resp = await client.get(
                "/v1/identity/me",
                headers={"Authorization": "Bearer valid-test-token"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["subject"] == "user@example.com"
            assert "editor" in data["effective_roles"]
        finally:
            app.state.identity_service = original_identity
            app.state.policy_service = original_policy

    async def test_expired_token_returns_401(self, client: AsyncClient) -> None:
        """When IdentityService rejects the token, middleware returns 401."""
        from documind.domain.errors import AuthenticationError

        mock_identity = AsyncMock()
        mock_identity.validate_oidc_token.side_effect = AuthenticationError(
            "Token has expired.",
            code="TOKEN_INVALID",
        )

        original = app.state.identity_service
        try:
            app.state.identity_service = mock_identity
            resp = await client.get(
                "/v1/identity/me",
                headers={"Authorization": "Bearer expired-token"},
            )
            assert resp.status_code == 401
            assert resp.json()["error"]["code"] == "TOKEN_INVALID"
        finally:
            app.state.identity_service = original
