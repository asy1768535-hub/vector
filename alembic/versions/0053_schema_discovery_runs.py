"""Add batch-level Schema discovery coordination and immutable draft metadata.

Revision ID: 0053
Revises: 0052
Create Date: 2026-08-05
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0053"
down_revision: Union[str, None] = "0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schema_discovery_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_set_key", sa.String(length=128), nullable=False),
        sa.Column("source_revision_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("ontology_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ontology_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ontology_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','discovering','succeeded','failed','cancelled')",
            name="ck_schema_discovery_runs_status",
        ),
        sa.ForeignKeyConstraint(["library_id"], ["sys_libraries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ontology_version_id"], ["ontology_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("library_id", "source_set_key", name="uq_schema_discovery_runs_library_source_set"),
    )
    op.create_index(
        "ix_schema_discovery_runs_library_status_created",
        "schema_discovery_runs",
        ["library_id", "status", "created_at"],
    )
    op.create_index(
        "ix_schema_discovery_runs_source_hash",
        "schema_discovery_runs",
        ["library_id", "source_hash"],
    )

    op.add_column(
        "ontology_versions",
        sa.Column("origin", sa.String(length=32), server_default="user", nullable=False),
    )
    op.add_column(
        "ontology_versions",
        sa.Column("confirmed", sa.Boolean(), server_default=sa.true(), nullable=False),
    )

    op.add_column(
        "graph_extraction_jobs",
        sa.Column("schema_discovery_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_graph_extraction_jobs_schema_discovery_run",
        "graph_extraction_jobs",
        "schema_discovery_runs",
        ["schema_discovery_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_graph_extraction_jobs_schema_discovery_run",
        "graph_extraction_jobs",
        ["schema_discovery_run_id", "status"],
    )
    op.drop_constraint("ck_graph_extraction_jobs_status", "graph_extraction_jobs", type_="check")
    op.create_check_constraint(
        "ck_graph_extraction_jobs_status",
        "graph_extraction_jobs",
        "status IN ('waiting_schema','queued','processing','partially_succeeded','succeeded','failed','cancelled','superseded')",
    )
    op.drop_constraint("ck_graph_extraction_jobs_current_stage", "graph_extraction_jobs", type_="check")
    op.create_check_constraint(
        "ck_graph_extraction_jobs_current_stage",
        "graph_extraction_jobs",
        "current_stage IS NULL OR current_stage IN ('waiting_schema','preparing','building_context','extracting','parsing','binding_evidence','aggregating','validating','scoring','materializing','finalizing')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_graph_extraction_jobs_current_stage", "graph_extraction_jobs", type_="check")
    op.create_check_constraint(
        "ck_graph_extraction_jobs_current_stage",
        "graph_extraction_jobs",
        "current_stage IS NULL OR current_stage IN ('preparing','building_context','extracting','parsing','binding_evidence','aggregating','validating','scoring','materializing','finalizing')",
    )
    op.drop_constraint("ck_graph_extraction_jobs_status", "graph_extraction_jobs", type_="check")
    op.create_check_constraint(
        "ck_graph_extraction_jobs_status",
        "graph_extraction_jobs",
        "status IN ('queued','processing','partially_succeeded','succeeded','failed','cancelled','superseded')",
    )
    op.drop_index("ix_graph_extraction_jobs_schema_discovery_run", table_name="graph_extraction_jobs")
    op.drop_constraint("fk_graph_extraction_jobs_schema_discovery_run", "graph_extraction_jobs", type_="foreignkey")
    op.drop_column("graph_extraction_jobs", "schema_discovery_run_id")
    op.drop_column("ontology_versions", "confirmed")
    op.drop_column("ontology_versions", "origin")
    op.drop_index("ix_schema_discovery_runs_source_hash", table_name="schema_discovery_runs")
    op.drop_index("ix_schema_discovery_runs_library_status_created", table_name="schema_discovery_runs")
    op.drop_table("schema_discovery_runs")
