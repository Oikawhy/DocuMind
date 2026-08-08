"""Add Task 5 type-suggestion and graph-fact provenance contracts.

Revision ID: 003
Revises: 002
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Extend immutable enrichment records without rewriting existing facts."""
    op.add_column("document_version", sa.Column("type_suggestion", postgresql.JSONB(), nullable=True))

    op.add_column("graph_fact", sa.Column("object_literal", postgresql.JSONB(), nullable=True))
    op.execute(
        """
        UPDATE graph_fact
        SET object_literal = jsonb_build_object('legacy_normalized_key', object_normalized_key)
        WHERE object_entity_id IS NULL
        """
    )
    op.add_column(
        "graph_fact",
        sa.Column("gleaning_pass", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "graph_fact",
        sa.Column("corroboration_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column("graph_fact", sa.Column("conflict_group_key", sa.Text(), nullable=True))
    op.add_column("graph_fact", sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True))

    op.drop_constraint("uq_fact_triple_chunk", "graph_fact", type_="unique")
    op.create_unique_constraint(
        "uq_fact_triple_chunk_route",
        "graph_fact",
        [
            "subject_entity_id",
            "predicate_key",
            "object_normalized_key",
            "source_chunk_id",
            "extraction_route_revision_id",
        ],
    )
    op.create_check_constraint("valid_gleaning_pass", "graph_fact", "gleaning_pass IN (0, 1)")
    op.create_check_constraint(
        "exactly_one_fact_object",
        "graph_fact",
        "(object_entity_id IS NOT NULL) <> (object_literal IS NOT NULL)",
    )


def downgrade() -> None:
    """Restore the prior, less-specific graph-fact identity contract."""
    op.drop_constraint("exactly_one_fact_object", "graph_fact", type_="check")
    op.drop_constraint("valid_gleaning_pass", "graph_fact", type_="check")
    op.drop_constraint("uq_fact_triple_chunk_route", "graph_fact", type_="unique")
    op.create_unique_constraint(
        "uq_fact_triple_chunk",
        "graph_fact",
        ["subject_entity_id", "predicate_key", "object_normalized_key", "source_chunk_id"],
    )
    op.drop_column("graph_fact", "tombstoned_at")
    op.drop_column("graph_fact", "conflict_group_key")
    op.drop_column("graph_fact", "corroboration_count")
    op.drop_column("graph_fact", "gleaning_pass")
    op.drop_column("graph_fact", "object_literal")
    op.drop_column("document_version", "type_suggestion")
