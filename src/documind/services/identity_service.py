"""OIDC token validation and SCIM identity projection per §4.1.

The service validates customer OIDC access tokens against a cached JWK
set and projects SCIM 2.0 identity events into the local PostgreSQL
``identity_subject`` / ``identity_group_membership`` tables.

No password database, local refresh token, registration endpoint, or
product-owned identity provider exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.config import Settings
from documind.domain.errors import AuthenticationError
from documind.models.identity import IdentityGroupMembership, IdentitySubject

logger = structlog.get_logger()


@dataclass(frozen=True)
class Principal:
    """Verified caller identity extracted from a validated OIDC token.

    Injected into ``request.state.principal`` by the OIDC middleware.
    """

    subject: str
    display_name: str
    email: str | None
    groups: list[str]
    active: bool
    issuer: str
    token_claims: dict[str, Any] = field(default_factory=dict, repr=False)


class _JWKCache:
    """In-memory JWK set cache with TTL-based refresh and hard expiry.

    After ``max_staleness`` seconds without a successful refresh the
    cache is considered expired and ``is_expired()`` returns ``True``.
    This enforces fail-closed behaviour: an indefinitely stale cache
    must not keep accepting tokens.
    """

    def __init__(self, ttl_seconds: int, *, max_staleness_factor: int = 2) -> None:
        self._ttl = ttl_seconds
        self._max_staleness = ttl_seconds * max_staleness_factor
        self._jwk_client: jwt.PyJWKClient | None = None
        self._last_refresh: float = 0.0
        self._jwks_uri: str | None = None

    def is_stale(self) -> bool:
        return time.monotonic() - self._last_refresh > self._ttl

    def is_expired(self) -> bool:
        """True when the cache exceeds the hard staleness limit."""
        return time.monotonic() - self._last_refresh > self._max_staleness

    def set_jwks_uri(self, uri: str) -> None:
        if self._jwks_uri != uri:
            self._jwks_uri = uri
            self._jwk_client = jwt.PyJWKClient(uri, cache_keys=True)
            self._last_refresh = time.monotonic()

    def refresh(self) -> None:
        if self._jwks_uri:
            self._jwk_client = jwt.PyJWKClient(self._jwks_uri, cache_keys=True)
            self._last_refresh = time.monotonic()

    @property
    def client(self) -> jwt.PyJWKClient | None:
        return self._jwk_client


class IdentityService:
    """OIDC validation and SCIM identity projection."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._jwk_cache = _JWKCache(settings.oidc_jwks_cache_ttl)
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    # ------------------------------------------------------------------
    # OIDC token validation
    # ------------------------------------------------------------------

    async def _ensure_jwks(self) -> jwt.PyJWKClient:
        """Fetch or refresh the JWK set from the OIDC discovery endpoint."""
        issuer = self._settings.oidc_issuer
        if not issuer:
            raise AuthenticationError(
                "OIDC issuer is not configured.",
                code="TOKEN_INVALID",
            )

        # Discover jwks_uri from well-known endpoint.
        if self._jwk_cache.client is None or self._jwk_cache.is_stale():
            discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
            try:
                resp = await self._http.get(discovery_url)
                resp.raise_for_status()
                jwks_uri = resp.json()["jwks_uri"]
            except Exception as exc:
                # Fail closed: if we have no cached JWKs at all, reject.
                if self._jwk_cache.client is None:
                    await logger.aerror("oidc_discovery_failed", error=str(exc))
                    raise AuthenticationError(
                        "OIDC discovery is unavailable.",
                        code="TOKEN_INVALID",
                    ) from exc
                # Fail closed: if stale beyond hard limit, reject.
                if self._jwk_cache.is_expired():
                    await logger.aerror(
                        "oidc_jwks_cache_expired",
                        error=str(exc),
                        staleness_seconds=time.monotonic() - self._jwk_cache._last_refresh,
                    )
                    raise AuthenticationError(
                        "OIDC key material has expired.",
                        code="TOKEN_INVALID",
                    ) from exc
                # If stale but within max-staleness, log warning and continue.
                await logger.awarning("oidc_discovery_refresh_failed", error=str(exc))
            else:
                self._jwk_cache.set_jwks_uri(jwks_uri)

        client = self._jwk_cache.client
        if client is None:
            raise AuthenticationError(
                "JWK set is unavailable.",
                code="TOKEN_INVALID",
            )
        return client

    async def validate_oidc_token(self, token: str) -> Principal:
        """Validate an OIDC access token and return a ``Principal``.

        Checks: issuer, audience, signature (RS256/ES256), expiry,
        not-before, and configured clock skew.

        Raises:
            AuthenticationError: On any validation failure (fail-closed).
        """
        jwk_client = await self._ensure_jwks()

        try:
            signing_key = jwk_client.get_signing_key_from_jwt(token)
        except (jwt.exceptions.PyJWKClientError, jwt.exceptions.DecodeError) as exc:
            await logger.awarning("oidc_signing_key_lookup_failed", error=str(exc))
            raise AuthenticationError(
                "Token signature could not be verified.",
                code="TOKEN_INVALID",
            ) from exc

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                leeway=self._settings.oidc_clock_skew_seconds,
                options={
                    "require": ["exp", "iss", "aud", "sub"],
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                    "verify_nbf": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Token has expired.", code="TOKEN_INVALID") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationError("Token audience is invalid.", code="TOKEN_INVALID") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthenticationError("Token issuer is invalid.", code="TOKEN_INVALID") from exc
        except jwt.InvalidTokenError as exc:
            await logger.awarning("oidc_token_invalid", error=str(exc))
            raise AuthenticationError("Token is invalid.", code="TOKEN_INVALID") from exc

        subject = claims["sub"]

        # Load local identity — subject MUST exist in SCIM projection.
        async with self._session_factory() as session:
            identity = await session.get(IdentitySubject, subject)

        if identity is None:
            await logger.awarning("oidc_subject_not_provisioned", subject=subject)
            raise AuthenticationError(
                "Subject not provisioned.",
                code="TOKEN_INVALID",
            )

        if not identity.active:
            raise AuthenticationError(
                "Identity has been deactivated.",
                code="TOKEN_INVALID",
            )

        # Load authoritative groups from the local SCIM projection,
        # NOT from token claims.  Token claim groups are untrusted.
        groups = await self.get_subject_groups(subject)

        display_name = identity.display_name or claims.get("name") or subject
        email = identity.email or claims.get("email")

        principal = Principal(
            subject=subject,
            display_name=display_name,
            email=email,
            groups=groups,
            active=identity.active,
            issuer=claims["iss"],
            token_claims=claims,
        )

        await logger.ainfo("oidc_token_validated", subject=subject)
        return principal

    # ------------------------------------------------------------------
    # SCIM identity projection
    # ------------------------------------------------------------------

    async def process_scim_user_create(
        self,
        subject: str,
        display_name: str,
        email: str | None,
        groups: list[str],
    ) -> None:
        """Create or reactivate an identity subject with group memberships."""
        async with self._session_factory() as session, session.begin():
            existing = await session.get(IdentitySubject, subject)
            now = datetime.now(UTC)

            if existing is not None:
                existing.display_name = display_name
                existing.email = email
                existing.active = True
                existing.reconciled_at = now
                existing.updated_at = now
            else:
                session.add(
                    IdentitySubject(
                        subject=subject,
                        display_name=display_name,
                        email=email,
                        active=True,
                        reconciled_at=now,
                    )
                )

            # Replace group memberships.
            await session.execute(
                delete(IdentityGroupMembership).where(
                    IdentityGroupMembership.subject == subject,
                )
            )
            for group_key in groups:
                session.add(
                    IdentityGroupMembership(
                        subject=subject,
                        group_key=group_key,
                    )
                )

        await logger.ainfo("scim_user_created", subject=subject, groups=groups)

    async def process_scim_user_update(
        self,
        subject: str,
        *,
        active: bool | None = None,
        display_name: str | None = None,
        groups: list[str] | None = None,
    ) -> None:
        """Update an identity subject's fields and/or group memberships."""
        async with self._session_factory() as session, session.begin():
            identity = await session.get(IdentitySubject, subject)
            if identity is None:
                await logger.awarning("scim_user_update_not_found", subject=subject)
                return

            now = datetime.now(UTC)
            if active is not None:
                identity.active = active
            if display_name is not None:
                identity.display_name = display_name
            identity.reconciled_at = now
            identity.updated_at = now

            if groups is not None:
                await session.execute(
                    delete(IdentityGroupMembership).where(
                        IdentityGroupMembership.subject == subject,
                    )
                )
                for group_key in groups:
                    session.add(
                        IdentityGroupMembership(
                            subject=subject,
                            group_key=group_key,
                        )
                    )

        await logger.ainfo("scim_user_updated", subject=subject)

    async def process_scim_user_deactivate(self, subject: str) -> None:
        """Mark a subject inactive per §4.1 SCIM deactivation rules.

        Convergence target: one minute from delivered event.
        """
        async with self._session_factory() as session, session.begin():
            identity = await session.get(IdentitySubject, subject)
            if identity is None:
                await logger.awarning("scim_user_deactivate_not_found", subject=subject)
                return

            identity.active = False
            identity.updated_at = datetime.now(UTC)

        await logger.ainfo("scim_user_deactivated", subject=subject)

    async def get_subject_groups(self, subject: str) -> list[str]:
        """Return the group keys for a subject from the local projection."""
        async with self._session_factory() as session:
            stmt = select(IdentityGroupMembership.group_key).where(
                IdentityGroupMembership.subject == subject,
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def close(self) -> None:
        """Shut down the underlying HTTP client."""
        await self._http.aclose()
