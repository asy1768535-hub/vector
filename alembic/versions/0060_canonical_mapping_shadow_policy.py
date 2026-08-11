"""Add the independent Phase 3 canonical mapping shadow policy."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0060"
down_revision: Union[str, None] = "0059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column(
            "canonical_mapping_shadow_policy",
            sa.String(length=16),
            server_default="inherit",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_lib_canonical_mapping_shadow_policy",
        "sys_libraries",
        "canonical_mapping_shadow_policy IN ('inherit','enabled','disabled')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_lib_canonical_mapping_shadow_policy",
        "sys_libraries",
        type_="check",
    )
    op.drop_column("sys_libraries", "canonical_mapping_shadow_policy")
