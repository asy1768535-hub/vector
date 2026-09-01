"""Add the explicit per-library current ontology pointer."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0063"
down_revision: Union[str, None] = "0062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column(
            "current_ontology_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_lib_current_ontology",
        "sys_libraries",
        "ontology_versions",
        ["current_ontology_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_sys_libraries_current_ontology_version_id",
        "sys_libraries",
        ["current_ontology_version_id"],
    )
    # Backfill only the unambiguous legacy case.  Libraries with zero or
    # multiple active versions remain NULL and must fail closed until an
    # explicit activation establishes Current Ontology.
    op.execute(
        sa.text(
            """
            UPDATE sys_libraries AS libraries
            SET current_ontology_version_id = ontology.id
            FROM ontology_versions AS ontology
            WHERE ontology.library_id = libraries.id
              AND ontology.status = 'active'
              AND ontology.version_key NOT IN ('ai-exploration', 'ai-draft')
              AND (
                  SELECT count(*)
                  FROM ontology_versions AS active_versions
                  WHERE active_versions.library_id = libraries.id
                    AND active_versions.status = 'active'
                    AND active_versions.version_key NOT IN ('ai-exploration', 'ai-draft')
              ) = 1
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sys_libraries_current_ontology_version_id",
        table_name="sys_libraries",
    )
    op.drop_constraint(
        "fk_lib_current_ontology",
        "sys_libraries",
        type_="foreignkey",
    )
    op.drop_column("sys_libraries", "current_ontology_version_id")
