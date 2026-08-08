"""Persist workflow outputs and enforce irreversible lifecycle evidence.

Revision ID: 002
Revises: 001
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add durable stage payloads and database-enforced irreversible rules."""
    op.add_column("processing_stage", sa.Column("output_json", postgresql.JSONB(), nullable=True))

    # Enum membership only rejects invalid values.  These guards make a direct
    # SQL update unable to revive an erased/failed version or skip mandatory
    # processing states, which keeps lifecycle truth in PostgreSQL rather than
    # in a particular API process.
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

    # Tombstones are retained WORM evidence.  Only insertion is permitted;
    # resolution lives in a separate append-only audit record rather than an
    # update or delete of the original proof.
    op.execute(
        """
        CREATE FUNCTION deletion_tombstone_immutable_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'deletion tombstones are immutable';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER deletion_tombstone_immutable_guard_trigger
        BEFORE UPDATE OR DELETE ON deletion_tombstone
        FOR EACH ROW EXECUTE FUNCTION deletion_tombstone_immutable_guard()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS deletion_tombstone_immutable_guard_trigger ON deletion_tombstone")
    op.execute("DROP FUNCTION IF EXISTS deletion_tombstone_immutable_guard()")
    op.execute("DROP TRIGGER IF EXISTS document_version_lifecycle_guard_trigger ON document_version")
    op.execute("DROP FUNCTION IF EXISTS document_version_lifecycle_guard()")
    op.drop_column("processing_stage", "output_json")
