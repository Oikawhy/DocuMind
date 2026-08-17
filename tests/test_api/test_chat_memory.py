"""Tests for _load_session_messages token budget enforcement (T9-04)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from documind.api.chat import _load_session_messages
from documind.models.chat import ChatMessage


def _make_msg(role: str, content: str, idx: int) -> MagicMock:
    msg = MagicMock(spec=ChatMessage)
    msg.role = role
    msg.content = content
    msg.token_count = len(content.split())
    msg.created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=idx)
    return msg


@pytest.fixture
def session_id() -> uuid.UUID:
    return uuid.uuid4()


async def test_summary_alone_exceeds_budget_is_truncated(session_id: uuid.UUID) -> None:
    """When the compaction summary exceeds max_tokens, it should be truncated."""
    long_summary = " ".join(["word"] * 5000)  # 5000 tokens
    summary_msg = _make_msg("assistant", f"[COMPACTION_SUMMARY] {long_summary}", 0)
    user_msg = _make_msg("user", "hello", 1)

    # Mock the async session — newest-first ordering
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [user_msg, summary_msg]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    messages, summary = await _load_session_messages(
        mock_session, session_id, window=20, max_tokens=100,
    )
    # Summary must be truncated to fit within max_tokens
    assert summary is not None
    assert len(summary.split()) <= 100
    # No room left for messages
    assert messages == []


async def test_summary_reduces_budget_for_messages(session_id: uuid.UUID) -> None:
    """Summary token count should reduce the budget available for messages."""
    summary_content = " ".join(["context"] * 50)  # 50 tokens
    summary_msg = _make_msg("assistant", f"[COMPACTION_SUMMARY] {summary_content}", 0)
    # 10 messages of 10 tokens each = 100 tokens total
    user_msgs = [_make_msg("user", " ".join(["hi"] * 10), i + 1) for i in range(10)]

    all_msgs = list(reversed(user_msgs)) + [summary_msg]  # newest-first
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = all_msgs
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    messages, summary = await _load_session_messages(
        mock_session, session_id, window=20, max_tokens=100,
    )
    # Budget is 100. Summary uses 50. Only 5 messages (50 tokens) should fit.
    assert len(messages) <= 5
    assert summary is not None


async def test_no_summary_full_budget_for_messages(session_id: uuid.UUID) -> None:
    """Without a summary, the full budget is available for messages."""
    # 10 messages of 10 tokens each = 100 tokens total
    user_msgs = [_make_msg("user", " ".join(["hi"] * 10), i) for i in range(10)]

    all_msgs = list(reversed(user_msgs))  # newest-first
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = all_msgs
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    messages, summary = await _load_session_messages(
        mock_session, session_id, window=20, max_tokens=100,
    )
    # All 10 messages = 100 tokens, exactly at budget
    assert len(messages) == 10
    assert summary is None
