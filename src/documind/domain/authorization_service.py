"""Deterministic authorization contract per §4.2.

Authorization is calculated before document lookup results, projection
queries, object reads, webhook administration, and agent-tool invocation.
The evaluator receives a canonical resource descriptor and returns only
**allow**, **deny**, or **not-found-equivalent-deny**.  It never accepts
a datastore query or an LLM recommendation as an input.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.domain.errors import PolicyUnavailableError
from documind.domain.label_service import LabelService
from documind.domain.policy_service import PolicyService, RoleMapping
from documind.models.document import Document, DocumentVersion
from documind.models.label import DeletionTombstone, DocumentLabel, LegalHold
from documind.services.audit_service import AuditEntry, AuditService
from documind.services.identity_service import Principal

logger = structlog.get_logger()


class AuthorizationDecision(enum.StrEnum):
    """Outcome of a deterministic authorization evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    NOT_FOUND = "not_found"


# Actions whose denial should appear as 404 (no existence disclosure).
_READ_ACTIONS = frozenset(
    {
        "read",
        "read_version",
        "read_chunk",
        "chat",
        "retrieval",
    }
)

# Actions that require no resource labels (resource-free checks).
_RESOURCE_FREE_ACTIONS = frozenset(
    {
        "identity_read",
    }
)

# Lifecycle states that are compatible with read actions.
_READABLE_LIFECYCLES = frozenset(
    {
        "accepted",
        "processing",
        "completed",
        "failed",
        "quarantined",
    }
)

# Lifecycle states that are compatible with write/mutate actions.
_WRITABLE_LIFECYCLES = frozenset(
    {
        "accepted",
        "processing",
        "completed",
        "failed",
    }
)


@dataclass(frozen=True)
class AuthorizationResult:
    """Outcome of an ``authorize()`` call with audit metadata."""

    decision: AuthorizationDecision
    reason: str
    rule_ids: list[str] = field(default_factory=list)


class AuthorizationService:
    """Deterministic, label-based authorization per §4.2 pseudocode."""

    def __init__(
        self,
        policy_service: PolicyService,
        label_service: LabelService,
        audit_service: AuditService,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._policy_service = policy_service
        self._label_service = label_service
        self._audit_service = audit_service
        self._session_factory = session_factory

    async def authorize(
        self,
        principal: Principal,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID | None = None,
    ) -> AuthorizationResult:
        """Evaluate a deterministic authorization decision.

        When *resource_id* is provided, the canonical resource descriptor
        (lifecycle and label IDs) is always loaded from the database.
        Caller-supplied labels or lifecycle are never trusted.

        Implements the exact §4.2 pseudocode::

            authorize(principal, action, resource):
              require principal.active and verified_identity
              require current_policy_revision available
              require resource.lifecycle compatible with action
              require resource not tombstoned
              require legal_hold permits action
              effective_roles = map_groups_to_roles(principal.groups)
              allowed_labels = union(role.allowed_labels for role in effective_roles)
              deny when resource.labels not subset of allowed_labels
              emit allow or deny audit event with rule IDs

        Returns:
            ``AuthorizationResult`` with decision, reason, and rule IDs.
        """
        rule_ids: list[str] = []

        # 1. Require principal active and verified.
        if not principal.active:
            result = self._deny(action, "principal_inactive", rule_ids)
            await self._emit_audit(principal, action, resource_type, resource_id, result)
            return result

        # 2. Require current policy revision available (fail-closed).
        try:
            role_mappings = await self._policy_service.get_role_mappings(principal.groups)
        except Exception as exc:
            await logger.aerror("authorization_policy_error", error=str(exc))
            raise PolicyUnavailableError() from exc

        if not role_mappings:
            result = self._deny(action, "no_role_mappings", rule_ids)
            await self._emit_audit(principal, action, resource_type, resource_id, result)
            return result

        rule_ids.extend(f"role:{rm.role_key}" for rm in role_mappings)

        # For resource-free actions, skip resource checks.
        if action in _RESOURCE_FREE_ACTIONS:
            result = AuthorizationResult(
                decision=AuthorizationDecision.ALLOW,
                reason="resource_free_action",
                rule_ids=rule_ids,
            )
            await self._emit_audit(principal, action, resource_type, resource_id, result)
            return result

        # 3. Load the canonical resource descriptor from the database.
        #    Never trust caller-supplied lifecycle or labels.
        resource_lifecycle: str | None = None
        resource_labels: list[uuid.UUID] | None = None

        if resource_id is not None:
            resource_lifecycle, resource_labels = await self._load_resource_descriptor(resource_id)

        # 4. Require resource lifecycle compatible with action.
        if resource_lifecycle is not None:
            if resource_lifecycle == "erased":
                result = self._deny(action, "resource_erased", rule_ids)
                await self._emit_audit(principal, action, resource_type, resource_id, result)
                return result

            if action in _READ_ACTIONS and resource_lifecycle not in _READABLE_LIFECYCLES:
                result = self._deny(action, "lifecycle_incompatible_read", rule_ids)
                await self._emit_audit(principal, action, resource_type, resource_id, result)
                return result

            if action not in _READ_ACTIONS and resource_lifecycle not in _WRITABLE_LIFECYCLES:
                result = self._deny(action, "lifecycle_incompatible_write", rule_ids)
                await self._emit_audit(principal, action, resource_type, resource_id, result)
                return result

        # 5. Require resource not tombstoned.
        if resource_id is not None and await self._is_tombstoned(resource_id):
            result = self._deny(action, "resource_tombstoned", rule_ids)
            await self._emit_audit(principal, action, resource_type, resource_id, result)
            return result

        # 6. Require legal hold permits action.
        if (
            resource_id is not None
            and action in {"delete", "export", "erase"}
            and await self._has_active_legal_hold(resource_id)
        ):
            result = AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                reason="legal_hold_active",
                rule_ids=rule_ids,
            )
            await self._emit_audit(principal, action, resource_type, resource_id, result)
            return result

        # 7–8. Compute effective roles → allowed labels.
        allowed_labels = self._union_allowed_labels(role_mappings)

        # Check action permission across all roles.
        if not self._action_permitted(action, role_mappings):
            result = self._deny(action, "action_not_permitted", rule_ids)
            await self._emit_audit(principal, action, resource_type, resource_id, result)
            return result

        # 9. Deny when resource labels not subset of allowed labels.
        if resource_labels is not None:
            resource_label_set = set(resource_labels)
            if not resource_label_set.issubset(allowed_labels):
                missing = resource_label_set - allowed_labels
                rule_ids.append(f"missing_labels:{len(missing)}")
                result = self._deny(action, "labels_not_permitted", rule_ids)
                await self._emit_audit(principal, action, resource_type, resource_id, result)
                return result

        # All checks passed.
        result = AuthorizationResult(
            decision=AuthorizationDecision.ALLOW,
            reason="authorized",
            rule_ids=rule_ids,
        )
        await self._emit_audit(principal, action, resource_type, resource_id, result)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _deny(action: str, reason: str, rule_ids: list[str]) -> AuthorizationResult:
        """Build a deny or not-found result depending on action type."""
        decision = AuthorizationDecision.NOT_FOUND if action in _READ_ACTIONS else AuthorizationDecision.DENY
        return AuthorizationResult(decision=decision, reason=reason, rule_ids=rule_ids)

    @staticmethod
    def _union_allowed_labels(mappings: list[RoleMapping]) -> set[uuid.UUID]:
        """Merge allowed label sets across all effective roles."""
        result: set[uuid.UUID] = set()
        for mapping in mappings:
            result.update(mapping.allowed_label_ids)
        return result

    @staticmethod
    def _action_permitted(action: str, mappings: list[RoleMapping]) -> bool:
        """Check if any effective role permits the requested action."""
        return any(action in mapping.permitted_actions for mapping in mappings)

    async def _load_resource_descriptor(
        self,
        resource_id: uuid.UUID,
    ) -> tuple[str | None, list[uuid.UUID] | None]:
        """Load the canonical lifecycle and label IDs for a document.

        Returns ``(lifecycle_value, label_id_list)``.  If the document
        is not found (e.g. it doesn't exist), both values are ``None``.
        """
        async with self._session_factory() as session:
            # Load the document to get current_completed_version_id.
            doc = await session.get(Document, resource_id)
            if doc is None:
                return None, None

            # Resolve lifecycle from the current completed version.
            lifecycle: str | None = None
            if doc.current_completed_version_id is not None:
                version = await session.get(DocumentVersion, doc.current_completed_version_id)
                if version is not None:
                    lifecycle = version.lifecycle.value if hasattr(version.lifecycle, "value") else str(version.lifecycle)

            # If erased_at is set, treat as erased regardless of version.
            if doc.erased_at is not None:
                lifecycle = "erased"

            # Load label IDs from document_label.
            label_stmt = select(DocumentLabel.label_id).where(
                DocumentLabel.document_id == resource_id,
            )
            label_result = await session.execute(label_stmt)
            label_ids = list(label_result.scalars().all())

        return lifecycle, label_ids

    async def _is_tombstoned(self, resource_id: uuid.UUID) -> bool:
        """Check whether a document has an active deletion tombstone."""
        async with self._session_factory() as session:
            stmt = select(DeletionTombstone.id).where(DeletionTombstone.document_id == resource_id).limit(1)
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    async def _has_active_legal_hold(self, resource_id: uuid.UUID) -> bool:
        """Check for an active legal hold on a document."""
        async with self._session_factory() as session:
            stmt = (
                select(LegalHold.id)
                .where(
                    LegalHold.document_id == resource_id,
                    LegalHold.active.is_(True),
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    async def _emit_audit(
        self,
        principal: Principal,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID | None,
        result: AuthorizationResult,
    ) -> None:
        """Write a durable audit event for every authorization decision.

        Access decisions are evidence-bearing security events.  If PostgreSQL
        cannot commit the audit record, callers must not proceed with an
        unaudited allow or deny response; the error propagates and the request
        fails closed.
        """
        await self._audit_service.write_event(
            AuditEntry(
                actor_subject=principal.subject,
                action=f"authorization.{result.decision.value}",
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id else None,
                details={
                    "requested_action": action,
                    "decision": result.decision.value,
                    "reason": result.reason,
                    "rule_ids": result.rule_ids,
                },
            )
        )
