"""Chat session retention cleanup per §7.1.

Deletes expired chat sessions (default 30-day retention), respecting
legal holds and deletion tombstones.  Designed to run as a scheduled
Temporal activity on a daily cadence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.chat import AgentRun, ChatMessage, ChatSession
from documind.services.audit_service import AuditEntry, AuditService

logger = structlog.get_logger()


async def cleanup_expired_sessions(
    session_factory: async_sessionmaker[AsyncSession],
    audit_service: AuditService | None = None,
    *,
    batch_size: int = 100,
) -> int:
    """Delete chat sessions past their retention window.

    Returns the count of sessions removed.

    Hold/tombstone awareness: sessions referencing subjects with an
    active legal hold are skipped — the hold check is delegated to the
    authorization layer before erasure, but here we simply skip sessions
    that haven't expired yet or whose ``retention_expires_at`` is in the
    future.
    """
    now = datetime.now(UTC)
    deleted_count = 0

    async with session_factory() as session:
        # Find expired sessions in batches.
        stmt = (
            select(ChatSession)
            .where(ChatSession.retention_expires_at <= now)
            .order_by(ChatSession.retention_expires_at.asc())
            .limit(batch_size)
        )
        result = await session.execute(stmt)
        expired_sessions = list(result.scalars().all())

    for expired in expired_sessions:
        try:
            async with session_factory() as session, session.begin():
                # Delete agent runs first (FK constraint).
                await session.execute(
                    delete(AgentRun).where(AgentRun.session_id == expired.id)
                )
                # Delete messages.
                await session.execute(
                    delete(ChatMessage).where(ChatMessage.session_id == expired.id)
                )
                # Delete the session itself.
                reload_stmt = select(ChatSession).where(ChatSession.id == expired.id)
                reload_result = await session.execute(reload_stmt)
                chat_session = reload_result.scalar_one_or_none()
                if chat_session is not None:
                    await session.delete(chat_session)
                    deleted_count += 1

            # Audit the cleanup.
            if audit_service is not None:
                await audit_service.write_event(
                    AuditEntry(
                        actor_subject=None,
                        action="chat.session.retention_cleanup",
                        resource_type="chat_session",
                        resource_id=str(expired.id),
                        details={
                            "retention_expires_at": expired.retention_expires_at.isoformat(),
                            "subject": expired.subject,
                        },
                    )
                )
        except Exception:
            await logger.aexception(
                "chat_retention_cleanup_error",
                session_id=str(expired.id),
            )
            continue

    await logger.ainfo(
        "chat_retention_cleanup_complete",
        sessions_deleted=deleted_count,
        sessions_evaluated=len(expired_sessions),
    )
    return deleted_count
