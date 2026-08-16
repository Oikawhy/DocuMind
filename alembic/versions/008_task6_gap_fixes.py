"""Task 6 gap fixes: projection state constraint and scope alignment.

Revision ID: 008_task6_gap_fixes
Revises: 007_task_1_2_gap_remediation
Create Date: 2026-08-16

Addresses T6-15 (add 'projected' to valid_projection_state constraint).
"""

from alembic import op

revision = "008_task6_gap_fixes"
down_revision = "007_task_1_2_gap_remediation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # T6-15: Add 'projected' to valid_projection_state check constraint.
    # The coordinator writes status='projected' after successful writer
    # outcomes, but the prior constraint only allowed:
    # pending | writing | verified | unhealthy | erased.
    op.drop_constraint("valid_projection_state", "projection_state", type_="check")
    op.create_check_constraint(
        "valid_projection_state",
        "projection_state",
        "state IN ('pending', 'writing', 'projected', 'verified', 'unhealthy', 'erased')",
    )


def downgrade() -> None:
    op.drop_constraint("valid_projection_state", "projection_state", type_="check")
    op.create_check_constraint(
        "valid_projection_state",
        "projection_state",
        "state IN ('pending', 'writing', 'verified', 'unhealthy', 'erased')",
    )
