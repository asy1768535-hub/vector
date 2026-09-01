"""Enable default knowledge artifacts.

Revision ID: 0047
Revises: 0046
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE sys_libraries
            SET knowledge_artifact_auto_enabled = true,
                summary_artifact_enabled = true,
                outline_artifact_enabled = true,
                knowledge_artifact_external_model_enabled = external_llm_enabled,
                knowledge_artifact_allowed_security_levels =
                    graph_extraction_allowed_security_levels
            WHERE deleted_at IS NULL
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE sys_libraries
            SET knowledge_artifact_auto_enabled = false,
                summary_artifact_enabled = false,
                outline_artifact_enabled = false,
                knowledge_artifact_external_model_enabled = false,
                knowledge_artifact_allowed_security_levels = '[]'::jsonb
            WHERE deleted_at IS NULL
            """
        )
    )
