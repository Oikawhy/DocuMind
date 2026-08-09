"""Versioned policy evaluation and lifecycle management per §4.3.

Policy revisions follow an immutable lifecycle: draft → review →
active (supersedes previous) → retired.  A revision in review may also
be rejected.  The service resolves the currently active revision for a
given ``(policy_kind, stable_key)`` pair and maps IdP group selectors
to role definitions for authorization.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.domain.errors import InvalidRequestError, ResourceConflictError, ResourceNotFoundError
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
    """Policy resolution and revision lifecycle management per §4.3."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

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

    async def get_revision(self, revision_id: uuid.UUID) -> PolicyRevision | None:
        """Return a single policy revision by ID, or ``None``."""
        async with self._session_factory() as session:
            return await session.get(PolicyRevision, revision_id)

    async def list_revisions(
        self,
        policy_kind: str,
        stable_key: str,
    ) -> list[PolicyRevision]:
        """Return all revisions for the given kind and key, newest first."""
        async with self._session_factory() as session:
            stmt = (
                select(PolicyRevision)
                .where(
                    PolicyRevision.policy_kind == policy_kind,
                    PolicyRevision.stable_key == stable_key,
                )
                .order_by(PolicyRevision.revision.desc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

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

    # ------------------------------------------------------------------
    # Write path — revision CRUD / activation lifecycle
    # ------------------------------------------------------------------

    async def create_revision(
        self,
        policy_kind: str,
        stable_key: str,
        body: dict[str, Any],
        created_by_subject: str,
    ) -> PolicyRevision:
        """Create a new DRAFT revision, auto-incrementing the revision number.

        Returns the newly created ``PolicyRevision``.
        """
        body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
        body_sha256 = hashlib.sha256(body_json.encode()).hexdigest()

        async with self._session_factory() as session, session.begin():
            # Determine the next revision number.
            stmt = select(func.coalesce(func.max(PolicyRevision.revision), 0)).where(
                PolicyRevision.policy_kind == policy_kind,
                PolicyRevision.stable_key == stable_key,
            )
            result = await session.execute(stmt)
            max_rev = result.scalar_one()
            next_rev = max_rev + 1

            revision = PolicyRevision(
                id=uuid.uuid4(),
                policy_kind=policy_kind,
                stable_key=stable_key,
                revision=next_rev,
                status=PolicyStatus.DRAFT,
                body=body,
                body_sha256=body_sha256,
                created_by_subject=created_by_subject,
            )
            session.add(revision)

        await logger.ainfo(
            "policy_revision_created",
            revision_id=str(revision.id),
            policy_kind=policy_kind,
            stable_key=stable_key,
            revision=next_rev,
        )
        return revision

    async def submit_for_review(self, revision_id: uuid.UUID) -> PolicyRevision:
        """Transition a revision from DRAFT → REVIEW.

        Raises:
            ResourceNotFoundError: Revision not found.
            InvalidRequestError: Revision is not in DRAFT status.
        """
        async with self._session_factory() as session, session.begin():
            revision = await session.get(PolicyRevision, revision_id)
            if revision is None:
                raise ResourceNotFoundError("Policy revision not found.")

            if revision.status != PolicyStatus.DRAFT:
                raise InvalidRequestError(
                    f"Cannot submit revision in '{revision.status.value}' status for review; "
                    f"only DRAFT revisions may be submitted.",
                    code="INVALID_LIFECYCLE_TRANSITION",
                )

            revision.status = PolicyStatus.REVIEW

        await logger.ainfo("policy_revision_submitted", revision_id=str(revision_id))
        return revision

    async def activate_revision(
        self,
        revision_id: uuid.UUID,
        approved_by_subject: str,
    ) -> PolicyRevision:
        """Transition a revision from REVIEW → ACTIVE and supersede the prior active.

        Any previously active revision for the same ``(policy_kind,
        stable_key)`` is transitioned to SUPERSEDED atomically.

        Raises:
            ResourceNotFoundError: Revision not found.
            InvalidRequestError: Revision is not in REVIEW status.
        """
        async with self._session_factory() as session, session.begin():
            revision = await session.get(PolicyRevision, revision_id)
            if revision is None:
                raise ResourceNotFoundError("Policy revision not found.")

            if revision.status != PolicyStatus.REVIEW:
                raise InvalidRequestError(
                    f"Cannot activate revision in '{revision.status.value}' status; "
                    f"only REVIEW revisions may be activated.",
                    code="INVALID_LIFECYCLE_TRANSITION",
                )

            # Supersede prior active revisions for the same (kind, key).
            stmt = select(PolicyRevision).where(
                PolicyRevision.policy_kind == revision.policy_kind,
                PolicyRevision.stable_key == revision.stable_key,
                PolicyRevision.status == PolicyStatus.ACTIVE,
            )
            result = await session.execute(stmt)
            for prior in result.scalars().all():
                prior.status = PolicyStatus.SUPERSEDED
                await logger.ainfo(
                    "policy_revision_superseded",
                    superseded_id=str(prior.id),
                    by_id=str(revision_id),
                )

            now = datetime.now(UTC)
            revision.status = PolicyStatus.ACTIVE
            revision.approved_by_subject = approved_by_subject
            revision.activated_at = now

        await logger.ainfo(
            "policy_revision_activated",
            revision_id=str(revision_id),
            approved_by=approved_by_subject,
        )
        return revision

    async def reject_revision(self, revision_id: uuid.UUID) -> PolicyRevision:
        """Transition a revision from REVIEW → REJECTED.

        Raises:
            ResourceNotFoundError: Revision not found.
            InvalidRequestError: Revision is not in REVIEW status.
        """
        async with self._session_factory() as session, session.begin():
            revision = await session.get(PolicyRevision, revision_id)
            if revision is None:
                raise ResourceNotFoundError("Policy revision not found.")

            if revision.status != PolicyStatus.REVIEW:
                raise InvalidRequestError(
                    f"Cannot reject revision in '{revision.status.value}' status; "
                    f"only REVIEW revisions may be rejected.",
                    code="INVALID_LIFECYCLE_TRANSITION",
                )

            revision.status = PolicyStatus.REJECTED

        await logger.ainfo("policy_revision_rejected", revision_id=str(revision_id))
        return revision

    async def retire_revision(self, revision_id: uuid.UUID) -> PolicyRevision:
        """Transition a revision from ACTIVE → RETIRED.

        Raises:
            ResourceNotFoundError: Revision not found.
            InvalidRequestError: Revision is not in ACTIVE status.
        """
        async with self._session_factory() as session, session.begin():
            revision = await session.get(PolicyRevision, revision_id)
            if revision is None:
                raise ResourceNotFoundError("Policy revision not found.")

            if revision.status != PolicyStatus.ACTIVE:
                raise InvalidRequestError(
                    f"Cannot retire revision in '{revision.status.value}' status; "
                    f"only ACTIVE revisions may be retired.",
                    code="INVALID_LIFECYCLE_TRANSITION",
                )

            revision.status = PolicyStatus.RETIRED

        await logger.ainfo("policy_revision_retired", revision_id=str(revision_id))
        return revision
