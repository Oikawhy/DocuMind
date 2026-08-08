"""Versioned policy evaluation per §4.3.

Policy revisions are immutable once activated.  The service resolves
the currently active revision for a given ``(policy_kind, stable_key)``
pair and maps IdP group selectors to role definitions for
authorization.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.enums import PolicyStatus
from documind.models.policy import PolicyRevision

logger = structlog.get_logger()


@dataclass(frozen=True)
class RoleMapping:
    """Resolved role with its label permissions and allowed actions.

    Produced by matching a principal's IdP groups against the
    ``group_selector`` in active ``authorization`` policy bodies.
    """

    role_key: str
    allowed_label_ids: set[uuid.UUID] = field(default_factory=set)
    permitted_actions: set[str] = field(default_factory=set)
    policy_revision_id: uuid.UUID | None = None


class PolicyService:
    """Read-path policy resolution for authorization decisions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_active_policy(
        self,
        policy_kind: str,
        stable_key: str,
    ) -> PolicyRevision | None:
        """Return the active revision for the given kind and key, or ``None``."""
        async with self._session_factory() as session:
            stmt = (
                select(PolicyRevision)
                .where(
                    PolicyRevision.policy_kind == policy_kind,
                    PolicyRevision.stable_key == stable_key,
                    PolicyRevision.status == PolicyStatus.ACTIVE,
                )
                .order_by(PolicyRevision.revision.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_role_mappings(self, group_keys: list[str]) -> list[RoleMapping]:
        """Resolve all active authorization role mappings that match the caller's groups.

        Each active ``authorization`` policy revision whose body contains a
        ``group_selector`` list intersecting with ``group_keys`` produces a
        ``RoleMapping``.

        Expected policy body JSON schema::

            {
                "group_selector": ["admin-group", "editors"],
                "role_key": "editor",
                "allowed_label_ids": ["<uuid>", ...],
                "permitted_actions": ["read", "upload", "version_create"]
            }
        """
        if not group_keys:
            return []

        group_set = set(group_keys)
        async with self._session_factory() as session:
            stmt = select(PolicyRevision).where(
                PolicyRevision.policy_kind == "authorization",
                PolicyRevision.status == PolicyStatus.ACTIVE,
            )
            result = await session.execute(stmt)
            revisions = result.scalars().all()

        mappings: list[RoleMapping] = []
        for rev in revisions:
            body = rev.body
            if not isinstance(body, dict):
                continue

            selectors = body.get("group_selector", [])
            if not isinstance(selectors, list):
                continue

            # Match if any of the caller's groups appear in the selector.
            if not group_set.intersection(selectors):
                continue

            role_key = body.get("role_key", "")
            raw_labels = body.get("allowed_label_ids", [])
            raw_actions = body.get("permitted_actions", [])

            allowed_label_ids: set[uuid.UUID] = set()
            for raw_id in raw_labels:
                try:
                    allowed_label_ids.add(uuid.UUID(str(raw_id)))
                except ValueError:
                    await logger.awarning(
                        "policy_invalid_label_id",
                        revision_id=str(rev.id),
                        raw_id=raw_id,
                    )

            mappings.append(
                RoleMapping(
                    role_key=str(role_key),
                    allowed_label_ids=allowed_label_ids,
                    permitted_actions=set(str(a) for a in raw_actions),
                    policy_revision_id=rev.id,
                )
            )

        return mappings
