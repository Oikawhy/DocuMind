"""Logical-role LiteLLM boundary for safe, auditable model invocations.

Enforces §4.4 role limits, resolves routes and short-lived credentials,
supports structured JSON output with bounded repair, and emits only
content-free audit data.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


class ModelRole(StrEnum):
    """Logical roles with §4.4 hard limits."""

    KEYWORDS = "KEYWORDS"
    EXTRACT = "EXTRACT"
    QUERY = "QUERY"
    VLM = "VLM"


@dataclass(frozen=True)
class RoleLimits:
    """Non-negotiable safety limits for a logical role."""

    temperature: float
    max_output_tokens: int
    timeout_seconds: int
    enabled: bool = True


#: §4.4 role limits — callers cannot override these.
ROLE_LIMITS: dict[ModelRole, RoleLimits] = {
    ModelRole.KEYWORDS: RoleLimits(temperature=0.0, max_output_tokens=256, timeout_seconds=10),
    ModelRole.EXTRACT: RoleLimits(temperature=0.0, max_output_tokens=4096, timeout_seconds=60),
    ModelRole.QUERY: RoleLimits(temperature=0.3, max_output_tokens=2048, timeout_seconds=30),
    ModelRole.VLM: RoleLimits(temperature=0.0, max_output_tokens=4096, timeout_seconds=60, enabled=False),
}

#: External cloud providers that require a secret_reference.
_CLOUD_PROVIDERS = frozenset({"openai", "anthropic", "azure", "cohere", "google"})


@dataclass(frozen=True)
class ModelRoute:
    """Resolved, validated route configuration for a single invocation."""

    revision_id: uuid.UUID
    role: ModelRole
    provider_kind: str
    model_digest: str
    model_alias: str
    timeout_seconds: int
    max_attempts: int
    external_consent_id: uuid.UUID | None = None
    secret_reference: str | None = None


class ModelRouteError(RuntimeError):
    """A route could not be resolved, was malformed, or was denied."""


@dataclass(frozen=True)
class ProviderResponse:
    """The raw provider response stripped of credentials and content detail."""

    content: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class StructuredOutputResult:
    """Result of structured output parsing with validation evidence."""

    parsed: dict[str, Any]
    valid: bool
    repair_attempted: bool = False
    repair_succeeded: bool = False


@dataclass(frozen=True)
class LLMResult:
    """The auditable result of a model invocation."""

    content: str
    input_tokens: int
    output_tokens: int
    route_revision_id: uuid.UUID
    model_digest: str
    model_alias: str
    latency_ms: float
    structured: StructuredOutputResult | None = None
    audit_record: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class RouteResolver(Protocol):
    """Resolve the newest active model route revision for a role."""

    async def newest_active(self, role: ModelRole) -> ModelRoute | None:
        """Return the newest active route, or ``None`` if none is active."""


class CredentialResolver(Protocol):
    """Resolve a short-lived credential from a secret reference."""

    async def resolve(self, secret_reference: str) -> str:
        """Return the credential string. Caller clears after use."""


class LLMAdapter(Protocol):
    """Async adapter over the LiteLLM completion call."""

    async def invoke(self, request: LLMRequest) -> ProviderResponse:
        """Send the request to the provider and return a response."""


class AuditSink(Protocol):
    """Persist content-free audit data for model invocations."""

    async def record(self, audit_data: dict[str, Any]) -> None:
        """Write a content-free audit record."""


# ---------------------------------------------------------------------------
# Request value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    """Fully resolved request ready for the LiteLLM adapter."""

    route_revision_id: uuid.UUID
    role: ModelRole
    provider_kind: str
    model_digest: str
    model_alias: str
    messages: list[dict[str, str]]
    temperature: float
    max_output_tokens: int
    timeout_seconds: int
    credential: str | None = None
    json_schema: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LLMService:
    """Enforce role limits, resolve routes, call LLM, and emit audit data.

    No raw credential, prompt, document content, provider response, or
    endpoint is written to logs or audit details.
    """

    def __init__(
        self,
        *,
        route_resolver: RouteResolver,
        adapter: LLMAdapter,
        credential_resolver: CredentialResolver | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._route_resolver = route_resolver
        self._adapter = adapter
        self._credential_resolver = credential_resolver
        self._audit_sink = audit_sink

    async def invoke(
        self,
        role: ModelRole,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Invoke a model under role limits with full audit trail."""
        # 1. Check role is enabled
        limits = ROLE_LIMITS[role]
        if not limits.enabled:
            raise ModelRouteError(f"Model role {role.value} is disabled by default.")

        # 2. Resolve route
        route = await self._route_resolver.newest_active(role)
        if route is None:
            raise ModelRouteError(f"No active model route for role {role.value}.")

        # 3. Validate route
        self._validate_route(route, role)

        # 4. Validate external consent for external routes
        if route.secret_reference and route.external_consent_id is None:
            raise ModelRouteError(f"External route for role {role.value} requires active consent.")

        # 5. Resolve credential if needed
        credential: str | None = None
        if route.secret_reference:
            if self._credential_resolver is None:
                raise ModelRouteError("Route requires a credential but no credential resolver is configured.")
            credential = await self._credential_resolver.resolve(route.secret_reference)

        # 6. Build request with role limits (not route overrides)
        request = LLMRequest(
            route_revision_id=route.revision_id,
            role=role,
            provider_kind=route.provider_kind,
            model_digest=route.model_digest,
            model_alias=route.model_alias,
            messages=messages,
            temperature=limits.temperature,
            max_output_tokens=limits.max_output_tokens,
            timeout_seconds=limits.timeout_seconds,
            credential=credential,
            json_schema=json_schema,
        )

        # 7. Call the adapter with retry loop using max_attempts
        start_time = time.monotonic()
        safe_error_class: str | None = None
        response: ProviderResponse | None = None
        last_exc: Exception | None = None

        for attempt in range(max(1, route.max_attempts)):
            try:
                response = await self._adapter.invoke(request)
                last_exc = None
                break
            except Exception as exc:
                safe_error_class = type(exc).__name__
                last_exc = exc
                if attempt >= route.max_attempts - 1:
                    break

        latency_ms = (time.monotonic() - start_time) * 1000

        # Clear credential from local state immediately
        credential = None  # noqa: F841

        if last_exc is not None or response is None:
            # Emit audit on error path before raising
            if self._audit_sink is not None:
                error_audit = {
                    "route_revision_id": str(route.revision_id),
                    "model_digest": route.model_digest,
                    "model_alias": route.model_alias,
                    "consent_id": str(route.external_consent_id) if route.external_consent_id else None,
                    "input_hash": hashlib.sha256(str(messages).encode("utf-8")).hexdigest(),
                    "output_hash": None,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": round(latency_ms, 2),
                    "safe_error_class": safe_error_class,
                }
                await self._audit_sink.record(error_audit)
            # T5.4-05: Wrap provider exceptions in ModelRouteError.
            if last_exc is not None:
                raise ModelRouteError(
                    f"Model invocation failed for role {role.value}: {type(last_exc).__name__}"
                ) from last_exc
            raise ModelRouteError(f"Adapter returned no response for role {role.value}.")

        # 8. Handle structured output
        structured: StructuredOutputResult | None = None
        if json_schema is not None:
            structured = self._parse_structured_output(
                response.content,
                json_schema,
            )
            if not structured.valid:
                # One bounded repair attempt — include credential
                repair_messages = messages + [
                    {"role": "assistant", "content": response.content},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON matching the schema. "
                            "Please respond with valid JSON only."
                        ),
                    },
                ]
                repair_request = LLMRequest(
                    route_revision_id=route.revision_id,
                    role=role,
                    provider_kind=route.provider_kind,
                    model_digest=route.model_digest,
                    model_alias=route.model_alias,
                    messages=repair_messages,
                    temperature=limits.temperature,
                    max_output_tokens=limits.max_output_tokens,
                    timeout_seconds=limits.timeout_seconds,
                    credential=credential,
                    json_schema=json_schema,
                )
                try:
                    repair_response = await self._adapter.invoke(repair_request)
                    repair_structured = self._parse_structured_output(
                        repair_response.content,
                        json_schema,
                    )
                    if repair_structured.valid:
                        structured = StructuredOutputResult(
                            parsed=repair_structured.parsed,
                            valid=True,
                            repair_attempted=True,
                            repair_succeeded=True,
                        )
                        response = repair_response
                    else:
                        structured = StructuredOutputResult(
                            parsed={},
                            valid=False,
                            repair_attempted=True,
                            repair_succeeded=False,
                        )
                except Exception:
                    structured = StructuredOutputResult(
                        parsed={},
                        valid=False,
                        repair_attempted=True,
                        repair_succeeded=False,
                    )
                if not structured.valid:
                    # T5.4-06: Emit audit before raising on structured-output exhaustion.
                    exhaustion_audit = {
                        "route_revision_id": str(route.revision_id),
                        "model_digest": route.model_digest,
                        "model_alias": route.model_alias,
                        "consent_id": str(route.external_consent_id) if route.external_consent_id else None,
                        "input_hash": hashlib.sha256(str(messages).encode("utf-8")).hexdigest(),
                        "output_hash": hashlib.sha256(response.content.encode("utf-8")).hexdigest(),
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                        "latency_ms": round(latency_ms, 2),
                        "safe_error_class": "StructuredOutputExhausted",
                    }
                    if self._audit_sink is not None:
                        await self._audit_sink.record(exhaustion_audit)
                    raise ModelRouteError(f"Structured output exhausted after repair for role {role.value}.")

        # 9. Build content-free audit record
        input_hash = hashlib.sha256(
            str(messages).encode("utf-8"),
        ).hexdigest()
        output_hash = hashlib.sha256(
            response.content.encode("utf-8"),
        ).hexdigest()
        audit_record = {
            "route_revision_id": str(route.revision_id),
            "model_digest": route.model_digest,
            "model_alias": route.model_alias,
            "consent_id": str(route.external_consent_id) if route.external_consent_id else None,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "latency_ms": round(latency_ms, 2),
            "safe_error_class": safe_error_class,
        }

        # 10. Emit audit
        if self._audit_sink is not None:
            await self._audit_sink.record(audit_record)

        return LLMResult(
            content=response.content,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            route_revision_id=route.revision_id,
            model_digest=route.model_digest,
            model_alias=route.model_alias,
            latency_ms=latency_ms,
            structured=structured,
            audit_record=audit_record,
        )

    @staticmethod
    def _validate_route(route: ModelRoute, role: ModelRole) -> None:
        """Validate route configuration before use."""
        if route.role != role:
            raise ModelRouteError(f"Route role {route.role.value} does not match requested role {role.value}.")
        if not route.model_digest:
            raise ModelRouteError("Route has no model digest.")
        if not route.model_alias:
            raise ModelRouteError("Route has no model alias.")
        if route.timeout_seconds <= 0:
            raise ModelRouteError("Route timeout must be positive.")
        if route.max_attempts <= 0:
            raise ModelRouteError("Route max_attempts must be positive.")
        # T5.4-02: External cloud providers must have a secret_reference.
        if route.provider_kind in _CLOUD_PROVIDERS and not route.secret_reference:
            raise ModelRouteError("External provider route requires a secret reference.")

    @staticmethod
    def _parse_structured_output(
        content: str,
        json_schema: dict[str, Any],
    ) -> StructuredOutputResult:
        """Parse JSON content and validate against the supplied schema.

        T5.4-03: Uses ``jsonschema`` for Draft 2020-12 validation instead of
        hand-written property/type checks.
        """
        import json as json_module

        try:
            parsed = json_module.loads(content)
        except (json_module.JSONDecodeError, ValueError):
            return StructuredOutputResult(parsed={}, valid=False)

        if not isinstance(parsed, dict):
            return StructuredOutputResult(parsed={}, valid=False)

        try:
            import jsonschema

            jsonschema.validate(parsed, json_schema)
        except ImportError:
            # Fallback: basic required-key check if jsonschema not installed
            required_keys = json_schema.get("required", [])
            if required_keys:
                missing = [key for key in required_keys if key not in parsed]
                if missing:
                    return StructuredOutputResult(parsed=parsed, valid=False)
        except jsonschema.ValidationError:
            return StructuredOutputResult(parsed=parsed, valid=False)

        return StructuredOutputResult(parsed=parsed, valid=True)
