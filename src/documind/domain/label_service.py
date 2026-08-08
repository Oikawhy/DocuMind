"""Hierarchical label validation and label-role intersection per §4.3.

Labels use explicit immutable UUIDs for authorization evaluation.
The hierarchical ``parent_id`` is for presentation only — the evaluator
never walks the tree.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.domain.errors import LabelValidationError
from documind.models.label import DocumentLabel, Label

logger = structlog.get_logger()


class LabelService:
    """Label validation and document-label lookup."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def validate_labels(
        self,
        label_ids: list[uuid.UUID],
        allowed_label_ids: set[uuid.UUID],
    ) -> list[Label]:
        """Validate that all requested labels exist, are active, and are permitted.

        Args:
            label_ids: Label UUIDs the caller wants to assign or access.
            allowed_label_ids: The caller's permitted labels derived from
                their effective roles.

        Returns:
            The list of validated ``Label`` ORM instances.

        Raises:
            LabelValidationError: If any label is missing, inactive, or
                not in the caller's permitted set.
        """
        if not label_ids:
            raise LabelValidationError("At least one label is required.")

        # De-duplicate while preserving order.
        unique_ids = list(dict.fromkeys(label_ids))

        async with self._session_factory() as session:
            stmt = select(Label).where(Label.id.in_(unique_ids))
            result = await session.execute(stmt)
            found = {label.id: label for label in result.scalars().all()}

        # Check for missing labels.
        missing = [lid for lid in unique_ids if lid not in found]
        if missing:
            raise LabelValidationError(
                f"Labels not found: {', '.join(str(m) for m in missing)}",
            )

        # Check for inactive labels.
        inactive = [lid for lid in unique_ids if not found[lid].active]
        if inactive:
            raise LabelValidationError(
                f"Labels are inactive: {', '.join(str(i) for i in inactive)}",
            )

        # Check authorization — every requested label must be in the caller's set.
        unauthorized = [lid for lid in unique_ids if lid not in allowed_label_ids]
        if unauthorized:
            raise LabelValidationError(
                f"Labels not permitted: {', '.join(str(u) for u in unauthorized)}",
            )

        return [found[lid] for lid in unique_ids]

    async def get_document_labels(self, document_id: uuid.UUID) -> list[uuid.UUID]:
        """Return all label IDs currently assigned to a document."""
        async with self._session_factory() as session:
            stmt = select(DocumentLabel.label_id).where(
                DocumentLabel.document_id == document_id,
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
