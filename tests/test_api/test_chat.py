"""HTTP contracts for Task 9 chat endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from documind.main import app
from documind.services.identity_service import Principal


def _principal() -> Principal:
    return Principal(
        subject="chatter@example.test",
        display_name="Chatter",
        email=None,
        groups=["users"],
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


def _mock_settings(**overrides: Any) -> MagicMock:
    defaults = {
        "chat_enabled": True,
        "chat_retention_days": 30,
        "chat_history_window": 20,
        "chat_history_max_tokens": 4096,
        "chat_compaction_threshold": 30,
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------------------
# POST /v1/chat — disabled
# ---------------------------------------------------------------------------


async def test_post_chat_returns_403_when_disabled(client: AsyncClient) -> None:
    """Chat disabled by default → 403 CHAT_DISABLED."""
    identity = _mock_identity()
    mock_settings = _mock_settings(chat_enabled=False)
    original_identity = app.state.identity_service
    original_settings = app.state.settings
    try:
        app.state.identity_service = identity
        app.state.settings = mock_settings
        response = await client.post(
            "/v1/chat",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == "CHAT_DISABLED"
    finally:
        app.state.identity_service = original_identity
        app.state.settings = original_settings


# ---------------------------------------------------------------------------
# POST /v1/chat — enabled, no RAG service
# ---------------------------------------------------------------------------


async def test_post_chat_creates_session_and_abstains_without_rag(client: AsyncClient) -> None:
    """With chat enabled but no RAG service wired, should abstain gracefully."""
    identity = _mock_identity()
    mock_settings = _mock_settings(chat_enabled=True)

    # Build a mock async session that supports `async with sf() as session, session.begin():`.
    mock_begin_ctx = MagicMock()
    mock_begin_ctx.__aenter__ = AsyncMock(return_value=None)
    mock_begin_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=mock_begin_ctx)
    mock_session.execute = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()

    # execute returns for ChatMessage operations.
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalar_one.return_value = 0
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    mock_factory = MagicMock(return_value=mock_session)

    original_identity = app.state.identity_service
    original_settings = app.state.settings
    original_sf = app.state.session_factory
    original_audit = app.state.audit_service
    original_rag = getattr(app.state, "rag_service", None)
    try:
        app.state.identity_service = identity
        app.state.settings = mock_settings
        app.state.session_factory = mock_factory
        app.state.audit_service = AsyncMock()
        app.state.audit_service.write_event = AsyncMock(return_value=uuid.uuid4())
        app.state.rag_service = None

        response = await client.post(
            "/v1/chat",
            json={"message": "What is in document X?"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["abstained"] is True
        assert "session_id" in body
        assert "message_id" in body
        assert "trace_id" in body
    finally:
        app.state.identity_service = original_identity
        app.state.settings = original_settings
        app.state.session_factory = original_sf
        app.state.audit_service = original_audit
        app.state.rag_service = original_rag


# ---------------------------------------------------------------------------
# POST /v1/chat — no auth
# ---------------------------------------------------------------------------


async def test_post_chat_returns_401_without_auth(client: AsyncClient) -> None:
    """Missing Authorization header → 401."""
    response = await client.post(
        "/v1/chat",
        json={"message": "Hello"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/chat/sessions — no auth
# ---------------------------------------------------------------------------


async def test_list_sessions_returns_401_without_auth(client: AsyncClient) -> None:
    response = await client.get("/v1/chat/sessions")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /v1/chat/messages/{id}/feedback — validation
# ---------------------------------------------------------------------------


async def test_feedback_rejects_invalid_score(client: AsyncClient) -> None:
    """Score outside [-1, 1] → 422."""
    identity = _mock_identity()
    original_identity = app.state.identity_service
    try:
        app.state.identity_service = identity
        msg_id = uuid.uuid4()
        response = await client.post(
            f"/v1/chat/messages/{msg_id}/feedback",
            json={"score": 5},
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 422
    finally:
        app.state.identity_service = original_identity


async def test_feedback_rejects_long_comment(client: AsyncClient) -> None:
    """Comment over 1024 chars → 422."""
    identity = _mock_identity()
    original_identity = app.state.identity_service
    try:
        app.state.identity_service = identity
        msg_id = uuid.uuid4()
        response = await client.post(
            f"/v1/chat/messages/{msg_id}/feedback",
            json={"score": 1, "comment": "x" * 1025},
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 422
    finally:
        app.state.identity_service = original_identity
