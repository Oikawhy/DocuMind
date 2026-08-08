"""Fail-closed OIDC authentication middleware per §4.1.

Every request except ``/health`` and ``/scim/v2/*`` must carry a valid
``Authorization: Bearer <token>`` header.  The middleware validates the
token via ``IdentityService.validate_oidc_token`` and injects the
resulting ``Principal`` into ``request.state.principal``.

On any failure the middleware returns a JSON 401 with
``AUTHENTICATION_REQUIRED`` or ``TOKEN_INVALID`` — it never falls
through without a verified principal.
"""

from __future__ import annotations

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from documind.domain.errors import AuthenticationError

logger = structlog.get_logger()

# Paths that are exempt from OIDC authentication.
_EXEMPT_PREFIXES = ("/health", "/scim/v2", "/docs", "/openapi.json")


def _is_exempt(path: str) -> bool:
    """Check if a request path is exempt from OIDC authentication."""
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _error_response(code: str, message: str, status: int = 401) -> JSONResponse:
    """Build a safe JSON error envelope matching §9.1."""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "trace_id": None,
                "details": [],
            }
        },
    )


async def oidc_middleware(request: Request, call_next: object) -> Response:
    """Validate OIDC bearer tokens on all non-exempt endpoints.

    This replaces the stub ``oidc_stub`` from the bootstrap ``main.py``.
    """
    if _is_exempt(request.url.path):
        return await call_next(request)  # type: ignore[operator]

    # Extract bearer token.
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        return _error_response("AUTHENTICATION_REQUIRED", "Authorization header is required.")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return _error_response("AUTHENTICATION_REQUIRED", "Bearer token is required.")

    token = parts[1]
    if not token:
        return _error_response("AUTHENTICATION_REQUIRED", "Bearer token is empty.")

    # Validate via IdentityService.
    identity_service = getattr(request.app.state, "identity_service", None)
    if identity_service is None:
        # Service not wired — fail closed.
        await logger.aerror("oidc_middleware_no_identity_service")
        return _error_response(
            "AUTHENTICATION_REQUIRED",
            "Identity service is not available.",
            status=503,
        )

    try:
        principal = await identity_service.validate_oidc_token(token)
    except AuthenticationError as exc:
        return _error_response(exc.code, exc.message)
    except Exception as exc:
        await logger.aerror("oidc_middleware_unexpected_error", error=str(exc))
        return _error_response("AUTHENTICATION_REQUIRED", "Authentication failed.")

    # Inject principal into request state for downstream handlers.
    request.state.principal = principal
    return await call_next(request)  # type: ignore[operator]
