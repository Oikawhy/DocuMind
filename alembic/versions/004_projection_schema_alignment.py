"""Align projection schema with §8.1 and add remediation lifecycle paths.

Revision ID: 004
Revises: 003
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Align projection_state / active_projection_generation with §8.1 and
    add missing lifecycle transitions (failed→processing, quarantined→processing).
    """

    # ------------------------------------------------------------------
    # 1. projection_state — rename and add columns
    # ------------------------------------------------------------------
    op.alter_column("projection_state", "projection_type", new_column_name="projection_kind")
    op.alter_column("projection_state", "checksum", new_column_name="source_sha256")

    op.add_column("projection_state", sa.Column("scope_key", sa.Text(), nullable=True))
    op.add_column("projection_state", sa.Column("expected_count", sa.Integer(), nullable=True))
    op.add_column("projection_state", sa.Column("observed_count", sa.Integer(), nullable=True))
    op.add_column("projection_state", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "projection_state",
        sa.Column(
            "state_changed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column("projection_state", sa.Column("last_error_code", sa.Text(), nullable=True))

    # Backfill scope_key from version_id (vector stores) or 'global' (neo4j).
    op.execute(
        "UPDATE projection_state SET scope_key = version_id::text "
        "WHERE projection_kind IN ('qdrant', 'opensearch') AND version_id IS NOT NULL"
    )
    op.execute(
        "UPDATE projection_state SET scope_key = 'global' "
        "WHERE projection_kind = 'neo4j'"
    )
    # If any rows still have NULL scope_key, default to version_id::text.
    op.execute(
        "UPDATE projection_state SET scope_key = COALESCE(version_id::text, 'global') "
        "WHERE scope_key IS NULL"
    )
    op.alter_column("projection_state", "scope_key", nullable=False)

    # Backfill expected_count — default to 0 for historical rows.
    op.execute("UPDATE projection_state SET expected_count = 0 WHERE expected_count IS NULL")
    op.alter_column("projection_state", "expected_count", nullable=False)

    # Backfill state_changed_at from started_at for existing rows.
    op.execute(
        "UPDATE projection_state SET state_changed_at = started_at WHERE state_changed_at IS NULL"
    )
    op.alter_column("projection_state", "state_changed_at", nullable=False)

    # Drop old columns not in spec.
    op.drop_column("projection_state", "metadata_json")
    # started_at → created_at is already present; drop started_at if present.
    # The spec uses created_at, not started_at. Rename for consistency.
    op.alter_column("projection_state", "started_at", new_column_name="created_at")
    op.drop_column("projection_state", "completed_at")

    # Update CHECK constraints — drop old, add new.
    op.drop_constraint("valid_projection_type", "projection_state", type_="check")
    op.drop_constraint("valid_projection_state", "projection_state", type_="check")
    op.create_check_constraint(
        "valid_projection_type",
        "projection_state",
        "projection_kind IN ('qdrant', 'opensearch', 'neo4j')",
    )
    op.create_check_constraint(
        "valid_projection_state",
        "projection_state",
        "state IN ('pending', 'writing', 'verified', 'unhealthy', 'erased')",
    )
    op.create_check_constraint(
        "projection_scope_consistency",
        "projection_state",
        "(projection_kind = 'neo4j' AND scope_key = 'global' AND version_id IS NULL) "
        "OR (projection_kind IN ('qdrant', 'opensearch') "
        "AND version_id IS NOT NULL AND scope_key = version_id::text)",
    )

    # Make version_id nullable (Neo4j rows have NULL).
    op.alter_column("projection_state", "version_id", nullable=True)

    # Drop old external_id column (not in spec).
    op.drop_column("projection_state", "external_id")

    # Update unique constraint.
    op.drop_constraint("uq_projection_version_gen", "projection_state", type_="unique")
    op.create_unique_constraint(
        "uq_projection_version_gen",
        "projection_state",
        ["projection_kind", "scope_key", "generation"],
    )

    # Update projection_unhealthy_idx to use new column names.
    op.drop_index("projection_unhealthy_idx", table_name="projection_state")
    op.execute(
        "CREATE INDEX projection_unhealthy_idx ON projection_state "
        "(projection_kind, state_changed_at) WHERE state = 'unhealthy'"
    )

    # ------------------------------------------------------------------
    # 2. active_projection_generation — restructure per §8.1
    # ------------------------------------------------------------------
    # Easier to recreate: drop + create with new schema.
    op.drop_table("active_projection_generation")
    op.create_table(
        "active_projection_generation",
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "activated_by_operation_id",
            sa.Uuid(),
            sa.ForeignKey("operation.id"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("projection_kind", "scope_key"),
        sa.CheckConstraint(
            "projection_kind IN ('qdrant', 'opensearch', 'neo4j')",
            name="apg_valid_projection_kind",
        ),
        sa.ForeignKeyConstraint(
            ["projection_kind", "scope_key", "generation"],
            [
                "projection_state.projection_kind",
                "projection_state.scope_key",
                "projection_state.generation",
            ],
            name="fk_apg_projection_state",
        ),
    )

    # ------------------------------------------------------------------
    # 3. Lifecycle trigger — add failed→processing, quarantined→processing
    # ------------------------------------------------------------------
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

            IF NOT (
                (OLD.lifecycle = 'accepted' AND NEW.lifecycle IN ('processing', 'quarantined', 'failed', 'erased'))
                OR (OLD.lifecycle = 'processing' AND NEW.lifecycle IN ('completed', 'quarantined', 'failed', 'erased'))
                OR (OLD.lifecycle = 'quarantined' AND NEW.lifecycle IN ('processing', 'erased'))
                OR (OLD.lifecycle = 'completed' AND NEW.lifecycle = 'erased')
                OR (OLD.lifecycle = 'failed' AND NEW.lifecycle IN ('processing', 'erased'))
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


def downgrade() -> None:
    # Restore original lifecycle trigger (without remediation paths).
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

            IF NOT (
                (OLD.lifecycle = 'accepted' AND NEW.lifecycle IN ('processing', 'quarantined', 'failed', 'erased'))
                OR (OLD.lifecycle = 'processing' AND NEW.lifecycle IN ('completed', 'quarantined', 'failed', 'erased'))
                OR (OLD.lifecycle = 'quarantined' AND NEW.lifecycle = 'erased')
                OR (OLD.lifecycle = 'completed' AND NEW.lifecycle = 'erased')
                OR (OLD.lifecycle = 'failed' AND NEW.lifecycle = 'erased')
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

    # Restore active_projection_generation to original schema.
    op.drop_table("active_projection_generation")
    op.create_table(
        "active_projection_generation",
        sa.Column("projection_type", sa.Text(), primary_key=True),
        sa.Column(
            "active_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Restore projection_state to original schema.
    op.drop_index("projection_unhealthy_idx", table_name="projection_state")

    op.drop_constraint("projection_scope_consistency", "projection_state", type_="check")
    op.drop_constraint("valid_projection_state", "projection_state", type_="check")
    op.drop_constraint("valid_projection_type", "projection_state", type_="check")

    op.drop_constraint("uq_projection_version_gen", "projection_state", type_="unique")

    op.add_column("projection_state", sa.Column("external_id", sa.Text(), nullable=True))
    op.alter_column("projection_state", "version_id", nullable=False)
    op.alter_column("projection_state", "created_at", new_column_name="started_at")
    op.add_column("projection_state", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "projection_state",
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )

    op.drop_column("projection_state", "last_error_code")
    op.drop_column("projection_state", "state_changed_at")
    op.drop_column("projection_state", "verified_at")
    op.drop_column("projection_state", "observed_count")
    op.drop_column("projection_state", "expected_count")
    op.drop_column("projection_state", "scope_key")

    op.alter_column("projection_state", "source_sha256", new_column_name="checksum")
    op.alter_column("projection_state", "projection_kind", new_column_name="projection_type")

    op.create_check_constraint(
        "valid_projection_type",
        "projection_state",
        "projection_type IN ('qdrant', 'opensearch', 'neo4j')",
    )
    op.create_check_constraint(
        "valid_projection_state",
        "projection_state",
        "state IN ('pending', 'projected', 'retracted', 'failed')",
    )
    op.create_unique_constraint(
        "uq_projection_version_gen",
        "projection_state",
        ["projection_type", "version_id", "generation"],
    )
    op.execute(
        "CREATE INDEX projection_unhealthy_idx ON projection_state "
        "(projection_type, started_at) WHERE state = 'failed'"
    )
