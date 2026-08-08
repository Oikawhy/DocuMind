"""Tests for the deterministic authorization service per §4.2."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from documind.domain.authorization_service import (
    AuthorizationDecision,
    AuthorizationService,
)
from documind.domain.errors import PolicyUnavailableError
from documind.domain.label_service import LabelService
from documind.domain.policy_service import PolicyService, RoleMapping
from documind.services.audit_service import AuditService
from documind.services.identity_service import Principal

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_principal(
    *,
    subject: str = "user@example.com",
    groups: list[str] | None = None,
    active: bool = True,
) -> Principal:
    return Principal(
        subject=subject,
        display_name="Test User",
        email="user@example.com",
        groups=groups or ["editors"],
        active=active,
        issuer="https://idp.example.com",
    )


LABEL_A = uuid.uuid4()
LABEL_B = uuid.uuid4()
LABEL_C = uuid.uuid4()


def _make_role_mapping(
    role_key: str = "editor",
    allowed_label_ids: set[uuid.UUID] | None = None,
    permitted_actions: set[str] | None = None,
) -> RoleMapping:
    return RoleMapping(
        role_key=role_key,
        allowed_label_ids=allowed_label_ids or {LABEL_A, LABEL_B},
        permitted_actions=permitted_actions or {"read", "upload", "version_create"},
    )


@pytest.fixture
def mock_policy_service() -> AsyncMock:
    svc = AsyncMock(spec=PolicyService)
    svc.get_role_mappings.return_value = [_make_role_mapping()]
    return svc


@pytest.fixture
def mock_label_service() -> AsyncMock:
    return AsyncMock(spec=LabelService)


@pytest.fixture
def mock_audit_service() -> AsyncMock:
    svc = AsyncMock(spec=AuditService)
    svc.write_event.return_value = uuid.uuid4()
    return svc


@pytest.fixture
def mock_session_factory() -> MagicMock:
    """Mock session factory that returns a session with no tombstones/holds.

    ``async_sessionmaker.__call__()`` is *synchronous* — it returns an
    ``AsyncSession`` which is itself an async context manager.  So the
    factory must be a regular ``MagicMock`` (not ``AsyncMock``).

    ``session.execute()`` is async, but the returned ``Result`` is a
    regular object with synchronous methods like ``scalar_one_or_none()``.
    """
    session = AsyncMock()
    # Result from execute is a regular object, not async.
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=session)
    return factory


@pytest.fixture
def auth_service(
    mock_policy_service: AsyncMock,
    mock_label_service: AsyncMock,
    mock_audit_service: AsyncMock,
    mock_session_factory: AsyncMock,
) -> AuthorizationService:
    return AuthorizationService(
        policy_service=mock_policy_service,
        label_service=mock_label_service,
        audit_service=mock_audit_service,
        session_factory=mock_session_factory,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAuthorizeAllow:
    """Tests for successful authorization."""

    async def test_active_principal_matching_labels_allow(
        self,
        auth_service: AuthorizationService,
        mock_audit_service: AsyncMock,
    ) -> None:
        """Active principal with matching labels → ALLOW."""
        principal = _make_principal()
        result = await auth_service.authorize(
            principal=principal,
            action="read",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_A],
            resource_lifecycle="completed",
        )
        assert result.decision == AuthorizationDecision.ALLOW
        assert result.reason == "authorized"
        mock_audit_service.write_event.assert_called_once()

    async def test_resource_free_action_allow(
        self,
        auth_service: AuthorizationService,
    ) -> None:
        """Resource-free actions skip resource checks."""
        principal = _make_principal()
        result = await auth_service.authorize(
            principal=principal,
            action="identity_read",
            resource_type="identity",
        )
        assert result.decision == AuthorizationDecision.ALLOW
        assert result.reason == "resource_free_action"

    async def test_multiple_labels_all_permitted(
        self,
        auth_service: AuthorizationService,
    ) -> None:
        """All resource labels within allowed set → ALLOW."""
        principal = _make_principal()
        result = await auth_service.authorize(
            principal=principal,
            action="read",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_A, LABEL_B],
            resource_lifecycle="completed",
        )
        assert result.decision == AuthorizationDecision.ALLOW


class TestAuthorizeDeny:
    """Tests for authorization denial."""

    async def test_inactive_principal_deny(
        self,
        auth_service: AuthorizationService,
        mock_audit_service: AsyncMock,
    ) -> None:
        """Inactive principal → DENY."""
        principal = _make_principal(active=False)
        result = await auth_service.authorize(
            principal=principal,
            action="read",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_A],
            resource_lifecycle="completed",
        )
        # Read denial uses NOT_FOUND for 404-safe behavior.
        assert result.decision == AuthorizationDecision.NOT_FOUND
        assert result.reason == "principal_inactive"
        mock_audit_service.write_event.assert_called_once()

    async def test_no_role_mappings_deny(
        self,
        auth_service: AuthorizationService,
        mock_policy_service: AsyncMock,
    ) -> None:
        """No matching role mappings → DENY."""
        mock_policy_service.get_role_mappings.return_value = []
        principal = _make_principal()
        result = await auth_service.authorize(
            principal=principal,
            action="read",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_A],
            resource_lifecycle="completed",
        )
        assert result.decision == AuthorizationDecision.NOT_FOUND
        assert result.reason == "no_role_mappings"

    async def test_policy_service_error_raises(
        self,
        auth_service: AuthorizationService,
        mock_policy_service: AsyncMock,
    ) -> None:
        """Policy service error → PolicyUnavailableError (fail-closed)."""
        mock_policy_service.get_role_mappings.side_effect = RuntimeError("DB down")
        principal = _make_principal()
        with pytest.raises(PolicyUnavailableError):
            await auth_service.authorize(
                principal=principal,
                action="read",
                resource_type="document",
            )

    async def test_labels_not_subset_deny(
        self,
        auth_service: AuthorizationService,
    ) -> None:
        """Resource labels not subset of allowed → NOT_FOUND (read) / DENY (write)."""
        principal = _make_principal()
        # LABEL_C is not in the role mapping's allowed set.
        result = await auth_service.authorize(
            principal=principal,
            action="read",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_C],
            resource_lifecycle="completed",
        )
        assert result.decision == AuthorizationDecision.NOT_FOUND
        assert result.reason == "labels_not_permitted"

    async def test_erased_resource_deny(
        self,
        auth_service: AuthorizationService,
    ) -> None:
        """Erased resource → DENY regardless of labels."""
        principal = _make_principal()
        result = await auth_service.authorize(
            principal=principal,
            action="read",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_A],
            resource_lifecycle="erased",
        )
        assert result.decision == AuthorizationDecision.NOT_FOUND
        assert result.reason == "resource_erased"

    async def test_lifecycle_incompatible_deny(
        self,
        auth_service: AuthorizationService,
    ) -> None:
        """Lifecycle incompatible with action → DENY."""
        principal = _make_principal()
        result = await auth_service.authorize(
            principal=principal,
            action="upload",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_A],
            resource_lifecycle="quarantined",
        )
        assert result.decision == AuthorizationDecision.DENY
        assert result.reason == "lifecycle_incompatible_write"

    async def test_action_not_permitted_deny(
        self,
        auth_service: AuthorizationService,
    ) -> None:
        """Action not in any role's permitted_actions → DENY."""
        principal = _make_principal()
        result = await auth_service.authorize(
            principal=principal,
            action="delete",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_A],
            resource_lifecycle="completed",
        )
        assert result.decision == AuthorizationDecision.DENY
        assert result.reason == "action_not_permitted"

    async def test_write_denial_uses_deny_not_not_found(
        self,
        auth_service: AuthorizationService,
    ) -> None:
        """Write action denial uses DENY (403), not NOT_FOUND (404)."""
        principal = _make_principal()
        result = await auth_service.authorize(
            principal=principal,
            action="upload",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_C],
            resource_lifecycle="completed",
        )
        assert result.decision == AuthorizationDecision.DENY


class TestAuthorizeTombstoneAndHold:
    """Tests for tombstone and legal hold checks."""

    async def test_tombstoned_resource_deny(
        self,
        auth_service: AuthorizationService,
        mock_session_factory: MagicMock,
    ) -> None:
        """Tombstoned resource → DENY."""
        # Make the session return a tombstone hit.
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = uuid.uuid4()
        session.execute.return_value = result_mock
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory.return_value = session

        principal = _make_principal()
        result = await auth_service.authorize(
            principal=principal,
            action="read",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_A],
            resource_lifecycle="completed",
        )
        assert result.decision == AuthorizationDecision.NOT_FOUND
        assert result.reason == "resource_tombstoned"

    async def test_legal_hold_blocks_deletion(
        self,
        mock_policy_service: AsyncMock,
        mock_label_service: AsyncMock,
        mock_audit_service: AsyncMock,
    ) -> None:
        """Active legal hold blocks delete action → DENY."""
        # First call (tombstone check) returns None, second call (hold check) returns a hold.
        session = AsyncMock()
        call_count = 0

        async def _mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count <= 1:
                result.scalar_one_or_none.return_value = None  # No tombstone
            else:
                result.scalar_one_or_none.return_value = uuid.uuid4()  # Has hold
            return result

        session.execute = _mock_execute
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=session)

        # Add delete to permitted actions.
        mock_policy_service.get_role_mappings.return_value = [
            _make_role_mapping(permitted_actions={"read", "delete"}),
        ]

        svc = AuthorizationService(
            policy_service=mock_policy_service,
            label_service=mock_label_service,
            audit_service=mock_audit_service,
            session_factory=factory,
        )

        principal = _make_principal()
        result = await svc.authorize(
            principal=principal,
            action="delete",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_A],
            resource_lifecycle="completed",
        )
        assert result.decision == AuthorizationDecision.DENY
        assert result.reason == "legal_hold_active"


class TestAuditEmission:
    """Every authorization decision must emit an audit event."""

    async def test_allow_emits_audit(
        self,
        auth_service: AuthorizationService,
        mock_audit_service: AsyncMock,
    ) -> None:
        result = await auth_service.authorize(
            principal=_make_principal(),
            action="read",
            resource_type="document",
            resource_id=uuid.uuid4(),
            resource_labels=[LABEL_A],
            resource_lifecycle="completed",
        )
        assert result.decision == AuthorizationDecision.ALLOW
        mock_audit_service.write_event.assert_called_once()
        entry = mock_audit_service.write_event.call_args[0][0]
        assert entry.action == "authorization.allow"

    async def test_deny_emits_audit(
        self,
        auth_service: AuthorizationService,
        mock_audit_service: AsyncMock,
    ) -> None:
        result = await auth_service.authorize(
            principal=_make_principal(active=False),
            action="read",
            resource_type="document",
        )
        assert result.decision == AuthorizationDecision.NOT_FOUND
        mock_audit_service.write_event.assert_called_once()
        entry = mock_audit_service.write_event.call_args[0][0]
        assert entry.action == "authorization.not_found"

    async def test_audit_write_failure_fails_closed(
        self,
        auth_service: AuthorizationService,
        mock_audit_service: AsyncMock,
    ) -> None:
        mock_audit_service.write_event.side_effect = RuntimeError("audit database unavailable")

        with pytest.raises(RuntimeError, match="audit database unavailable"):
            await auth_service.authorize(
                principal=_make_principal(),
                action="read",
                resource_type="document",
                resource_id=uuid.uuid4(),
                resource_labels=[LABEL_A],
                resource_lifecycle="completed",
            )
