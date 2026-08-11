"""Add the per-library raw claim shadow extraction rollout policy."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column(
            "claim_graph_shadow_policy",
            sa.String(length=16),
            nullable=False,
            server_default="inherit",
        ),
    )
    op.create_check_constraint(
        "ck_lib_claim_graph_shadow_policy",
        "sys_libraries",
        "claim_graph_shadow_policy IN ('inherit','enabled','disabled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_lib_claim_graph_shadow_policy", "sys_libraries", type_="check")
    op.drop_column("sys_libraries", "claim_graph_shadow_policy")
