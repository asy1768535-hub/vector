"""Add stable predicate resolution policies.

Revision ID: 0068
Revises: 0067
Create Date: 2026-09-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stable_predicate_identities",
        sa.Column("resolution_policy", postgresql.JSONB(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE stable_predicate_identities
            SET resolution_status = 'pending'
            WHERE resolution_status = 'resolved'
              AND resolution_policy IS NULL
            """
        )
    )
    op.create_check_constraint(
        "ck_stable_predicate_identities_resolution_policy_json",
        "stable_predicate_identities",
        "resolution_policy IS NULL OR jsonb_typeof(resolution_policy) = 'object'",
    )
    op.create_check_constraint(
        "ck_stable_predicate_identities_resolved_requires_policy",
        "stable_predicate_identities",
        "resolution_status != 'resolved' OR resolution_policy IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_stable_predicate_identities_resolved_requires_policy",
        "stable_predicate_identities",
        type_="check",
    )
    op.drop_constraint(
        "ck_stable_predicate_identities_resolution_policy_json",
        "stable_predicate_identities",
        type_="check",
    )
    op.drop_column("stable_predicate_identities", "resolution_policy")
