"""HTTP contracts for Task 9 webhook endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from documind.domain.errors import SSRFViolationError
from documind.main import app
from documind.services.identity_service import Principal


def _principal() -> Principal:
    return Principal(
        subject="admin@example.test",
        display_name="Admin",
        email=None,
        groups=["admins"],
        active=True,
        issuer="https://issuer.example.test",
    )


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _mock_identity() -> AsyncMock:
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    return identity


# ---------------------------------------------------------------------------
# POST /v1/webhooks — success
# ---------------------------------------------------------------------------


async def test_register_webhook_returns_201(client: AsyncClient) -> None:
    """Successful webhook registration returns 201 with no secret leak."""
    webhook_id = uuid.uuid4()
    now = datetime.now(UTC)

    mock_webhook = MagicMock()
    mock_webhook.id = webhook_id
    mock_webhook.target_url = "https://hooks.example.com/receive"
    mock_webhook.event_type_glob = "document.version.*"
    mock_webhook.active = True
    mock_webhook.failure_streak = 0
    mock_webhook.created_at = now

    service = AsyncMock()
    service.register_webhook.return_value = mock_webhook
    identity = _mock_identity()

    original_identity = app.state.identity_service
    original_webhook = app.state.webhook_service
    try:
        app.state.identity_service = identity
        app.state.webhook_service = service
        response = await client.post(
            "/v1/webhooks",
            json={
                "target_url": "https://hooks.example.com/receive",
                "event_type_glob": "document.version.*",
                "secret": "a" * 32,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["id"] == str(webhook_id)
        assert body["active"] is True
        # Secret must never be returned.
        assert "secret" not in body
        assert "secret_hash" not in body
    finally:
        app.state.identity_service = original_identity
        app.state.webhook_service = original_webhook


# ---------------------------------------------------------------------------
# POST /v1/webhooks — SSRF rejection
# ---------------------------------------------------------------------------


async def test_register_webhook_rejects_ssrf(client: AsyncClient) -> None:
    """Webhook with private/loopback target → 400 SSRF_VIOLATION."""
    service = AsyncMock()
    service.register_webhook.side_effect = SSRFViolationError("Loopback addresses are not permitted.")
    identity = _mock_identity()

    original_identity = app.state.identity_service
    original_webhook = app.state.webhook_service
    try:
        app.state.identity_service = identity
        app.state.webhook_service = service
        response = await client.post(
            "/v1/webhooks",
            json={
                "target_url": "https://127.0.0.1/hook",
                "event_type_glob": "*",
                "secret": "b" * 32,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "SSRF_VIOLATION"
    finally:
        app.state.identity_service = original_identity
        app.state.webhook_service = original_webhook


# ---------------------------------------------------------------------------
# POST /v1/webhooks — HTTP URL rejected at schema level
# ---------------------------------------------------------------------------


async def test_register_webhook_rejects_http_url(client: AsyncClient) -> None:
    """Non-HTTPS URL → 422 from Pydantic validation."""
    identity = _mock_identity()
    original_identity = app.state.identity_service
    try:
        app.state.identity_service = identity
        response = await client.post(
            "/v1/webhooks",
            json={
                "target_url": "http://hooks.example.com/receive",
                "event_type_glob": "*",
                "secret": "c" * 32,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 422
    finally:
        app.state.identity_service = original_identity


# ---------------------------------------------------------------------------
# POST /v1/webhooks — secret too short
# ---------------------------------------------------------------------------


async def test_register_webhook_rejects_short_secret(client: AsyncClient) -> None:
    """Secret under 32 chars → 422."""
    identity = _mock_identity()
    original_identity = app.state.identity_service
    try:
        app.state.identity_service = identity
        response = await client.post(
            "/v1/webhooks",
            json={
                "target_url": "https://hooks.example.com/receive",
                "event_type_glob": "*",
                "secret": "short",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 422
    finally:
        app.state.identity_service = original_identity


# ---------------------------------------------------------------------------
# DELETE /v1/webhooks/{id} — not found
# ---------------------------------------------------------------------------


async def test_deactivate_webhook_returns_404_when_missing(client: AsyncClient) -> None:
    service = AsyncMock()
    service.deactivate_webhook.return_value = False
    identity = _mock_identity()

    original_identity = app.state.identity_service
    original_webhook = app.state.webhook_service
    try:
        app.state.identity_service = identity
        app.state.webhook_service = service
        response = await client.delete(
            f"/v1/webhooks/{uuid.uuid4()}",
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 404
    finally:
        app.state.identity_service = original_identity
        app.state.webhook_service = original_webhook


# ---------------------------------------------------------------------------
# No auth
# ---------------------------------------------------------------------------


async def test_webhooks_require_auth(client: AsyncClient) -> None:
    response = await client.post("/v1/webhooks", json={})
    assert response.status_code == 401
