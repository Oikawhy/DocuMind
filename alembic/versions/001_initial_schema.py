"""Initial schema: all tables, ENUMs, indexes, and audit partitions.

Revision ID: 001
Revises:
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ENUM types matching §8.1 DDL
_DOCUMENT_LIFECYCLE = postgresql.ENUM(
    "accepted",
    "quarantined",
    "processing",
    "completed",
    "failed",
    "erased",
    name="document_lifecycle",
    create_type=False,
)
_STAGE_STATUS = postgresql.ENUM(
    "queued",
    "running",
    "succeeded",
    "retrying",
    "failed",
    "cancelled",
    "skipped",
    name="stage_status",
    create_type=False,
)
_EXTRACTION_STATUS = postgresql.ENUM(
    "not_requested",
    "pending_template",
    "queued",
    "completed",
    "failed",
    name="extraction_status",
    create_type=False,
)
_POLICY_STATUS = postgresql.ENUM(
    "draft",
    "review",
    "active",
    "superseded",
    "retired",
    "rejected",
    name="policy_status",
    create_type=False,
)
_OPERATION_STATUS = postgresql.ENUM(
    "accepted",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    name="operation_status",
    create_type=False,
)


def upgrade() -> None:
    # --- ENUM types ---
    _DOCUMENT_LIFECYCLE.create(op.get_bind(), checkfirst=True)
    _STAGE_STATUS.create(op.get_bind(), checkfirst=True)
    _EXTRACTION_STATUS.create(op.get_bind(), checkfirst=True)
    _POLICY_STATUS.create(op.get_bind(), checkfirst=True)
    _OPERATION_STATUS.create(op.get_bind(), checkfirst=True)

    # --- Tables (dependency order) ---

    # policy_revision (no FKs to other app tables)
    op.create_table(
        "policy_revision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("policy_kind", sa.Text(), nullable=False),
        sa.Column("stable_key", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", _POLICY_STATUS, nullable=False),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("created_by_subject", sa.Text(), nullable=False),
        sa.Column("approved_by_subject", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("revision > 0", name="positive_revision"),
        sa.UniqueConstraint("policy_kind", "stable_key", "revision", name="uq_policy_revision"),
    )

    # declared_type
    op.create_table(
        "declared_type",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("stable_key", sa.Text(), nullable=False, unique=True),
        sa.Column("active_policy_revision_id", sa.Uuid(), sa.ForeignKey("policy_revision.id"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )

    # chunk_profile_revision
    op.create_table(
        "chunk_profile_revision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", _POLICY_STATUS, nullable=False),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("configuration_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("profile_id", "revision", name="uq_chunk_profile_revision"),
    )

    # extraction_template_revision
    op.create_table(
        "extraction_template_revision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("declared_type_id", sa.Uuid(), sa.ForeignKey("declared_type.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", _POLICY_STATUS, nullable=False),
        sa.Column("json_schema", postgresql.JSONB(), nullable=False),
        sa.Column("field_dictionary", postgresql.JSONB(), nullable=False),
        sa.Column("schema_sha256", sa.String(64), nullable=False),
        sa.Column("created_by_subject", sa.Text(), nullable=False),
        sa.Column("approved_by_subject", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("template_id", "revision", name="uq_template_revision"),
    )

    # model_route_revision
    op.create_table(
        "model_route_revision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", _POLICY_STATUS, nullable=False),
        sa.Column("route_configuration", postgresql.JSONB(), nullable=False),
        sa.Column("external_consent_revision_id", sa.Uuid(), sa.ForeignKey("policy_revision.id"), nullable=True),
        sa.Column("secret_reference", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role IN ('KEYWORDS', 'EXTRACT', 'QUERY', 'VLM')", name="valid_model_role"),
        sa.UniqueConstraint("role", "revision", name="uq_model_route_revision"),
    )

    # label
    op.create_table(
        "label",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("stable_key", sa.Text(), nullable=False, unique=True),
        sa.Column("parent_id", sa.Uuid(), sa.ForeignKey("label.id"), nullable=True),
        sa.Column("retention_class", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # document
    op.create_table(
        "document",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("declared_type_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_subject", sa.Text(), nullable=False),
        sa.Column("current_completed_version_id", sa.Uuid(), nullable=True),
        sa.Column("legal_hold_state", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("length(title) >= 1 AND length(title) <= 1024", name="title_length"),
        sa.CheckConstraint(
            "(erased_at IS NULL) OR (current_completed_version_id IS NULL)",
            name="erased_no_current_version",
        ),
    )

    # document_version
    op.create_table(
        "document_version",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("document.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("detected_mime_type", sa.Text(), nullable=True),
        sa.Column("declared_mime_family", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("quarantine_object_key", sa.Text(), nullable=False),
        sa.Column("accepted_object_key", sa.Text(), nullable=True),
        sa.Column("normalized_object_key", sa.Text(), nullable=True),
        sa.Column("lifecycle", _DOCUMENT_LIFECYCLE, nullable=False, server_default=sa.text("'accepted'")),
        sa.Column("extraction_state", _EXTRACTION_STATUS, nullable=False, server_default=sa.text("'not_requested'")),
        sa.Column("selected_chunk_profile_revision_id", sa.Uuid(), nullable=False),
        sa.Column("selected_template_revision_id", sa.Uuid(), nullable=True),
        sa.Column("parser_revision", sa.Text(), nullable=True),
        sa.Column("normalization_revision", sa.Text(), nullable=True),
        sa.Column("completed_projection_revision", sa.BigInteger(), nullable=True),
        sa.Column("tombstone_generation", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("failure_safe_message", sa.Text(), nullable=True),
        sa.Column("created_by_subject", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version_number > 0", name="positive_version_number"),
        sa.CheckConstraint("byte_size >= 0 AND byte_size <= 524288000", name="byte_size_range"),
        sa.UniqueConstraint("document_id", "version_number", name="uq_document_version_number"),
        sa.UniqueConstraint("document_id", "content_sha256", name="uq_document_content_sha256"),
    )

    # Deferred FKs for document circular refs
    op.create_foreign_key(
        "document_declared_type_fk",
        "document",
        "declared_type",
        ["declared_type_id"],
        ["id"],
    )
    op.create_foreign_key(
        "document_current_version_fk",
        "document",
        "document_version",
        ["current_completed_version_id"],
        ["id"],
    )
    op.create_foreign_key(
        "version_chunk_profile_fk",
        "document_version",
        "chunk_profile_revision",
        ["selected_chunk_profile_revision_id"],
        ["id"],
    )
    op.create_foreign_key(
        "version_template_fk",
        "document_version",
        "extraction_template_revision",
        ["selected_template_revision_id"],
        ["id"],
    )

    # document_label
    op.create_table(
        "document_label",
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("document.id"), primary_key=True),
        sa.Column("label_id", sa.Uuid(), sa.ForeignKey("label.id"), primary_key=True),
        sa.Column("assignment_policy_revision_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_by_subject", sa.Text(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_foreign_key(
        "label_assignment_policy_fk",
        "document_label",
        "policy_revision",
        ["assignment_policy_revision_id"],
        ["id"],
    )

    # legal_hold
    op.create_table(
        "legal_hold",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("document.id"), nullable=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("document_version.id"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("imposed_by_subject", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("imposed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(document_id IS NOT NULL) <> (version_id IS NOT NULL)",
            name="hold_scope_xor",
        ),
    )

    # deletion_tombstone
    op.create_table(
        "deletion_tombstone",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("document.id"), nullable=False),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("document_version.id"), nullable=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("tombstone_generation", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("sealed_object_key", sa.Text(), nullable=False),
        sa.Column("sealed_hash", sa.String(64), nullable=False),
        sa.Column("created_by_subject", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "scope IN ('document', 'version', 'chat', 'export')",
            name="valid_scope",
        ),
        sa.UniqueConstraint(
            "document_id",
            "version_id",
            "tombstone_generation",
            name="uq_tombstone_generation",
            postgresql_nulls_not_distinct=True,
        ),
    )

    # operation
    op.create_table(
        "operation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("operation_type", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("document.id"), nullable=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("document_version.id"), nullable=True),
        sa.Column("requested_by_subject", sa.Text(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", _OPERATION_STATUS, nullable=False, server_default=sa.text("'accepted'")),
        sa.Column("temporal_workflow_id", sa.Text(), nullable=True, unique=True),
        sa.Column("result_json", postgresql.JSONB(), nullable=True),
        sa.Column("safe_error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "requested_by_subject",
            "idempotency_key_hash",
            name="uq_operation_idempotency",
        ),
    )

    # processing_run
    op.create_table(
        "processing_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("document_version.id"), nullable=False),
        sa.Column("temporal_workflow_id", sa.Text(), nullable=False),
        sa.Column("temporal_run_id", sa.Text(), nullable=False),
        sa.Column("trigger_event_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "version_id",
            "temporal_workflow_id",
            "temporal_run_id",
            name="uq_processing_run_temporal",
        ),
    )

    # processing_stage
    op.create_table(
        "processing_stage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("processing_run_id", sa.Uuid(), sa.ForeignKey("processing_run.id"), nullable=False),
        sa.Column("stage_name", sa.Text(), nullable=False),
        sa.Column("stage_order", sa.SmallInteger(), nullable=False),
        sa.Column("status", _STAGE_STATUS, nullable=False, server_default=sa.text("'queued'")),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("output_sha256", sa.String(64), nullable=True),
        sa.Column("policy_revision_json", postgresql.JSONB(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("safe_error_code", sa.Text(), nullable=True),
        sa.Column("evidence_object_key", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("processing_run_id", "stage_name", name="uq_stage_name"),
        sa.UniqueConstraint("processing_run_id", "idempotency_key", name="uq_stage_idempotency"),
    )

    # outbox_event
    op.create_table(
        "outbox_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Text(), nullable=False),
        sa.Column("cloud_event", postgresql.JSONB(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("redis_stream_id", sa.Text(), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'published', 'failed')", name="valid_outbox_status"),
    )

    # dead_letter
    op.create_table(
        "dead_letter",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("processing_run_id", sa.Uuid(), sa.ForeignKey("processing_run.id"), nullable=True),
        sa.Column("stage_id", sa.Uuid(), sa.ForeignKey("processing_stage.id"), nullable=True),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("activity_name", sa.Text(), nullable=False),
        sa.Column("safe_error_class", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("retry_eligible", sa.Boolean(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('open', 'replaying', 'resolved', 'cancelled')", name="valid_dead_letter_state"),
    )

    # template_proposal
    op.create_table(
        "template_proposal",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("document_version.id"), nullable=False),
        sa.Column("proposed_declared_type_key", sa.Text(), nullable=False),
        sa.Column("candidate_json_schema", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("sample_source_spans", postgresql.JSONB(), nullable=False),
        sa.Column("model_route_revision_id", sa.Uuid(), nullable=False),
        sa.Column("state", _POLICY_STATUS, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by_subject", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_foreign_key(
        "proposal_model_route_fk",
        "template_proposal",
        "model_route_revision",
        ["model_route_revision_id"],
        ["id"],
    )

    # document_chunk
    op.create_table(
        "document_chunk",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("document_version.id"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("section_path", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("block_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("profile_revision_id", sa.Uuid(), sa.ForeignKey("chunk_profile_revision.id"), nullable=False),
        sa.Column("embedding_model_digest", sa.Text(), nullable=False),
        sa.Column("tombstone_generation", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("chunk_index >= 0", name="non_negative_chunk_index"),
        sa.CheckConstraint("start_offset >= 0", name="non_negative_start_offset"),
        sa.CheckConstraint("end_offset > start_offset", name="end_after_start"),
        sa.CheckConstraint("token_count > 0", name="positive_token_count"),
        sa.UniqueConstraint("version_id", "chunk_index", name="uq_chunk_version_index"),
        sa.UniqueConstraint(
            "version_id",
            "content_sha256",
            "start_offset",
            "end_offset",
            name="uq_chunk_content_span",
        ),
    )

    # structured_extraction
    op.create_table(
        "structured_extraction",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("document_version.id"), nullable=False),
        sa.Column("template_revision_id", sa.Uuid(), sa.ForeignKey("extraction_template_revision.id"), nullable=False),
        sa.Column("model_route_revision_id", sa.Uuid(), sa.ForeignKey("model_route_revision.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("source_spans", postgresql.JSONB(), nullable=False),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('validated', 'validation_failed', 'erased')", name="valid_extraction_status"),
        sa.UniqueConstraint("version_id", "template_revision_id", name="uq_extraction_version_template"),
    )

    # graph_entity
    op.create_table(
        "graph_entity",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("normalized_key", sa.Text(), nullable=False),
        sa.Column("display_value", sa.Text(), nullable=False),
        sa.Column("merged_into_id", sa.Uuid(), sa.ForeignKey("graph_entity.id"), nullable=True),
        sa.Column("tombstone_generation", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("entity_type", "normalized_key", name="uq_entity_type_key"),
    )

    # graph_fact
    op.create_table(
        "graph_fact",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("subject_entity_id", sa.Uuid(), sa.ForeignKey("graph_entity.id"), nullable=False),
        sa.Column("predicate_key", sa.Text(), nullable=False),
        sa.Column("object_entity_id", sa.Uuid(), sa.ForeignKey("graph_entity.id"), nullable=True),
        sa.Column("object_normalized_key", sa.Text(), nullable=False),
        sa.Column("source_chunk_id", sa.Uuid(), sa.ForeignKey("document_chunk.id"), nullable=False),
        sa.Column("source_version_id", sa.Uuid(), sa.ForeignKey("document_version.id"), nullable=False),
        sa.Column("extraction_route_revision_id", sa.Uuid(), sa.ForeignKey("model_route_revision.id"), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 3), nullable=False),
        sa.Column("evidence_span", sa.Text(), nullable=True),
        sa.Column("tombstone_generation", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="valid_confidence"),
        sa.UniqueConstraint(
            "subject_entity_id",
            "predicate_key",
            "object_normalized_key",
            "source_chunk_id",
            name="uq_fact_triple_chunk",
        ),
    )

    # projection_state
    op.create_table(
        "projection_state",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("projection_type", sa.Text(), nullable=False),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("document_version.id"), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("projection_type IN ('qdrant', 'opensearch', 'neo4j')", name="valid_projection_type"),
        sa.CheckConstraint("state IN ('pending', 'projected', 'retracted', 'failed')", name="valid_projection_state"),
        sa.UniqueConstraint("projection_type", "version_id", "generation", name="uq_projection_version_gen"),
    )

    # active_projection_generation
    op.create_table(
        "active_projection_generation",
        sa.Column("projection_type", sa.Text(), primary_key=True),
        sa.Column("active_generation", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # identity_subject
    op.create_table(
        "identity_subject",
        sa.Column("subject", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("scim_version", sa.Text(), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # identity_group_membership
    op.create_table(
        "identity_group_membership",
        sa.Column("subject", sa.Text(), sa.ForeignKey("identity_subject.subject"), primary_key=True),
        sa.Column("group_key", sa.Text(), primary_key=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # chat_session
    op.create_table(
        "chat_session",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # chat_message
    op.create_table(
        "chat_message",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("chat_session.id"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role IN ('system', 'user', 'assistant', 'tool')", name="valid_chat_role"),
    )

    # agent_run
    op.create_table(
        "agent_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("chat_session.id"), nullable=False),
        sa.Column("trigger_message_id", sa.Uuid(), sa.ForeignKey("chat_message.id"), nullable=False),
        sa.Column("graph_state_checkpoint", postgresql.JSONB(), nullable=False),
        sa.Column("result_message_id", sa.Uuid(), sa.ForeignKey("chat_message.id"), nullable=True),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # webhook
    op.create_table(
        "webhook",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("event_type_glob", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("failure_streak", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by_subject", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # webhook_delivery
    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("webhook_id", sa.Uuid(), sa.ForeignKey("webhook.id"), nullable=False),
        sa.Column("outbox_event_id", sa.Uuid(), sa.ForeignKey("outbox_event.id"), nullable=False),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("response_body_sha256", sa.String(64), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("state IN ('pending', 'delivered', 'failed', 'exhausted')", name="valid_delivery_state"),
    )

    # audit_event_identity (non-partitioned guard table)
    op.create_table(
        "audit_event_identity",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
    )

    # audit_anchor
    op.create_table(
        "audit_anchor",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_event_hash", sa.String(64), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("sealed_object_key", sa.Text(), nullable=False),
        sa.Column("sealed_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("period_start", "period_end", name="uq_audit_anchor_period"),
    )

    # audit_event (partitioned table — created via raw DDL)
    op.execute("""
        CREATE TABLE audit_event (
            id UUID NOT NULL,
            event_time TIMESTAMPTZ NOT NULL DEFAULT now(),
            actor_subject TEXT,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT,
            trace_id UUID,
            details JSONB NOT NULL,
            previous_hash VARCHAR(64),
            event_hash VARCHAR(64) NOT NULL,
            PRIMARY KEY (event_time, id)
        ) PARTITION BY RANGE (event_time)
    """)

    # Create 3 monthly partitions for audit_event
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    for offset in range(3):
        month = now.month + offset
        year = now.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        next_month = month + 1
        next_year = year + (next_month - 1) // 12
        next_month = (next_month - 1) % 12 + 1
        name = f"audit_event_y{year}m{month:02d}"
        op.execute(
            f"CREATE TABLE {name} PARTITION OF audit_event "
            f"FOR VALUES FROM ('{year}-{month:02d}-01') TO ('{next_year}-{next_month:02d}-01')"
        )

    # --- §8.2 Mandatory Indexes ---

    op.create_index(
        "document_active_idx",
        "document",
        ["created_at"],
        postgresql_using="btree",
        postgresql_where=sa.text("erased_at IS NULL"),
        unique=False,
    )
    # Sort descending via raw DDL since op.create_index doesn't support DESC easily
    op.execute(
        "CREATE INDEX document_version_document_state_idx "
        "ON document_version (document_id, lifecycle, version_number DESC)"
    )
    op.execute(
        "CREATE INDEX document_version_completed_idx "
        "ON document_version (completed_at DESC) "
        "WHERE lifecycle = 'completed'"
    )
    op.create_index(
        "document_label_label_document_idx",
        "document_label",
        ["label_id", "document_id"],
    )
    op.create_index(
        "operation_status_idx",
        "operation",
        ["status", "created_at"],
    )
    op.execute(
        "CREATE INDEX processing_stage_open_idx "
        "ON processing_stage (status, started_at) "
        "WHERE status IN ('queued', 'running', 'retrying')"
    )
    op.execute("CREATE INDEX outbox_pending_idx ON outbox_event (created_at) WHERE status = 'pending'")
    op.execute("CREATE INDEX dead_letter_open_idx ON dead_letter (created_at) WHERE state = 'open'")
    op.create_index(
        "chunk_version_idx",
        "document_chunk",
        ["version_id", "chunk_index"],
    )
    op.create_index(
        "graph_fact_source_idx",
        "graph_fact",
        ["source_version_id", "source_chunk_id"],
        postgresql_where=sa.text("tombstone_generation = 0"),
    )
    op.create_index(
        "projection_unhealthy_idx",
        "projection_state",
        ["projection_type", "started_at"],
        postgresql_where=sa.text("state = 'failed'"),
    )
    op.execute("CREATE INDEX chat_message_session_idx ON chat_message (session_id, created_at DESC)")
    op.execute("CREATE INDEX webhook_delivery_due_idx ON webhook_delivery (attempted_at) WHERE state = 'pending'")
    op.execute("CREATE INDEX audit_event_actor_time_idx ON audit_event (actor_subject, event_time DESC)")


def downgrade() -> None:
    # Drop deferred FK constraints first to avoid dependency errors
    op.drop_constraint("document_declared_type_fk", "document", type_="foreignkey")
    op.drop_constraint("document_current_version_fk", "document", type_="foreignkey")
    op.drop_constraint("version_chunk_profile_fk", "document_version", type_="foreignkey")
    op.drop_constraint("version_template_fk", "document_version", type_="foreignkey")
    op.drop_constraint("label_assignment_policy_fk", "document_label", type_="foreignkey")
    op.drop_constraint("proposal_model_route_fk", "template_proposal", type_="foreignkey")

    # Drop partitioned audit_event (CASCADE drops child partitions)
    op.execute("DROP TABLE IF EXISTS audit_event CASCADE")

    # Drop tables in reverse dependency order
    tables = [
        "audit_anchor",
        "audit_event_identity",
        "webhook_delivery",
        "webhook",
        "agent_run",
        "chat_message",
        "chat_session",
        "identity_group_membership",
        "identity_subject",
        "active_projection_generation",
        "projection_state",
        "graph_fact",
        "graph_entity",
        "structured_extraction",
        "document_chunk",
        "template_proposal",
        "dead_letter",
        "outbox_event",
        "processing_stage",
        "processing_run",
        "operation",
        "deletion_tombstone",
        "legal_hold",
        "document_label",
        "document_version",
        "document",
        "model_route_revision",
        "extraction_template_revision",
        "chunk_profile_revision",
        "declared_type",
        "label",
        "policy_revision",
    ]
    for table in tables:
        op.drop_table(table)

    # Drop ENUM types
    _OPERATION_STATUS.drop(op.get_bind(), checkfirst=True)
    _POLICY_STATUS.drop(op.get_bind(), checkfirst=True)
    _EXTRACTION_STATUS.drop(op.get_bind(), checkfirst=True)
    _STAGE_STATUS.drop(op.get_bind(), checkfirst=True)
    _DOCUMENT_LIFECYCLE.drop(op.get_bind(), checkfirst=True)
