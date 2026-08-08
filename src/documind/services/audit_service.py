"""Hash-chained audit event writer per §10.4.

The writer serialises event insertion through a PostgreSQL transaction-scoped
advisory lock so the ``previous_hash → event_hash`` chain remains deterministic
across every application and worker process.  Each event is persisted together with an
``AuditEventIdentity`` row that enforces cross-partition uniqueness on
``(id, event_hash)``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.audit import AuditAnchor, AuditEvent, AuditEventIdentity

logger = structlog.get_logger()

_GENESIS_HASH = "0" * 64  # Sentinel for the first event in the chain.


@dataclass(frozen=True)
class AuditEntry:
    """Caller-facing value object describing an auditable action.

    ``details`` must not contain raw document content, credentials,
    or full prompts (§10.4).
    """

    actor_subject: str | None
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    trace_id: uuid.UUID | None = None


class AuditService:
    """Database-serialised, hash-chained audit event writer."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    async def _lock_chain(session: AsyncSession) -> None:
        """Serialize all writer processes for the duration of this transaction."""
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('documind.audit.hash_chain'))"))

    async def _load_last_hash(self, session: AsyncSession) -> str:
        """Load the most recent event hash to resume the chain after restart."""
        stmt = (
            select(AuditEventIdentity.event_hash)
            .order_by(AuditEventIdentity.event_time.desc(), AuditEventIdentity.id.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        return row if row is not None else _GENESIS_HASH

    @staticmethod
    def _compute_hash(
        previous_hash: str,
        action: str,
        resource_type: str,
        resource_id: str | None,
        details: dict[str, Any],
        event_time_iso: str,
    ) -> str:
        """SHA-256 over the chain link fields."""
        parts = [
            previous_hash,
            action,
            resource_type,
            resource_id or "",
            json.dumps(details, sort_keys=True, default=str),
            event_time_iso,
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    async def write_event(self, entry: AuditEntry) -> uuid.UUID:
        """Persist an audit event with hash-chain linkage.

        Returns the UUID of the newly created event.
        """
        async with self._session_factory() as session, session.begin():
            await self._lock_chain(session)
            previous_hash = await self._load_last_hash(session)
            event_id = uuid.uuid4()
            now = datetime.now(UTC)
            event_time_iso = now.isoformat()

            event_hash = self._compute_hash(
                previous_hash,
                entry.action,
                entry.resource_type,
                entry.resource_id,
                entry.details,
                event_time_iso,
            )

            event = AuditEvent(
                id=event_id,
                event_time=now,
                actor_subject=entry.actor_subject,
                action=entry.action,
                resource_type=entry.resource_type,
                resource_id=entry.resource_id,
                trace_id=entry.trace_id,
                details=entry.details,
                previous_hash=previous_hash,
                event_hash=event_hash,
            )
            identity = AuditEventIdentity(
                id=event_id,
                event_hash=event_hash,
                event_time=now,
            )

            # The identity table is the cross-partition uniqueness guard and
            # must be written first in this same transaction (§8.2).
            session.add(identity)
            await session.flush()
            session.add(event)

        await logger.ainfo(
            "audit_event_written",
            event_id=str(event_id),
            action=entry.action,
            resource_type=entry.resource_type,
        )
        return event_id

    async def write_event_in_session(self, session: AsyncSession, entry: AuditEntry) -> uuid.UUID:
        """Add an audit event to a caller-owned transaction without committing.

        Admission uses this method so lifecycle metadata, its audit record,
        and the outbox envelope have one all-or-nothing PostgreSQL boundary.
        """
        await self._lock_chain(session)
        previous_hash = await self._load_last_hash(session)
        event_id = uuid.uuid4()
        now = datetime.now(UTC)
        event_hash = self._compute_hash(
            previous_hash,
            entry.action,
            entry.resource_type,
            entry.resource_id,
            entry.details,
            now.isoformat(),
        )
        # Flush the identity first so its uniqueness invariant is evaluated
        # before routing the event into its monthly audit partition.
        session.add(AuditEventIdentity(id=event_id, event_hash=event_hash, event_time=now))
        await session.flush()
        session.add(
            AuditEvent(
                id=event_id,
                event_time=now,
                actor_subject=entry.actor_subject,
                action=entry.action,
                resource_type=entry.resource_type,
                resource_id=entry.resource_id,
                trace_id=entry.trace_id,
                details=entry.details,
                previous_hash=previous_hash,
                event_hash=event_hash,
            )
        )
        return event_id

    async def seal_anchor(
        self,
        period_start: datetime,
        period_end: datetime,
        signature: str,
        sealed_object_key: str,
        sealed_sha256: str,
    ) -> uuid.UUID:
        """Create a WORM-sealed audit anchor for a time period.

        The anchor records the terminal event hash, a cryptographic
        signature, and the MinIO object key of the sealed log bundle.
        """
        async with self._session_factory() as session, session.begin():
            await self._lock_chain(session)
            anchor = AuditAnchor(
                id=uuid.uuid4(),
                period_start=period_start,
                period_end=period_end,
                terminal_event_hash=await self._load_last_hash(session),
                signature=signature,
                sealed_object_key=sealed_object_key,
                sealed_sha256=sealed_sha256,
            )
            session.add(anchor)

        await logger.ainfo(
            "audit_anchor_sealed",
            anchor_id=str(anchor.id),
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )
        return anchor.id
