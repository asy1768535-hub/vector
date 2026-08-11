"""Add explicit Schema confirmation policy and discovery waiting state.

Revision ID: 0055
Revises: 0054
Create Date: 2026-08-06
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column(
            "schema_confirmation_policy",
            sa.String(length=16),
            nullable=False,
            server_default="required",
        ),
    )
    op.create_check_constraint(
        "ck_lib_schema_confirmation_policy",
        "sys_libraries",
        "schema_confirmation_policy IN ('required','automatic')",
    )
    op.add_column(
        "schema_discovery_runs",
        sa.Column(
            "confirmation_policy",
            sa.String(length=16),
            nullable=False,
            server_default="required",
        ),
    )
    op.add_column(
        "schema_discovery_runs",
        sa.Column(
            "concept_inventory",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "schema_discovery_runs",
        sa.Column(
            "discovery_trace",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.drop_constraint(
        "ck_schema_discovery_runs_status",
        "schema_discovery_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_schema_discovery_runs_status",
        "schema_discovery_runs",
        "status IN ('queued','discovering','waiting_confirmation','succeeded','failed','cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_schema_discovery_runs_status", "schema_discovery_runs", type_="check")
    op.create_check_constraint(
        "ck_schema_discovery_runs_status",
        "schema_discovery_runs",
        "status IN ('queued','discovering','succeeded','failed','cancelled')",
    )
    op.drop_column("schema_discovery_runs", "concept_inventory")
    op.drop_column("schema_discovery_runs", "discovery_trace")
    op.drop_column("schema_discovery_runs", "confirmation_policy")
    op.drop_constraint("ck_lib_schema_confirmation_policy", "sys_libraries", type_="check")
    op.drop_column("sys_libraries", "schema_confirmation_policy")
