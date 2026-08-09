"""PostgreSQL-backed projection evidence store and incident sink.

Implements the ``ProjectionEvidenceStore`` and ``ProjectionIncidentSink``
protocols from :mod:`projection_service` using the ``projection_state``
ORM model from :mod:`models.projection`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.projection import ProjectionState
from documind.services.projection_service import (
    ProjectionBackend,
    ProjectionIncident,
    ProjectionManifest,
    ProjectionSnapshot,
    WriterOutcome,
)

logger = logging.getLogger(__name__)


class PostgresEvidenceStore:
    """Durable evidence store backed by the ``projection_state`` table.

    Satisfies both ``ProjectionEvidenceStore`` and ``ProjectionIncidentSink``
    protocols so the coordinator can use a single adapter for all evidence.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- ProjectionEvidenceStore ---

    async def state_for(
        self,
        backend: ProjectionBackend,
        snapshot: ProjectionSnapshot,
    ) -> WriterOutcome | None:
        """Retrieve the most recent writer outcome for this backend+snapshot."""
        scope_key = _scope_key(backend, snapshot)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ProjectionState)
                    .where(
                        ProjectionState.projection_kind == backend.value,
                        ProjectionState.scope_key == scope_key,
                        ProjectionState.generation == snapshot.generation,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return WriterOutcome(
            backend=backend,
            snapshot_id=snapshot.snapshot_id,
            generation=row.generation,
            tombstone_generation=snapshot.tombstone_generation,
            status=row.state,
            manifest=ProjectionManifest(
                backend=backend,
                snapshot_id=snapshot.snapshot_id,
                generation=row.generation,
                tombstone_generation=snapshot.tombstone_generation,
                record_count=row.expected_count,
                checksum=row.source_sha256,
            ) if row.state == "verified" else None,
        )

    async def record_outcome(self, outcome: WriterOutcome) -> None:
        """Persist or update a writer outcome as a ``projection_state`` row."""
        async with self._session_factory() as session, session.begin():
            scope_key = outcome.snapshot_id if outcome.backend != ProjectionBackend.NEO4J else "global"
            row = (
                await session.execute(
                    select(ProjectionState).where(
                        ProjectionState.projection_kind == outcome.backend.value,
                        ProjectionState.scope_key == scope_key,
                        ProjectionState.generation == outcome.generation,
                    )
                )
            ).scalar_one_or_none()

            now = datetime.now(UTC)
            if row is None:
                row = ProjectionState(
                    id=uuid.uuid4(),
                    projection_kind=outcome.backend.value,
                    scope_key=scope_key,
                    generation=outcome.generation,
                    state=outcome.status,
                    expected_count=outcome.manifest.record_count if outcome.manifest else 0,
                    source_sha256=outcome.manifest.checksum if outcome.manifest else "",
                    created_at=now,
                    state_changed_at=now,
                    last_error_code=outcome.safe_error_class,
                )
                session.add(row)
            else:
                row.state = outcome.status
                row.state_changed_at = now
                if outcome.manifest:
                    row.expected_count = outcome.manifest.record_count
                    row.source_sha256 = outcome.manifest.checksum
                if outcome.safe_error_class:
                    row.last_error_code = outcome.safe_error_class

    async def record_manifest(self, manifest: ProjectionManifest) -> None:
        """Mark a projection as verified with the observed manifest data."""
        async with self._session_factory() as session, session.begin():
            scope_key = manifest.snapshot_id if manifest.backend != ProjectionBackend.NEO4J else "global"
            row = (
                await session.execute(
                    select(ProjectionState).where(
                        ProjectionState.projection_kind == manifest.backend.value,
                        ProjectionState.scope_key == scope_key,
                        ProjectionState.generation == manifest.generation,
                    )
                )
            ).scalar_one_or_none()

            now = datetime.now(UTC)
            if row is None:
                row = ProjectionState(
                    id=uuid.uuid4(),
                    projection_kind=manifest.backend.value,
                    scope_key=scope_key,
                    generation=manifest.generation,
                    state="verified",
                    expected_count=manifest.record_count,
                    observed_count=manifest.record_count,
                    source_sha256=manifest.checksum,
                    verified_at=now,
                    created_at=now,
                    state_changed_at=now,
                )
                session.add(row)
            else:
                row.state = "verified"
                row.observed_count = manifest.record_count
                row.verified_at = now
                row.state_changed_at = now

    # --- ProjectionIncidentSink ---

    async def record_incident(self, incident: ProjectionIncident) -> None:
        """Record an integrity incident as an unhealthy projection_state row."""
        async with self._session_factory() as session, session.begin():
            scope_key = incident.snapshot_id if incident.projection_type != ProjectionBackend.NEO4J else "global"
            now = datetime.now(UTC)
            row = ProjectionState(
                id=uuid.uuid4(),
                projection_kind=incident.projection_type.value,
                scope_key=scope_key,
                generation=incident.generation,
                state="unhealthy",
                expected_count=incident.expected_count,
                observed_count=incident.observed_count,
                source_sha256=incident.expected_checksum,
                created_at=now,
                state_changed_at=now,
                last_error_code=incident.safe_error_class,
            )
            session.add(row)
            logger.warning(
                "Projection incident recorded: %s/%s gen=%d err=%s",
                incident.projection_type.value,
                scope_key,
                incident.generation,
                incident.safe_error_class,
            )


def _scope_key(backend: ProjectionBackend, snapshot: ProjectionSnapshot) -> str:
    """Determine the scope_key per the §8.1 scope consistency rules."""
    if backend == ProjectionBackend.NEO4J:
        return "global"
    return snapshot.version_id if snapshot.version_id else snapshot.snapshot_id
