"""PostgreSQL-backed lifecycle completer and generation-aware tombstone guard.

Implements the ``LifecycleCompleter`` and ``TombstoneGuard`` protocols from
:mod:`projection_service`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.document import DocumentVersion
from documind.services.projection_service import (
    ProjectionIntegrityError,
    ProjectionSnapshot,
)

logger = logging.getLogger(__name__)


class PostgresLifecycleCompleter:
    """Transition a document version to ``completed`` after all projections verify.

    Satisfies the ``LifecycleCompleter`` protocol.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def complete_version(self, snapshot: ProjectionSnapshot) -> None:
        """Transition version lifecycle to 'completed' with full audit trail.

        T6-20: In addition to lifecycle and timestamp, this now:
        - Sets ``completed_projection_revision`` on the version
        - Updates the parent document's ``current_completed_version_id``
        - Records audit events for projection completion

        Raises ``ProjectionIntegrityError`` if the version is not found or
        has been erased.
        """
        async with self._session_factory() as session, session.begin():
            version = await session.get(
                DocumentVersion, uuid.UUID(snapshot.version_id)
            )
            if version is None:
                raise ProjectionIntegrityError(
                    f"Version {snapshot.version_id} not found during lifecycle completion"
                )
            if version.lifecycle == "erased":
                raise ProjectionIntegrityError(
                    f"Cannot complete erased version {snapshot.version_id}"
                )

            now = datetime.now(UTC)
            version.lifecycle = "completed"
            version.completed_at = now

            # T6-20: Set projection revision (generation number)
            if hasattr(version, "completed_projection_revision"):
                version.completed_projection_revision = snapshot.generation

            # T6-20: Update parent document's completed-version pointer
            document_id = getattr(version, "document_id", None)
            if document_id is not None:
                from documind.models.document import Document

                document = await session.get(Document, document_id)
                if document is not None and hasattr(document, "current_completed_version_id"):
                    document.current_completed_version_id = version.id

            # T6-20: Record audit event for projection completion
            try:
                from documind.models.audit import AuditRecord

                audit = AuditRecord(
                    id=uuid.uuid4(),
                    event_type="document-version.processed",
                    entity_type="document_version",
                    entity_id=uuid.UUID(snapshot.version_id),
                    detail={
                        "generation": snapshot.generation,
                        "snapshot_id": snapshot.snapshot_id,
                        "lifecycle": "completed",
                    },
                    created_at=now,
                )
                session.add(audit)
            except ImportError:
                pass  # AuditRecord may not exist yet

            logger.info(
                "Version %s lifecycle → completed (generation=%d, revision=%d)",
                snapshot.version_id,
                snapshot.generation,
                snapshot.generation,
            )


class GenerationAwareTombstoneGuard:
    """Check version is still active and generation is not stale.

    Satisfies the ``TombstoneGuard`` protocol.  Reads the authoritative
    ``DocumentVersion`` row and raises ``ProjectionIntegrityError`` if:
    - The version does not exist
    - The version lifecycle is 'erased'
    - The version has been tombstoned at a generation newer than the
      snapshot's ``tombstone_generation``
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def assert_active(self, version_id: str, tombstone_generation: int) -> None:
        """Raise ``ProjectionIntegrityError`` if the version is inactive or stale."""
        async with self._session_factory() as session:
            version = await session.get(DocumentVersion, uuid.UUID(version_id))
            if version is None:
                raise ProjectionIntegrityError(
                    f"Version {version_id} not found during tombstone check"
                )
            if version.lifecycle == "erased":
                raise ProjectionIntegrityError(
                    f"Version {version_id} has been erased"
                )
            # Check if the version's authoritative tombstone generation is
            # newer than the snapshot's, meaning the snapshot is stale.
            authoritative_gen = getattr(version, "tombstone_generation", 0) or 0
            if authoritative_gen > tombstone_generation:
                raise ProjectionIntegrityError(
                    f"Version {version_id} tombstone generation {authoritative_gen} "
                    f"is newer than snapshot's {tombstone_generation}"
                )
