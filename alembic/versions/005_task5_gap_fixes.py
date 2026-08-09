"""Task 5 gap fixes: template proposal uniqueness and enrichment constraints.

Revision ID: 005
Revises: 004
Create Date: 2026-08-09
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # T5-11: Add unique constraint on template_proposal.version_id
    # to prevent crash-retry from creating duplicate proposals.
    op.create_unique_constraint(
        "uq_template_proposal_version",
        "template_proposal",
        ["version_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_template_proposal_version",
        "template_proposal",
        type_="unique",
    )
