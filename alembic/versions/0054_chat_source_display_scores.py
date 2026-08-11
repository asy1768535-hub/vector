"""persist chat source display score semantics

Revision ID: 0054
Revises: 0053
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa


revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_message_sources",
        sa.Column("score_type", sa.String(length=16), nullable=False, server_default="rrf"),
    )
    op.add_column("chat_message_sources", sa.Column("display_score", sa.Float(), nullable=True))
    # Existing rows predate score semantics and cannot be reconstructed safely.
    op.execute(
        sa.text(
            "UPDATE chat_message_sources SET score_type = 'legacy' "
            "WHERE score_type = 'rrf'"
        )
    )


def downgrade() -> None:
    op.drop_column("chat_message_sources", "display_score")
    op.drop_column("chat_message_sources", "score_type")
