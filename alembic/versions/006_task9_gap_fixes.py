"""Task 9 gap fixes: deleted_at on chat_session, chat_feedback table, webhook columns.

Revision ID: 006_task9_gap_fixes
Revises: 005_task5_gap_fixes
"""

from alembic import op
import sqlalchemy as sa

revision = "006_task9_gap_fixes"
down_revision = "005_task5_gap_fixes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # T9-04: Add deleted_at to chat_session for tombstone erasure.
    op.add_column("chat_session", sa.Column("deleted_at", sa.DateTime(), nullable=True))

    # T9-08: Chat feedback table.
    op.create_table(
        "chat_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("message_id", sa.Uuid(), sa.ForeignKey("chat_message.id"), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("score IN (-1, 1)", name="valid_feedback_score"),
    )

    # T9-10: Secret reference on webhook.
    op.add_column("webhook", sa.Column("secret_reference", sa.Text(), nullable=True))

    # T9-12: Due-at and uniqueness for delivery retries.
    op.add_column("webhook_delivery", sa.Column("due_at", sa.DateTime(), nullable=True))
    op.create_unique_constraint(
        "uq_delivery_attempt",
        "webhook_delivery",
        ["webhook_id", "outbox_event_id", "attempt"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_delivery_attempt", "webhook_delivery", type_="unique")
    op.drop_column("webhook_delivery", "due_at")
    op.drop_column("webhook", "secret_reference")
    op.drop_table("chat_feedback")
    op.drop_column("chat_session", "deleted_at")
