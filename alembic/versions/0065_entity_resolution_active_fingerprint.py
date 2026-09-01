"""Allow superseded entity-resolution fingerprints to reappear."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0065"
down_revision: Union[str, None] = "0064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_entity_resolution_decisions_library_fingerprint",
        "entity_resolution_decisions",
        type_="unique",
    )
    op.create_index(
        "uq_entity_resolution_decisions_library_fingerprint_active",
        "entity_resolution_decisions",
        ["library_id", "decision_fingerprint"],
        unique=True,
        postgresql_where=sa.text("lifecycle_status = 'active'"),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM entity_resolution_decisions
                    GROUP BY library_id, decision_fingerprint
                    HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'cannot restore global decision fingerprint uniqueness while historical duplicates exist';
                END IF;
            END $$;
            """
        )
    )
    op.drop_index(
        "uq_entity_resolution_decisions_library_fingerprint_active",
        table_name="entity_resolution_decisions",
    )
    op.create_unique_constraint(
        "uq_entity_resolution_decisions_library_fingerprint",
        "entity_resolution_decisions",
        ["library_id", "decision_fingerprint"],
    )
