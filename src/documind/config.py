"""Application configuration via non-secret values and OpenBao references."""

from collections.abc import Mapping
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """DocuMind self-hosted application settings.

    Secret values are deliberately absent from this model.  Configuration may
    provide an OpenBao reference, which a later secret service resolves at
    runtime under a scoped identity.
    """

    model_config = SettingsConfigDict(env_prefix="DOCUMIND_", env_file=".env", extra="ignore")

    # App
    app_name: str = "DocuMind"
    debug: bool = False
    api_v1_prefix: str = "/v1"
    release_digest: str = "unavailable"
    migration_level: str = "unavailable"

    # Database (PostgreSQL 16)
    database_url_ref: str = ""

    # Redis 7. URLs include credentials, so only OpenBao references may name
    # them.  The secret resolver is introduced with the data-service clients.
    redis_url_ref: str = ""
    redis_streams_url_ref: str = ""

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key_ref: str = ""
    minio_secret_key_ref: str = ""
    minio_secure: bool = False
    minio_bucket: str = "documind-documents"

    # HMAC key material is resolved from OpenBao before the document service
    # is constructed; opaque list cursors are never merely base64-encoded.
    cursor_hmac_key_ref: str = ""

    # Qdrant
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_collection: str = "documind_chunks"

    # OpenSearch
    opensearch_host: str = "localhost"
    opensearch_port: int = 9200
    opensearch_index: str = "documind_chunks"

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_auth_ref: str = ""

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "documind"

    # OpenBao
    openbao_addr: str = "http://localhost:8200"
    openbao_auth_ref: str = ""

    # OIDC
    oidc_issuer: str = ""
    oidc_audience: str = "documind"
    oidc_jwks_cache_ttl: int = 3600
    oidc_clock_skew_seconds: int = 30

    # Model routes
    litellm_proxy_url: str = "http://localhost:4000"
    default_model_role_keywords: str = "local/qwen2.5-7b-instruct"
    default_model_role_extract: str = "local/qwen2.5-7b-instruct"
    default_model_role_query: str = "local/qwen2.5-7b-instruct"

    # Upload limits (§5)
    upload_default_max_bytes: int = 104857600
    upload_hard_cap_bytes: int = 524288000

    # Retrieval limits (§6)
    retrieval_max_candidates: int = 100
    retrieval_max_evidence: int = 10
    retrieval_reranker_threshold: float = 0.10
    retrieval_rrf_constant: int = 60
    retrieval_budget_ms: int = 2500

    # Chat memory (§7.1)
    chat_enabled: bool = False
    chat_history_window: int = 20
    chat_history_max_tokens: int = 4096
    chat_compaction_threshold: int = 30
    chat_retention_days: int = 30

    # Observability
    otel_exporter_endpoint: str = "http://localhost:4317"
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key_ref: str = ""
    langfuse_secret_key_ref: str = ""

    # ClamAV
    clamav_host: str = "localhost"
    clamav_port: int = 3310

    # Worker parser sandbox command. Credentials remain OpenBao-resolved; this
    # is only the fixed no-network executable path and arguments.
    docling_sandbox_command: str = ""

    # SCIM provisioning
    scim_bearer_token: str = ""

    @classmethod
    def from_precedence_layers(
        cls,
        *,
        signed_profile_values: Mapping[str, Any] | None = None,
        customer_values: Mapping[str, Any] | None = None,
        openbao_references: Mapping[str, Any] | None = None,
        emergency_overrides: Mapping[str, Any] | None = None,
    ) -> "Settings":
        """Build settings from admitted layers in architecture precedence order."""
        reference_values = dict(openbao_references or {})
        invalid_references = {
            key: value
            for key, value in reference_values.items()
            if key not in cls.model_fields
            or not key.endswith("_ref")
            or not isinstance(value, str)
            or not value.startswith("openbao://")
        }
        if invalid_references:
            names = ", ".join(sorted(invalid_references))
            raise ValueError(f"OpenBao reference layer contains invalid values: {names}")

        merged: dict[str, Any] = {
            name: field.get_default(call_default_factory=True) for name, field in cls.model_fields.items()
        }
        for layer in (
            signed_profile_values,
            customer_values,
            reference_values,
            emergency_overrides,
        ):
            if layer:
                merged.update(layer)
        return cls(_env_file=None, **merged)


settings = Settings()
