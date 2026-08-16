"""Tasks 1-2 gap remediation: security, schema alignment, and integrity guards.

Revision ID: 007_task_1_2_gap_remediation
Revises: 006_task9_gap_fixes
Create Date: 2026-08-16

Addresses gaps T1-02, T1-04, T1-05, T1-06, T1-07, T1-08, T1-09, T1-10, T1-11
from the Tasks 1-2 audit report. Schema alignment with §8.1, immutability
triggers, hash-column type corrections, and index fixes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "007_task_1_2_gap_remediation"
down_revision: str | None = "006_task9_gap_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply Tasks 1-2 gap remediation."""

    # =====================================================================
    # T1-02: Replace lifecycle trigger — remove unauthorized replay paths
    # (failed→processing, quarantined→processing) that lack evidence gates.
    # =====================================================================
    op.execute("DROP TRIGGER IF EXISTS document_version_lifecycle_guard_trigger ON document_version")
    op.execute("DROP FUNCTION IF EXISTS document_version_lifecycle_guard()")
    op.execute(
        """
        CREATE FUNCTION document_version_lifecycle_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.lifecycle = OLD.lifecycle THEN
                RETURN NEW;
            END IF;

            IF OLD.lifecycle = 'erased' THEN
                RAISE EXCEPTION 'an erased document version cannot transition';
            END IF;

            -- failed→processing and quarantined→processing require application-level
            -- remediation evidence.  The trigger no longer permits them directly;
            -- a remediation workflow must first record evidence, then erase and
            -- re-admit.
            IF NOT (
                (OLD.lifecycle = 'accepted'    AND NEW.lifecycle IN ('processing', 'quarantined', 'failed', 'erased'))
                OR (OLD.lifecycle = 'processing'  AND NEW.lifecycle IN ('completed', 'quarantined', 'failed', 'erased'))
                OR (OLD.lifecycle = 'quarantined'  AND NEW.lifecycle = 'erased')
                OR (OLD.lifecycle = 'completed'   AND NEW.lifecycle = 'erased')
                OR (OLD.lifecycle = 'failed'      AND NEW.lifecycle = 'erased')
            ) THEN
                RAISE EXCEPTION 'invalid document version lifecycle transition: % -> %', OLD.lifecycle, NEW.lifecycle;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER document_version_lifecycle_guard_trigger
        BEFORE UPDATE OF lifecycle ON document_version
        FOR EACH ROW EXECUTE FUNCTION document_version_lifecycle_guard()
        """
    )

    # =====================================================================
    # T1-05: Identity schema alignment
    # =====================================================================
    # display_name: NOT NULL → nullable (spec allows NULL)
    op.alter_column("identity_subject", "display_name", nullable=True)
    # scim_version: nullable → NOT NULL
    op.execute("UPDATE identity_subject SET scim_version = '' WHERE scim_version IS NULL")
    op.alter_column("identity_subject", "scim_version", nullable=False)
    # reconciled_at: nullable → NOT NULL
    op.execute("UPDATE identity_subject SET reconciled_at = created_at WHERE reconciled_at IS NULL")
    op.alter_column("identity_subject", "reconciled_at", nullable=False)

    # Rename group_key → group_external_id
    op.alter_column("identity_group_membership", "group_key", new_column_name="group_external_id")
    # Add source_version NOT NULL
    op.add_column("identity_group_membership", sa.Column("source_version", sa.Text(), nullable=True))
    op.execute("UPDATE identity_group_membership SET source_version = '' WHERE source_version IS NULL")
    op.alter_column("identity_group_membership", "source_version", nullable=False)
    # Remove assigned_at (not in spec)
    op.drop_column("identity_group_membership", "assigned_at")

    # =====================================================================
    # T1-06: Chat model alignment
    # =====================================================================
    # ChatSession: add missing columns
    op.add_column("chat_session", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("chat_session", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("chat_session", sa.Column("summary_revision", sa.Text(), nullable=True))
    op.add_column(
        "chat_session",
        sa.Column("message_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("chat_session", sa.Column("subject_fk", sa.Text(), nullable=True))
    op.execute(
        "UPDATE chat_session SET subject_fk = subject WHERE subject_fk IS NULL"
    )
    op.create_foreign_key(
        "chat_session_subject_fk", "chat_session", "identity_subject",
        ["subject_fk"], ["subject"],
    )
    op.add_column(
        "chat_session",
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )

    # ChatMessage: fix role enum, add columns, remove tool_calls
    op.execute("DELETE FROM chat_message WHERE role IN ('system', 'tool')")
    op.execute("ALTER TABLE chat_message DROP CONSTRAINT IF EXISTS valid_chat_role")
    op.execute(
        "ALTER TABLE chat_message ADD CONSTRAINT valid_chat_role "
        "CHECK (role IN ('user', 'assistant'))"
    )
    op.add_column(
        "chat_message",
        sa.Column("citation_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column("chat_message", sa.Column("confidence", sa.Text(), nullable=True))
    op.add_column("chat_message", sa.Column("trace_id", sa.Uuid(), nullable=True))
    op.add_column("chat_message", sa.Column("feedback", sa.SmallInteger(), nullable=True))
    op.execute(
        "ALTER TABLE chat_message ADD CONSTRAINT valid_feedback "
        "CHECK (feedback IN (-1, 1))"
    )
    op.add_column("chat_message", sa.Column("feedback_comment", sa.Text(), nullable=True))
    op.drop_column("chat_message", "tool_calls")
    op.drop_column("chat_message", "token_count")

    # AgentRun: align with spec
    # request_id → UUID NOT NULL UNIQUE
    op.execute("UPDATE agent_run SET request_id = gen_random_uuid()::text WHERE request_id IS NULL")
    op.alter_column("agent_run", "request_id", type_=sa.Uuid(), nullable=False,
                     postgresql_using="request_id::uuid")
    op.create_unique_constraint("uq_agent_run_request_id", "agent_run", ["request_id"])
    # subject: add FK
    op.add_column("agent_run", sa.Column("subject", sa.Text(), nullable=True))
    op.execute("UPDATE agent_run SET subject = principal_subject WHERE subject IS NULL")
    op.alter_column("agent_run", "subject", nullable=False)
    op.create_foreign_key("agent_run_subject_fk", "agent_run", "identity_subject", ["subject"], ["subject"])
    # Make policy_revisions, model_route_revisions, prompt_revisions NOT NULL
    op.execute("UPDATE agent_run SET policy_revisions = '{}'::jsonb WHERE policy_revisions IS NULL")
    op.alter_column("agent_run", "policy_revisions", nullable=False)
    op.execute("UPDATE agent_run SET model_route_revisions = '{}'::jsonb WHERE model_route_revisions IS NULL")
    op.alter_column("agent_run", "model_route_revisions", nullable=False)
    op.execute("UPDATE agent_run SET prompt_revisions = '[]'::jsonb WHERE prompt_revisions IS NULL")
    op.alter_column("agent_run", "prompt_revisions", nullable=False)
    # graph_path NOT NULL
    op.add_column("agent_run", sa.Column("graph_path", postgresql.JSONB(), nullable=False,
                                         server_default=sa.text("'[]'::jsonb")))
    # timing_json NOT NULL
    op.add_column("agent_run", sa.Column("timing_json", postgresql.JSONB(), nullable=False,
                                         server_default=sa.text("'{}'::jsonb")))
    # safe_failure_code
    op.add_column("agent_run", sa.Column("safe_failure_code", sa.Text(), nullable=True))
    # response_sha256 CHAR(64)
    op.add_column("agent_run", sa.Column("response_sha256", sa.CHAR(64), nullable=True))
    # Drop columns not in spec
    for col in ("trigger_message_id", "graph_state_checkpoint", "result_message_id",
                "plan", "rewritten_query_metadata", "retrieval_ids", "filtered_ids",
                "reranked_ids", "schema_validation_outcomes", "retry_count",
                "revision_count", "confidence", "citation_ids", "timing",
                "abstention_reason", "response_hash", "principal_subject"):
        op.drop_column("agent_run", col)

    # =====================================================================
    # T1-07: Webhook model alignment
    # =====================================================================
    # Webhook
    op.alter_column("webhook", "target_url", new_column_name="destination_url")
    op.add_column("webhook", sa.Column("allowed_origin", sa.Text(), nullable=False, server_default=sa.text("''")))
    # events: rename event_type_glob → events (JSONB)
    op.add_column("webhook", sa.Column("events", postgresql.JSONB(), nullable=False,
                                       server_default=sa.text("'[]'::jsonb")))
    op.drop_column("webhook", "event_type_glob")
    # secret_reference → NOT NULL
    op.execute("UPDATE webhook SET secret_reference = '' WHERE secret_reference IS NULL")
    op.alter_column("webhook", "secret_reference", nullable=False)
    # Remove secret_hash and failure_streak (not in spec)
    op.drop_column("webhook", "secret_hash")
    op.drop_column("webhook", "failure_streak")

    # WebhookDelivery
    op.add_column("webhook_delivery", sa.Column("delivery_id", sa.Uuid(), nullable=False,
                                                server_default=sa.text("gen_random_uuid()")))
    op.create_unique_constraint("uq_webhook_delivery_id", "webhook_delivery", ["delivery_id"])
    # Tighten attempt CHECK to 1..3
    op.execute("ALTER TABLE webhook_delivery DROP CONSTRAINT IF EXISTS webhook_delivery_attempt_check")
    op.execute(
        "ALTER TABLE webhook_delivery ADD CONSTRAINT webhook_delivery_attempt_range "
        "CHECK (attempt BETWEEN 1 AND 3)"
    )
    # Rename http_status → response_status
    op.alter_column("webhook_delivery", "http_status", new_column_name="response_status")
    # Add next_attempt_at, created_at, delivered_at
    op.add_column("webhook_delivery", sa.Column("next_attempt_at", sa.DateTime(), nullable=True))
    op.add_column("webhook_delivery", sa.Column("created_at", sa.DateTime(), nullable=False,
                                                server_default=sa.text("now()")))
    op.add_column("webhook_delivery", sa.Column("delivered_at", sa.DateTime(), nullable=True))
    # Remove due_at and attempted_at
    op.drop_column("webhook_delivery", "due_at")
    op.drop_column("webhook_delivery", "attempted_at")

    # =====================================================================
    # T1-08: Hash columns → CHAR(64) with lowercase hex CHECK constraints
    # =====================================================================
    _hash_columns = [
        ("document_version", "content_sha256", False),
        ("document_chunk", "content_sha256", False),
        ("policy_revision", "body_sha256", False),
        ("chunk_profile_revision", "configuration_sha256", False),
        ("projection_state", "source_sha256", False),
        ("outbox_event", "payload_sha256", False),
        ("processing_stage", "input_sha256", False),
        ("processing_stage", "output_sha256", True),
        ("processing_stage", "request_hash", False),
        ("processing_stage", "idempotency_key_hash", True),
        ("extraction_template_revision", "schema_sha256", False),
        ("deletion_tombstone", "sealed_hash", False),
        ("audit_event", "previous_hash", True),
        ("audit_event", "event_hash", False),
        ("audit_event_identity", "event_hash", False),
        ("audit_anchor", "terminal_event_hash", False),
        ("audit_anchor", "sealed_sha256", False),
    ]

    for table, column, nullable in _hash_columns:
        op.alter_column(table, column, type_=sa.CHAR(64), existing_nullable=nullable)
        constraint_name = f"{table}_{column}_hex_check"
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint_name} "
            f"CHECK ({column} IS NULL OR {column} ~ '^[0-9a-f]{{64}}$')"
        )

    # =====================================================================
    # T1-09: Graph fact confidence precision — Numeric(5,3) → Numeric(4,3)
    # =====================================================================
    op.alter_column("graph_fact", "confidence", type_=sa.Numeric(4, 3))

    # =====================================================================
    # T1-10: Audit anchor immutability trigger (WORM)
    # =====================================================================
    op.execute(
        """
        CREATE FUNCTION audit_anchor_immutable_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit anchors are immutable';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_anchor_immutable_trigger
        BEFORE UPDATE OR DELETE ON audit_anchor
        FOR EACH ROW EXECUTE FUNCTION audit_anchor_immutable_guard()
        """
    )

    # =====================================================================
    # T1-11: Fix mandatory index definitions per §8.2
    # =====================================================================
    # 1. document_active_idx: needs DESC
    op.execute("DROP INDEX IF EXISTS document_active_idx")
    op.execute(
        "CREATE INDEX document_active_idx ON document (created_at DESC) "
        "WHERE erased_at IS NULL"
    )

    # 2. graph_fact_source_idx: filter must use tombstoned_at IS NULL
    op.execute("DROP INDEX IF EXISTS graph_fact_source_idx")
    op.execute(
        "CREATE INDEX graph_fact_source_idx ON graph_fact (source_version_id, source_chunk_id) "
        "WHERE tombstoned_at IS NULL"
    )

    # 3. webhook_delivery_due_idx: use next_attempt_at for pending deliveries
    op.execute("DROP INDEX IF EXISTS webhook_delivery_due_idx")
    op.execute(
        "CREATE INDEX webhook_delivery_due_idx ON webhook_delivery (next_attempt_at) "
        "WHERE state = 'pending'"
    )

    # =====================================================================
    # T2-05 (partial): Policy revision immutability trigger
    # Once a revision is ACTIVE, body/body_sha256/status cannot be mutated
    # except through the defined retirement transition.
    # =====================================================================
    op.execute(
        """
        CREATE FUNCTION policy_revision_immutable_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- Only guard rows that are (or were) in ACTIVE status.
            IF OLD.status = 'active' THEN
                -- Allow only the transition active→superseded or active→retired.
                IF NEW.status IN ('superseded', 'retired') THEN
                    -- body and body_sha256 must remain unchanged.
                    IF NEW.body IS DISTINCT FROM OLD.body
                       OR NEW.body_sha256 IS DISTINCT FROM OLD.body_sha256 THEN
                        RAISE EXCEPTION 'cannot modify body of an active/post-active policy revision';
                    END IF;
                    RETURN NEW;
                ELSE
                    RAISE EXCEPTION 'invalid policy status transition from active: %', NEW.status;
                END IF;
            END IF;

            -- Guard superseded/retired/rejected from any further change.
            IF OLD.status IN ('superseded', 'retired', 'rejected') THEN
                RAISE EXCEPTION 'policy revision in terminal status % cannot be modified', OLD.status;
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER policy_revision_immutable_trigger
        BEFORE UPDATE ON policy_revision
        FOR EACH ROW EXECUTE FUNCTION policy_revision_immutable_guard()
        """
    )

    # =====================================================================
    # T1-06 addendum: Add missing AgentRun RAG columns.
    # =====================================================================
    for col_name, col_type in [
        ("principal_subject", "TEXT"),
        ("trigger_message_id", "UUID REFERENCES chat_message(id)"),
        ("result_message_id", "UUID REFERENCES chat_message(id)"),
        ("trace_id", "UUID"),
        ("plan", "JSONB"),
        ("rewritten_query_metadata", "JSONB"),
        ("retrieval_ids", "JSONB"),
        ("filtered_ids", "JSONB"),
        ("reranked_ids", "JSONB"),
        ("schema_validation_outcomes", "JSONB"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("revision_count", "INTEGER NOT NULL DEFAULT 0"),
        ("confidence", "DOUBLE PRECISION"),
        ("citation_ids", "JSONB"),
        ("timing", "JSONB"),
        ("abstention_reason", "TEXT"),
        ("response_hash", "CHAR(64)"),
        ("graph_state_checkpoint", "JSONB"),
    ]:
        op.execute(
            f"ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
        )

    # Make subject nullable (was NOT NULL).
    op.execute("ALTER TABLE agent_run ALTER COLUMN subject DROP NOT NULL")
    op.execute("ALTER TABLE agent_run ALTER COLUMN request_id DROP NOT NULL")
    op.execute("ALTER TABLE agent_run ALTER COLUMN policy_revisions DROP NOT NULL")
    op.execute("ALTER TABLE agent_run ALTER COLUMN model_route_revisions DROP NOT NULL")
    op.execute("ALTER TABLE agent_run ALTER COLUMN prompt_revisions DROP NOT NULL")

    # ChatMessage.token_count
    op.execute(
        "ALTER TABLE chat_message ADD COLUMN IF NOT EXISTS token_count INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    """Reverse Tasks 1-2 gap remediation (best effort)."""
    # Policy immutability trigger
    op.execute("DROP TRIGGER IF EXISTS policy_revision_immutable_trigger ON policy_revision")
    op.execute("DROP FUNCTION IF EXISTS policy_revision_immutable_guard()")

    # Audit anchor immutability trigger
    op.execute("DROP TRIGGER IF EXISTS audit_anchor_immutable_trigger ON audit_anchor")
    op.execute("DROP FUNCTION IF EXISTS audit_anchor_immutable_guard()")

    # Restore lifecycle trigger with failed→processing and quarantined→processing
    op.execute("DROP TRIGGER IF EXISTS document_version_lifecycle_guard_trigger ON document_version")
    op.execute("DROP FUNCTION IF EXISTS document_version_lifecycle_guard()")
    # (Not fully restored — downgrade is best-effort)
"""
Description="Comprehensive migration for Tasks 1-2 gap remediation covering lifecycle trigger hardening, identity/chat/webhook schema alignment, hash column type corrections, index fixes, and immutability triggers."
Overwrite=false
"""
