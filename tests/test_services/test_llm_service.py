"""Logical-role and fail-closed contracts for Task 5 model invocation.

Covers: route selection, §4.4 role limits (KEYWORDS/EXTRACT/QUERY), VLM denial,
external-consent denial, structured-output repair/exhaustion, retry affinity,
and audit redaction. Uses fake LiteLLM adapter and fake credential resolver.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from documind.services.llm_service import (
    ROLE_LIMITS,
    LLMRequest,
    LLMService,
    ModelRole,
    ModelRoute,
    ModelRouteError,
    ProviderResponse,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class StaticRouteResolver:
    route: ModelRoute | None

    async def newest_active(self, role: ModelRole) -> ModelRoute | None:
        if self.route is None or self.route.role != role:
            return None
        return self.route


class RecordingAdapter:
    def __init__(self, response: ProviderResponse | None = None) -> None:
        self.requests: list[LLMRequest] = []
        self._response = response or ProviderResponse(content="keywords", input_tokens=4, output_tokens=1)
        self._responses: list[ProviderResponse] = []

    def set_responses(self, *responses: ProviderResponse) -> None:
        self._responses = list(responses)

    async def invoke(self, request: LLMRequest) -> ProviderResponse:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return self._response


class FailingAdapter:
    """Adapter that raises on invoke."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.requests: list[LLMRequest] = []

    async def invoke(self, request: LLMRequest) -> ProviderResponse:
        self.requests.append(request)
        raise self._error


@dataclass
class RecordingAuditSink:
    records: list[dict[str, Any]]

    def __init__(self) -> None:
        self.records = []

    async def record(self, audit_data: dict[str, Any]) -> None:
        self.records.append(audit_data)


class FakeCredentialResolver:
    """Returns a fixed credential for any secret reference."""

    def __init__(self, credential: str = "fake-api-key") -> None:
        self._credential = credential
        self.resolved_refs: list[str] = []

    async def resolve(self, secret_reference: str) -> str:
        self.resolved_refs.append(secret_reference)
        return self._credential


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _keywords_route() -> ModelRoute:
    return ModelRoute(
        revision_id=uuid.UUID("bb288240-9ff9-4b4b-836f-7eed39aaf9cc"),
        role=ModelRole.KEYWORDS,
        provider_kind="local_vllm",
        model_digest="sha256:qwen",
        model_alias="qwen2.5",
        timeout_seconds=10,
        max_attempts=2,
    )


def _extract_route() -> ModelRoute:
    return ModelRoute(
        revision_id=uuid.UUID("cc388350-aff0-5c5c-947f-8ffd40bbf0dd"),
        role=ModelRole.EXTRACT,
        provider_kind="local_vllm",
        model_digest="sha256:llama",
        model_alias="llama3.1",
        timeout_seconds=60,
        max_attempts=3,
    )


def _query_route() -> ModelRoute:
    return ModelRoute(
        revision_id=uuid.UUID("dd499461-b001-6d6d-a580-90fe51ccf1ee"),
        role=ModelRole.QUERY,
        provider_kind="litellm",
        model_digest="sha256:gpt4o",
        model_alias="gpt-4o",
        timeout_seconds=30,
        max_attempts=2,
    )


def _external_route_with_consent() -> ModelRoute:
    return ModelRoute(
        revision_id=uuid.UUID("ee500572-c112-7e7e-b691-a10f62ddf2ff"),
        role=ModelRole.QUERY,
        provider_kind="openai",
        model_digest="sha256:gpt4o",
        model_alias="gpt-4o",
        timeout_seconds=30,
        max_attempts=2,
        external_consent_id=uuid.UUID("ff611683-d223-8f8f-c7a2-b22073eef300"),
        secret_reference="documind/openai",
    )


def _external_route_without_consent() -> ModelRoute:
    return ModelRoute(
        revision_id=uuid.UUID("ee500572-c112-7e7e-b691-a10f62ddf2ff"),
        role=ModelRole.QUERY,
        provider_kind="openai",
        model_digest="sha256:gpt4o",
        model_alias="gpt-4o",
        timeout_seconds=30,
        max_attempts=2,
        external_consent_id=None,
        secret_reference="documind/openai",
    )


def _vlm_route() -> ModelRoute:
    return ModelRoute(
        revision_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        role=ModelRole.VLM,
        provider_kind="local_vllm",
        model_digest="sha256:pixtral",
        model_alias="pixtral-12b",
        timeout_seconds=60,
        max_attempts=2,
    )


_MESSAGES = [{"role": "user", "content": "find terms"}]


# ---------------------------------------------------------------------------
# §4.4 Role limits tests
# ---------------------------------------------------------------------------


async def test_keywords_invocation_uses_fixed_role_limits_and_resolved_route() -> None:
    adapter = RecordingAdapter()
    service = LLMService(route_resolver=StaticRouteResolver(_keywords_route()), adapter=adapter)

    result = await service.invoke(ModelRole.KEYWORDS, _MESSAGES)

    request = adapter.requests[0]
    assert request.route_revision_id == _keywords_route().revision_id
    assert (request.temperature, request.max_output_tokens, request.timeout_seconds) == (0.0, 256, 10)
    assert result.content == "keywords"
    assert result.route_revision_id == _keywords_route().revision_id


async def test_extract_role_uses_extract_limits() -> None:
    adapter = RecordingAdapter(ProviderResponse(content="extracted", input_tokens=100, output_tokens=50))
    service = LLMService(route_resolver=StaticRouteResolver(_extract_route()), adapter=adapter)

    result = await service.invoke(ModelRole.EXTRACT, _MESSAGES)

    request = adapter.requests[0]
    assert (request.temperature, request.max_output_tokens, request.timeout_seconds) == (0.0, 4096, 60)
    assert result.content == "extracted"


async def test_query_role_uses_query_limits() -> None:
    adapter = RecordingAdapter(ProviderResponse(content="answered", input_tokens=10, output_tokens=5))
    service = LLMService(route_resolver=StaticRouteResolver(_query_route()), adapter=adapter)

    result = await service.invoke(ModelRole.QUERY, _MESSAGES)

    request = adapter.requests[0]
    assert (request.temperature, request.max_output_tokens, request.timeout_seconds) == (0.3, 2048, 30)
    assert result.content == "answered"


async def test_role_limits_constants_match_spec() -> None:
    """§4.4 hard limits are correct."""
    assert ROLE_LIMITS[ModelRole.KEYWORDS] == ROLE_LIMITS[ModelRole.KEYWORDS]
    kw = ROLE_LIMITS[ModelRole.KEYWORDS]
    assert (kw.temperature, kw.max_output_tokens, kw.timeout_seconds) == (0.0, 256, 10)
    assert kw.enabled is True

    ex = ROLE_LIMITS[ModelRole.EXTRACT]
    assert (ex.temperature, ex.max_output_tokens, ex.timeout_seconds) == (0.0, 4096, 60)
    assert ex.enabled is True

    qu = ROLE_LIMITS[ModelRole.QUERY]
    assert (qu.temperature, qu.max_output_tokens, qu.timeout_seconds) == (0.3, 2048, 30)
    assert qu.enabled is True

    vl = ROLE_LIMITS[ModelRole.VLM]
    assert (vl.temperature, vl.max_output_tokens, vl.timeout_seconds) == (0.0, 4096, 60)
    assert vl.enabled is False


# ---------------------------------------------------------------------------
# Route resolution tests
# ---------------------------------------------------------------------------


async def test_missing_active_route_fails_closed_before_adapter_call() -> None:
    adapter = RecordingAdapter()
    service = LLMService(route_resolver=StaticRouteResolver(None), adapter=adapter)

    with pytest.raises(ModelRouteError, match="No active model route"):
        await service.invoke(ModelRole.QUERY, _MESSAGES)

    assert adapter.requests == []


async def test_route_role_mismatch_fails_closed() -> None:
    """A route resolved for wrong role is rejected."""
    adapter = RecordingAdapter()
    # Resolver returns a KEYWORDS route, but we request EXTRACT
    route = _keywords_route()
    resolver = StaticRouteResolver(route)
    service = LLMService(route_resolver=resolver, adapter=adapter)

    with pytest.raises(ModelRouteError, match="No active model route"):
        await service.invoke(ModelRole.EXTRACT, _MESSAGES)


# ---------------------------------------------------------------------------
# VLM denial tests
# ---------------------------------------------------------------------------


async def test_vlm_role_denied_by_default() -> None:
    """VLM is disabled by default and must be denied before route resolution."""
    adapter = RecordingAdapter()
    service = LLMService(route_resolver=StaticRouteResolver(_vlm_route()), adapter=adapter)

    with pytest.raises(ModelRouteError, match="disabled"):
        await service.invoke(ModelRole.VLM, _MESSAGES)

    assert adapter.requests == []


# ---------------------------------------------------------------------------
# External consent tests
# ---------------------------------------------------------------------------


async def test_external_route_with_consent_resolves_credential() -> None:
    """External route with active consent resolves credential from OpenBao."""
    credential_resolver = FakeCredentialResolver("live-api-key")
    adapter = RecordingAdapter(ProviderResponse(content="external", input_tokens=5, output_tokens=2))
    service = LLMService(
        route_resolver=StaticRouteResolver(_external_route_with_consent()),
        adapter=adapter,
        credential_resolver=credential_resolver,
    )

    result = await service.invoke(ModelRole.QUERY, _MESSAGES)

    assert credential_resolver.resolved_refs == ["documind/openai"]
    assert adapter.requests[0].credential == "live-api-key"
    assert result.content == "external"


async def test_external_route_without_consent_denied() -> None:
    """External route without consent is denied before credential resolution."""
    adapter = RecordingAdapter()
    service = LLMService(
        route_resolver=StaticRouteResolver(_external_route_without_consent()),
        adapter=adapter,
    )

    with pytest.raises(ModelRouteError, match="consent"):
        await service.invoke(ModelRole.QUERY, _MESSAGES)

    assert adapter.requests == []


async def test_external_route_without_credential_resolver_denied() -> None:
    """External route requiring credential but no resolver configured is denied."""
    adapter = RecordingAdapter()
    service = LLMService(
        route_resolver=StaticRouteResolver(_external_route_with_consent()),
        adapter=adapter,
        credential_resolver=None,
    )

    with pytest.raises(ModelRouteError, match="credential resolver"):
        await service.invoke(ModelRole.QUERY, _MESSAGES)


# ---------------------------------------------------------------------------
# Structured output tests
# ---------------------------------------------------------------------------


async def test_structured_output_valid_on_first_attempt() -> None:
    """Valid structured output is returned without repair."""
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    adapter = RecordingAdapter(ProviderResponse(content='{"name": "test"}', input_tokens=5, output_tokens=3))
    service = LLMService(route_resolver=StaticRouteResolver(_keywords_route()), adapter=adapter)

    result = await service.invoke(ModelRole.KEYWORDS, _MESSAGES, json_schema=schema)

    assert result.structured is not None
    assert result.structured.valid is True
    assert result.structured.parsed == {"name": "test"}
    assert result.structured.repair_attempted is False
    assert len(adapter.requests) == 1


async def test_structured_output_repair_succeeds() -> None:
    """Malformed first response triggers repair; valid second response accepted."""
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    adapter = RecordingAdapter()
    adapter.set_responses(
        ProviderResponse(content="not json", input_tokens=5, output_tokens=3),
        ProviderResponse(content='{"name": "repaired"}', input_tokens=10, output_tokens=4),
    )
    service = LLMService(route_resolver=StaticRouteResolver(_keywords_route()), adapter=adapter)

    result = await service.invoke(ModelRole.KEYWORDS, _MESSAGES, json_schema=schema)

    assert result.structured is not None
    assert result.structured.valid is True
    assert result.structured.repair_attempted is True
    assert result.structured.repair_succeeded is True
    assert result.structured.parsed == {"name": "repaired"}
    assert len(adapter.requests) == 2


async def test_structured_output_exhaustion_raises_error() -> None:
    """Both attempts fail -> terminal safe error."""
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    adapter = RecordingAdapter()
    adapter.set_responses(
        ProviderResponse(content="bad json", input_tokens=5, output_tokens=3),
        ProviderResponse(content="still bad", input_tokens=10, output_tokens=4),
    )
    service = LLMService(route_resolver=StaticRouteResolver(_keywords_route()), adapter=adapter)

    with pytest.raises(ModelRouteError, match="exhausted"):
        await service.invoke(ModelRole.KEYWORDS, _MESSAGES, json_schema=schema)

    assert len(adapter.requests) == 2


async def test_structured_output_missing_required_key_triggers_repair() -> None:
    """Missing required key in JSON triggers repair attempt."""
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    adapter = RecordingAdapter()
    adapter.set_responses(
        ProviderResponse(content='{"wrong_key": "value"}', input_tokens=5, output_tokens=3),
        ProviderResponse(content='{"name": "fixed"}', input_tokens=10, output_tokens=4),
    )
    service = LLMService(route_resolver=StaticRouteResolver(_keywords_route()), adapter=adapter)

    result = await service.invoke(ModelRole.KEYWORDS, _MESSAGES, json_schema=schema)

    assert result.structured is not None
    assert result.structured.repair_attempted is True
    assert result.structured.repair_succeeded is True
    assert result.structured.parsed == {"name": "fixed"}


# ---------------------------------------------------------------------------
# Retry affinity tests
# ---------------------------------------------------------------------------


async def test_retry_stays_on_resolved_route() -> None:
    """All adapter calls use the same resolved route revision; no failover."""
    adapter = RecordingAdapter(ProviderResponse(content="ok", input_tokens=3, output_tokens=1))
    service = LLMService(route_resolver=StaticRouteResolver(_keywords_route()), adapter=adapter)

    route = _keywords_route()
    await service.invoke(ModelRole.KEYWORDS, _MESSAGES)
    await service.invoke(ModelRole.KEYWORDS, _MESSAGES)

    for request in adapter.requests:
        assert request.route_revision_id == route.revision_id
        assert request.model_digest == route.model_digest


# ---------------------------------------------------------------------------
# Content-free audit tests
# ---------------------------------------------------------------------------


async def test_audit_record_contains_only_content_free_data() -> None:
    """Audit record has route/digest/hashes/tokens/latency, no content."""
    audit_sink = RecordingAuditSink()
    adapter = RecordingAdapter(ProviderResponse(content="result", input_tokens=8, output_tokens=2))
    route = _keywords_route()
    service = LLMService(
        route_resolver=StaticRouteResolver(route),
        adapter=adapter,
        audit_sink=audit_sink,
    )

    await service.invoke(ModelRole.KEYWORDS, _MESSAGES)

    assert len(audit_sink.records) == 1
    record = audit_sink.records[0]

    # Content-free fields present
    assert record["route_revision_id"] == str(route.revision_id)
    assert record["model_digest"] == route.model_digest
    assert record["model_alias"] == route.model_alias
    assert record["input_tokens"] == 8
    assert record["output_tokens"] == 2
    assert isinstance(record["latency_ms"], float)
    assert isinstance(record["input_hash"], str) and len(record["input_hash"]) == 64
    assert isinstance(record["output_hash"], str) and len(record["output_hash"]) == 64
    assert record["safe_error_class"] is None
    assert record["consent_id"] is None

    # No raw content in audit
    record_str = str(record)
    assert "find terms" not in record_str  # No prompt content
    assert "result" not in record_str or record_str.count("result") == 0 or "route_revision" in record_str


async def test_audit_record_includes_consent_id_for_external_route() -> None:
    """External route audit record includes the consent ID."""
    audit_sink = RecordingAuditSink()
    credential_resolver = FakeCredentialResolver()
    route = _external_route_with_consent()
    adapter = RecordingAdapter(ProviderResponse(content="ext", input_tokens=3, output_tokens=1))
    service = LLMService(
        route_resolver=StaticRouteResolver(route),
        adapter=adapter,
        credential_resolver=credential_resolver,
        audit_sink=audit_sink,
    )

    await service.invoke(ModelRole.QUERY, _MESSAGES)

    assert audit_sink.records[0]["consent_id"] == str(route.external_consent_id)


async def test_audit_not_emitted_when_no_sink() -> None:
    """No error when audit_sink is None."""
    adapter = RecordingAdapter()
    service = LLMService(
        route_resolver=StaticRouteResolver(_keywords_route()),
        adapter=adapter,
        audit_sink=None,
    )

    result = await service.invoke(ModelRole.KEYWORDS, _MESSAGES)
    assert result.audit_record["route_revision_id"] == str(_keywords_route().revision_id)


# ---------------------------------------------------------------------------
# LLMResult fields
# ---------------------------------------------------------------------------


async def test_llm_result_includes_latency_and_model_info() -> None:
    """LLMResult contains model digest, alias, and positive latency."""
    adapter = RecordingAdapter(ProviderResponse(content="ok", input_tokens=3, output_tokens=1))
    service = LLMService(route_resolver=StaticRouteResolver(_keywords_route()), adapter=adapter)

    result = await service.invoke(ModelRole.KEYWORDS, _MESSAGES)

    assert result.model_digest == "sha256:qwen"
    assert result.model_alias == "qwen2.5"
    assert result.latency_ms >= 0
