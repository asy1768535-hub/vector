"""Add Schema file import lifecycle action kind.

Revision ID: 0051
Revises: 0050
Create Date: 2026-08-03
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_ACTION_KINDS = (
    "action_kind IN ('clone_version','create_item','update_item',"
    "'disable_item','activate_version','delete_version','disable_version')"
)
_NEW_ACTION_KINDS = (
    "action_kind IN ('clone_version','import_version','create_item','update_item',"
    "'disable_item','activate_version','delete_version','disable_version')"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_schema_lifecycle_actions_kind",
        "schema_lifecycle_actions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_schema_lifecycle_actions_kind",
        "schema_lifecycle_actions",
        _NEW_ACTION_KINDS,
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM schema_lifecycle_actions
                WHERE action_kind = 'import_version'
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade while Schema import audit rows exist';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(
        "ck_schema_lifecycle_actions_kind",
        "schema_lifecycle_actions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_schema_lifecycle_actions_kind",
        "schema_lifecycle_actions",
        _OLD_ACTION_KINDS,
    )
