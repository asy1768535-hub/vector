"""v0.8 knowledge artifact runtime

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-21
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_HEARTBEAT_TYPES_V08 = (
    "service_type IN ('api','embedding_worker','cleanup_worker','graph_extractor',"
    "'knowledge_artifact_worker')"
)
_HEARTBEAT_TYPES_PRE_V08 = (
    "service_type IN ('api','embedding_worker','cleanup_worker','graph_extractor')"
)


def upgrade() -> None:
    op.add_column(
        "knowledge_artifact_jobs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_artifact_jobs",
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "knowledge_artifact_jobs",
        sa.Column("claimed_by", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "knowledge_artifact_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "knowledge_artifact_jobs",
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE knowledge_artifact_jobs SET status = 'queued', "
        "error_code = 'runtime_upgrade_requeued', "
        "error_message = 'Job was requeued while worker leasing was introduced', "
        "finished_at = NULL WHERE status = 'processing'"
    )
    op.create_check_constraint(
        "ck_knowledge_artifact_jobs_attempt_count",
        "knowledge_artifact_jobs",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_knowledge_artifact_jobs_claim_state",
        "knowledge_artifact_jobs",
        "((status = 'processing' AND claim_token IS NOT NULL "
        "AND claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL "
        "AND last_heartbeat_at IS NOT NULL) OR "
        "(status <> 'processing' AND claim_token IS NULL "
        "AND claimed_by IS NULL AND lease_expires_at IS NULL "
        "AND last_heartbeat_at IS NULL))",
    )
    op.create_index(
        "ix_knowledge_artifact_jobs_claimable",
        "knowledge_artifact_jobs",
        ["status", "lease_expires_at", "created_at"],
    )

    for name in (
        "knowledge_artifact_auto_enabled",
        "summary_artifact_enabled",
        "outline_artifact_enabled",
        "knowledge_artifact_external_model_enabled",
    ):
        op.add_column(
            "sys_libraries",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    op.add_column(
        "sys_libraries",
        sa.Column(
            "knowledge_artifact_allowed_security_levels",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_lib_knowledge_artifact_security_levels_array",
        "sys_libraries",
        "jsonb_typeof(knowledge_artifact_allowed_security_levels) = 'array'",
    )

    op.drop_constraint(
        "ck_heartbeat_service_type", "service_heartbeats", type_="check"
    )
    op.create_check_constraint(
        "ck_heartbeat_service_type", "service_heartbeats", _HEARTBEAT_TYPES_V08
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM service_heartbeats "
        "WHERE service_type = 'knowledge_artifact_worker'"
    )
    op.drop_constraint(
        "ck_heartbeat_service_type", "service_heartbeats", type_="check"
    )
    op.create_check_constraint(
        "ck_heartbeat_service_type",
        "service_heartbeats",
        _HEARTBEAT_TYPES_PRE_V08,
    )

    op.drop_constraint(
        "ck_lib_knowledge_artifact_security_levels_array",
        "sys_libraries",
        type_="check",
    )
    op.drop_column("sys_libraries", "knowledge_artifact_allowed_security_levels")
    op.drop_column("sys_libraries", "knowledge_artifact_external_model_enabled")
    op.drop_column("sys_libraries", "outline_artifact_enabled")
    op.drop_column("sys_libraries", "summary_artifact_enabled")
    op.drop_column("sys_libraries", "knowledge_artifact_auto_enabled")

    op.execute(
        "UPDATE knowledge_artifact_jobs SET status = 'queued', "
        "error_code = 'runtime_downgrade_requeued', "
        "error_message = 'Job was requeued before worker leasing was removed', "
        "finished_at = NULL, claim_token = NULL, claimed_by = NULL, "
        "lease_expires_at = NULL, last_heartbeat_at = NULL "
        "WHERE status = 'processing'"
    )

    op.drop_index(
        "ix_knowledge_artifact_jobs_claimable",
        table_name="knowledge_artifact_jobs",
    )
    op.drop_constraint(
        "ck_knowledge_artifact_jobs_claim_state",
        "knowledge_artifact_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_knowledge_artifact_jobs_attempt_count",
        "knowledge_artifact_jobs",
        type_="check",
    )
    for name in (
        "last_heartbeat_at",
        "lease_expires_at",
        "claimed_by",
        "claim_token",
        "attempt_count",
    ):
        op.drop_column("knowledge_artifact_jobs", name)
