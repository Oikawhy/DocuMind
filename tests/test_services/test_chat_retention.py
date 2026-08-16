"""Tests for chat session retention cleanup per §7.1."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from documind.workflows.maintenance.chat_retention import cleanup_expired_sessions


def _make_session(
    *,
    subject: str = "user@test",
    expired: bool = True,
    deleted: bool = False,
) -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.subject = subject
    s.retention_expires_at = (
        datetime.now(UTC) - timedelta(days=1) if expired
        else datetime.now(UTC) + timedelta(days=30)
    )
    s.deleted_at = datetime.now(UTC) if deleted else None
    return s


@pytest.mark.asyncio
async def test_cleanup_skips_held_subjects() -> None:
    """Sessions owned by held subjects are not deleted."""
    held_session = _make_session(subject="held@test", expired=True)

    mock_db_session = MagicMock()
    mock_db_session.__aenter__ = AsyncMock(return_value=mock_db_session)
    mock_db_session.__aexit__ = AsyncMock(return_value=False)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [held_session]
    mock_db_session.execute = AsyncMock(return_value=mock_result)

    mock_factory = MagicMock(return_value=mock_db_session)

    count = await cleanup_expired_sessions(
        mock_factory,
        held_subjects=frozenset({"held@test"}),
    )
    assert count == 0


@pytest.mark.asyncio
async def test_cleanup_processes_expired_sessions() -> None:
    """Expired sessions without holds are cleaned up."""
    expired_session = _make_session(expired=True)

    # First call returns sessions, subsequent calls manage the update.
    mock_db_session = MagicMock()
    mock_db_session.__aenter__ = AsyncMock(return_value=mock_db_session)
    mock_db_session.__aexit__ = AsyncMock(return_value=False)

    mock_begin = MagicMock()
    mock_begin.__aenter__ = AsyncMock(return_value=None)
    mock_begin.__aexit__ = AsyncMock(return_value=False)
    mock_db_session.begin = MagicMock(return_value=mock_begin)

    # First execute returns sessions list, second is the update, third is reload.
    session_result = MagicMock()
    session_result.scalars.return_value.all.return_value = [expired_session]

    reload_result = MagicMock()
    reload_result.scalar_one_or_none.return_value = expired_session

    mock_db_session.execute = AsyncMock(side_effect=[session_result, None, reload_result])

    mock_factory = MagicMock(return_value=mock_db_session)

    count = await cleanup_expired_sessions(mock_factory)
    assert count == 1
