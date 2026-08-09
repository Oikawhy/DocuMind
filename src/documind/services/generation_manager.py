"""Active generation manager for ``active_projection_generation`` table.

Reads, allocates, and atomically switches the active generation pointer
for each ``(projection_kind, scope_key)`` pair per §8.1.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.projection import ActiveProjectionGeneration, ProjectionState

logger = logging.getLogger(__name__)


class ActiveGenerationManager:
    """Manage generation allocation and atomic activation.

    Provides three operations:
    - ``allocate``: reserve the next generation number
    - ``activate``: atomically switch the active pointer
    - ``current``: read the currently active generation
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def allocate(self, kind: str, scope_key: str) -> int:
        """Return the next generation number for this (kind, scope_key).

        Queries ``projection_state`` for the max generation and returns
        max + 1.  If no prior generation exists, returns 1.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.max(ProjectionState.generation)).where(
                    ProjectionState.projection_kind == kind,
                    ProjectionState.scope_key == scope_key,
                )
            )
            max_gen = result.scalar_one_or_none()
            return (max_gen or 0) + 1

    async def activate(
        self,
        kind: str,
        scope_key: str,
        generation: int,
        *,
        operation_id: uuid.UUID | None = None,
    ) -> None:
        """Atomically switch the active generation pointer.

        Upserts the ``active_projection_generation`` row for this
        (kind, scope_key) to point at the given generation.
        """
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    select(ActiveProjectionGeneration).where(
                        ActiveProjectionGeneration.projection_kind == kind,
                        ActiveProjectionGeneration.scope_key == scope_key,
                    )
                )
            ).scalar_one_or_none()

            now = datetime.now(UTC)
            if row is None:
                row = ActiveProjectionGeneration(
                    projection_kind=kind,
                    scope_key=scope_key,
                    generation=generation,
                    activated_at=now,
                    activated_by_operation_id=operation_id,
                )
                session.add(row)
            else:
                row.generation = generation
                row.activated_at = now
                row.activated_by_operation_id = operation_id

            logger.info(
                "Activated generation %d for %s/%s",
                generation,
                kind,
                scope_key,
            )

    async def current(self, kind: str, scope_key: str) -> int | None:
        """Read the currently active generation, or None if never activated."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ActiveProjectionGeneration).where(
                        ActiveProjectionGeneration.projection_kind == kind,
                        ActiveProjectionGeneration.scope_key == scope_key,
                    )
                )
            ).scalar_one_or_none()
            return row.generation if row is not None else None
