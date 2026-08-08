"""Domain error hierarchy for DocuMind.

Every domain error carries a machine-readable ``code`` and a safe
``message`` that may be returned to API callers without leaking
internal details.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain-layer errors."""

    code: str = "DOMAIN_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message


class AuthenticationError(DomainError):
    """OIDC token validation or identity verification failure (HTTP 401)."""

    code = "AUTHENTICATION_REQUIRED"

    def __init__(self, message: str = "Authentication is required.", *, code: str | None = None) -> None:
        super().__init__(message, code=code or "AUTHENTICATION_REQUIRED")


class AuthorizationDeniedError(DomainError):
    """Deterministic authorization deny (HTTP 404 for reads, 403 for writes).

    Per §4.2 the server returns a 404-equivalent for read denials so that
    resource existence is never disclosed to unauthorized callers.
    """

    code = "DOCUMENT_NOT_FOUND"

    def __init__(
        self,
        message: str = "The requested resource is not available.",
        *,
        code: str | None = None,
        use_404: bool = True,
    ) -> None:
        super().__init__(message, code=code or ("DOCUMENT_NOT_FOUND" if use_404 else "ACTION_NOT_PERMITTED"))
        self.use_404 = use_404


class PolicyUnavailableError(DomainError):
    """No active policy revision is available — fail-closed (HTTP 503)."""

    code = "AUTHORIZATION_UNAVAILABLE"

    def __init__(self, message: str = "Authorization policy is temporarily unavailable.") -> None:
        super().__init__(message, code="AUTHORIZATION_UNAVAILABLE")


class LabelValidationError(DomainError):
    """Label IDs are missing, inactive, or not permitted for the caller (HTTP 422)."""

    code = "LABEL_INVALID"

    def __init__(self, message: str = "One or more labels are invalid or not permitted.") -> None:
        super().__init__(message, code="LABEL_INVALID")


class ResourceConflictError(DomainError):
    """Idempotency key, revision, or legal-hold conflict (HTTP 409)."""

    code = "VERSION_CONFLICT"

    def __init__(self, message: str = "A conflicting operation is in progress.", *, code: str | None = None) -> None:
        super().__init__(message, code=code or "VERSION_CONFLICT")


class UploadTooLargeError(DomainError):
    """The streaming upload crossed the configured admission limit (HTTP 413)."""

    code = "UPLOAD_TOO_LARGE"

    def __init__(self, message: str = "The uploaded file exceeds the permitted size.") -> None:
        super().__init__(message, code="UPLOAD_TOO_LARGE")


class InvalidRequestError(DomainError):
    """Admission input is syntactically invalid or unsafe (HTTP 400)."""

    code = "INVALID_REQUEST"

    def __init__(self, message: str = "The request is invalid.", *, code: str | None = None) -> None:
        super().__init__(message, code=code or "INVALID_REQUEST")


class ResourceNotFoundError(DomainError):
    """A resource is absent or must not be disclosed (HTTP 404)."""

    code = "DOCUMENT_NOT_FOUND"

    def __init__(self, message: str = "The requested resource is not available.", *, code: str | None = None) -> None:
        super().__init__(message, code=code or "DOCUMENT_NOT_FOUND")


class ChunkProfileValidationError(DomainError):
    """No active, permitted chunk profile can be selected (HTTP 422)."""

    code = "CHUNK_PROFILE_INVALID"

    def __init__(self, message: str = "The selected chunk profile is invalid.") -> None:
        super().__init__(message, code="CHUNK_PROFILE_INVALID")


class TemplateResolutionError(DomainError):
    """The configured extraction template revision is inactive or mismatched (HTTP 422)."""

    code = "TEMPLATE_RESOLUTION_INVALID"

    def __init__(self, message: str = "The configured extraction template is unavailable.") -> None:
        super().__init__(message, code="TEMPLATE_RESOLUTION_INVALID")


class UploadValidationError(DomainError):
    """An upload filename or admission field failed safe validation (HTTP 400)."""

    code = "INVALID_REQUEST"

    def __init__(self, message: str = "The upload request is invalid.") -> None:
        super().__init__(message, code="INVALID_REQUEST")


class SecretRetrievalError(DomainError):
    """OpenBao secret retrieval failure — fail-closed (HTTP 503)."""

    code = "DEPENDENCY_UNAVAILABLE"

    def __init__(self, message: str = "Secret retrieval failed.") -> None:
        super().__init__(message, code="DEPENDENCY_UNAVAILABLE")


class ChatDisabledError(DomainError):
    """Chat is disabled by configuration (HTTP 403)."""

    code = "CHAT_DISABLED"

    def __init__(self, message: str = "Chat is disabled.") -> None:
        super().__init__(message, code="CHAT_DISABLED")


class SSRFViolationError(DomainError):
    """Webhook target URL failed SSRF validation (HTTP 400)."""

    code = "SSRF_VIOLATION"

    def __init__(self, message: str = "The webhook target URL is not permitted.") -> None:
        super().__init__(message, code="SSRF_VIOLATION")
