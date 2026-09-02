"""Allow Fact Resolution decision history while preserving one current decision."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0069"
down_revision: Union[str, None] = "0068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_fact_resolution_decisions_library_fingerprint",
        "fact_resolution_decisions",
        type_="unique",
    )
    op.create_index(
        "uq_fact_resolution_decisions_library_fingerprint_active",
        "fact_resolution_decisions",
        ["library_id", "decision_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status <> 'superseded'"),
    )
    op.create_index(
        "uq_fact_resolution_decisions_library_subject_active",
        "fact_resolution_decisions",
        ["library_id", "source_kind", "subject_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status <> 'superseded'"),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM fact_resolution_decisions
                    GROUP BY library_id, decision_fingerprint
                    HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'cannot restore global Fact Resolution decision fingerprint uniqueness while historical duplicates exist';
                END IF;
            END $$;
            """
        )
    )
    op.drop_index(
        "uq_fact_resolution_decisions_library_subject_active",
        table_name="fact_resolution_decisions",
    )
    op.drop_index(
        "uq_fact_resolution_decisions_library_fingerprint_active",
        table_name="fact_resolution_decisions",
    )
    op.create_unique_constraint(
        "uq_fact_resolution_decisions_library_fingerprint",
        "fact_resolution_decisions",
        ["library_id", "decision_fingerprint"],
    )
