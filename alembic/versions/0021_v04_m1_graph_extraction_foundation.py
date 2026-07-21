"""v0.4 graph extraction pipeline m1 foundation

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-10
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column(
            "graph_extraction_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "sys_libraries",
        sa.Column(
            "external_llm_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "sys_libraries",
        sa.Column(
            "graph_extraction_allowed_security_levels",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    op.drop_constraint("ck_heartbeat_service_type",
        "service_heartbeats",
        type_="check",
    )
    op.create_check_constraint(
        "ck_heartbeat_service_type",
        "service_heartbeats",
        "service_type IN ('api','embedding_worker','cleanup_worker','graph_extractor')",
    )

    _create_graph_extraction_jobs()
    _create_graph_extraction_units()
    _create_extraction_context_snapshots()
    _create_extraction_raw_output_attempts()


def _create_graph_extraction_jobs() -> None:
    op.create_table("graph_extraction_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "sys_libraries.id",
                ondelete="CASCADE",
                name="fk_graph_extraction_jobs_library",
            ),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "documents.id",
                ondelete="RESTRICT",
                name="fk_graph_extraction_jobs_document",
            ),
            nullable=False,
        ),
        sa.Column(
            "document_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "document_revisions.id",
                ondelete="RESTRICT",
                name="fk_graph_extraction_jobs_revision",
            ),
            nullable=False,
        ),
        sa.Column(
            "ontology_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "ontology_versions.id",
                ondelete="RESTRICT",
                name="fk_graph_extraction_jobs_ontology",
            ),
            nullable=False,
        ),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="queued"
        ),
        sa.Column("current_stage", sa.String(length=32), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "rerun_of_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "graph_extraction_jobs.id",
                ondelete="SET NULL",
                name="fk_graph_extraction_jobs_rerun",
            ),
            nullable=True,
        ),
        sa.Column("retry_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_provider", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.String(length=64), nullable=False),
        sa.Column("output_parser_version", sa.String(length=64), nullable=False),
        sa.Column("context_policy_version", sa.String(length=64), nullable=False),
        sa.Column("extraction_policy_version", sa.String(length=64), nullable=False),
        sa.Column("normalization_rule_version", sa.String(length=64), nullable=False),
        sa.Column("confidence_policy_version", sa.String(length=64), nullable=False),
        sa.Column("document_parser_version", sa.String(length=64), nullable=False),
        sa.Column("chunking_strategy_version", sa.String(length=64), nullable=False),
        sa.Column("model_config_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("policy_config_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("model_config_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_config_hash", sa.String(length=64), nullable=False),
        sa.Column("ontology_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("ontology_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "sys_users.id",
                ondelete="SET NULL",
                name="fk_graph_extraction_jobs_requested_by",
            ),
            nullable=True,
        ),
        sa.Column(
            "sensitive_payload_purged_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "counts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "statistics",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger_type IN ('manual','revision_published','full_rerun','repair','eval')",
            name="ck_graph_extraction_jobs_trigger_type",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('production','eval','repair')",
            name="ck_graph_extraction_jobs_execution_mode",
        ),
        sa.CheckConstraint(
            "status IN ('queued','processing','partially_succeeded','succeeded','failed','cancelled','superseded')",
            name="ck_graph_extraction_jobs_status",
        ),
        sa.CheckConstraint(
            "current_stage IS NULL OR current_stage IN "
            "('preparing','building_context','extracting','parsing','binding_evidence',"
            "'aggregating','validating','scoring','materializing','finalizing')",
            name="ck_graph_extraction_jobs_current_stage",
        ),
        sa.CheckConstraint(
            "retry_generation >= 0",
            name="ck_graph_extraction_jobs_retry_generation",
        ),
        sa.CheckConstraint(
            "(trigger_type = 'full_rerun' AND rerun_of_job_id IS NOT NULL) OR "
            "trigger_type = 'repair' OR "
            "(trigger_type IN ('manual','revision_published','eval') "
            "AND rerun_of_job_id IS NULL)",
            name="ck_graph_extraction_jobs_rerun_scope",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_graph_extraction_jobs_idempotency_key",
        ),
    )
    op.create_index(
        "ix_graph_extraction_jobs_status_created",
        "graph_extraction_jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_graph_extraction_jobs_library_status_created",
        "graph_extraction_jobs",
        ["library_id", "status", "created_at"],
    )
    op.create_index(
        "ix_graph_extraction_jobs_revision_created",
        "graph_extraction_jobs",
        ["document_revision_id", "created_at"],
    )
    op.create_index(
        "ix_graph_extraction_jobs_rerun_of",
        "graph_extraction_jobs",
        ["rerun_of_job_id"],
    )
    op.create_index(
        "ix_graph_extraction_jobs_sensitive_purge",
        "graph_extraction_jobs",
        ["sensitive_payload_purged_at", "created_at"],
    )


def _create_graph_extraction_units() -> None:
    op.create_table("graph_extraction_units",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "graph_extraction_jobs.id",
                ondelete="CASCADE",
                name="fk_graph_extraction_units_job",
            ),
            nullable=False,
        ),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "sys_libraries.id",
                ondelete="CASCADE",
                name="fk_graph_extraction_units_library",
            ),
            nullable=False,
        ),
        sa.Column(
            "document_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "document_revisions.id",
                ondelete="RESTRICT",
                name="fk_graph_extraction_units_revision",
            ),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "center_chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "chunks.id",
                ondelete="RESTRICT",
                name="fk_graph_extraction_units_center_chunk",
            ),
            nullable=False,
        ),
        sa.Column(
            "center_evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "evidence_units.id",
                ondelete="RESTRICT",
                name="fk_graph_extraction_units_center_evidence",
            ),
            nullable=False,
        ),
        sa.Column("unit_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="queued"
        ),
        sa.Column(
            "model_attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "retryable", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','processing','succeeded','failed','cancelled')",
            name="ck_graph_extraction_units_status",
        ),
        sa.CheckConstraint(
            "model_attempt_count >= 0",
            name="ck_graph_extraction_units_attempt_count",
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND worker_id IS NOT NULL AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_graph_extraction_units_claim_fields",
        ),
        sa.UniqueConstraint(
            "job_id",
            "center_chunk_id",
            name="uq_graph_extraction_units_job_chunk",
        ),
        sa.UniqueConstraint(
            "job_id",
            "ordinal",
            name="uq_graph_extraction_units_job_ordinal",
        ),
        sa.UniqueConstraint(
            "job_id",
            "unit_fingerprint",
            name="uq_graph_extraction_units_job_fingerprint",
        ),
    )
    op.create_index(
        "ix_graph_extraction_units_claimable",
        "graph_extraction_units",
        ["status", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_graph_extraction_units_job_status_ordinal",
        "graph_extraction_units",
        ["job_id", "status", "ordinal"],
    )


def _create_extraction_context_snapshots() -> None:
    op.create_table("extraction_context_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "graph_extraction_jobs.id",
                ondelete="CASCADE",
                name="fk_extraction_context_snapshots_job",
            ),
            nullable=False,
        ),
        sa.Column(
            "extraction_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "graph_extraction_units.id",
                ondelete="CASCADE",
                name="fk_extraction_context_snapshots_unit",
            ),
            nullable=False,
        ),
        sa.Column(
            "center_chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "chunks.id",
                ondelete="RESTRICT",
                name="fk_extraction_context_snapshots_chunk",
            ),
            nullable=False,
        ),
        sa.Column(
            "center_evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "evidence_units.id",
                ondelete="RESTRICT",
                name="fk_extraction_context_snapshots_evidence",
            ),
            nullable=False,
        ),
        sa.Column(
            "previous_chunk_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "next_chunk_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "block_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("title_path_source", sa.String(length=32), nullable=False),
        sa.Column("ontology_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("context_mapping", postgresql.JSONB(), nullable=False),
        sa.Column("context_policy_version", sa.String(length=64), nullable=False),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("context_char_count", sa.Integer(), nullable=False),
        sa.Column("context_json", postgresql.JSONB(), nullable=True),
        sa.Column("context_text", sa.Text(), nullable=True),
        sa.Column("document_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("chunk_title_path", postgresql.JSONB(), nullable=True),
        sa.Column("block_title_path", postgresql.JSONB(), nullable=True),
        sa.Column("effective_title_path", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "extraction_unit_id",
            name="uq_extraction_context_snapshots_unit",
        ),
        sa.CheckConstraint(
            "(purged_at IS NULL AND context_json IS NOT NULL AND context_text IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND context_json IS NULL AND context_text IS NULL "
            "AND document_metadata IS NULL AND chunk_title_path IS NULL "
            "AND block_title_path IS NULL AND effective_title_path IS NULL)",
            name="ck_extraction_context_snapshots_payload_or_purged",
        ),
    )
    op.create_index(
        "ix_extraction_context_snapshots_job",
        "extraction_context_snapshots",
        ["job_id"],
    )
    op.create_index(
        "ix_extraction_context_snapshots_purge",
        "extraction_context_snapshots",
        ["purged_at", "created_at"],
    )


def _create_extraction_raw_output_attempts() -> None:
    op.create_table("extraction_raw_output_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "extraction_unit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "graph_extraction_units.id",
                ondelete="CASCADE",
                name="fk_extraction_raw_attempts_unit",
            ),
            nullable=False,
        ),
        sa.Column(
            "context_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "extraction_context_snapshots.id",
                ondelete="CASCADE",
                name="fk_extraction_raw_attempts_context",
            ),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "request_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("parse_status", sa.String(length=32), nullable=True),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column("parsed_response", postgresql.JSONB(), nullable=True),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column("input_token_count", sa.Integer(), nullable=True),
        sa.Column("output_token_count", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("finish_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandoned_reason", sa.String(length=64), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "extraction_unit_id",
            "attempt_no",
            name="uq_extraction_raw_attempts_unit_no",
        ),
        sa.CheckConstraint(
            "attempt_no >= 1",
            name="ck_extraction_raw_attempts_attempt_no",
        ),
        sa.CheckConstraint(
            "request_status IN ('pending','succeeded','abandoned','timeout','network_error','http_error')",
            name="ck_extraction_raw_attempts_request_status",
        ),
        sa.CheckConstraint(
            "parse_status IS NULL OR parse_status IN ('valid','invalid_json','invalid_schema')",
            name="ck_extraction_raw_attempts_parse_status",
        ),
        sa.CheckConstraint(
            "(request_status IN ('pending','abandoned') AND latency_ms IS NULL) OR "
            "(request_status IN ('succeeded','timeout','network_error','http_error') "
            "AND latency_ms IS NOT NULL)",
            name="ck_extraction_raw_attempts_latency",
        ),
        sa.CheckConstraint(
            "(request_status = 'succeeded' AND parse_status IS NOT NULL) OR "
            "(request_status <> 'succeeded' AND parse_status IS NULL)",
            name="ck_extraction_raw_attempts_parse_by_request",
        ),
        sa.CheckConstraint(
            "(request_status = 'abandoned' AND abandoned_at IS NOT NULL "
            "AND abandoned_reason IS NOT NULL) OR "
            "(request_status <> 'abandoned' AND abandoned_at IS NULL "
            "AND abandoned_reason IS NULL)",
            name="ck_extraction_raw_attempts_abandoned_fields",
        ),
        sa.CheckConstraint(
            "abandoned_reason IS NULL OR abandoned_reason IN "
            "('lease_expired','claim_replaced','unit_cancelled')",
            name="ck_extraction_raw_attempts_abandoned_reason",
        ),
        sa.CheckConstraint(
            "purged_at IS NULL OR (raw_response IS NULL AND parsed_response IS NULL "
            "AND parse_error IS NULL)",
            name="ck_extraction_raw_attempts_payload_or_purged",
        ),
    )
    op.create_index(
        "ix_extraction_raw_attempts_unit_no",
        "extraction_raw_output_attempts",
        ["extraction_unit_id", sa.text("attempt_no DESC")],
    )
    op.create_index(
        "ix_extraction_raw_attempts_pending_claim",
        "extraction_raw_output_attempts",
        ["claim_token"],
        postgresql_where=sa.text("request_status = 'pending'"),
    )
    op.create_index(
        "ix_extraction_raw_attempts_purge",
        "extraction_raw_output_attempts",
        ["purged_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extraction_raw_attempts_purge",
        table_name="extraction_raw_output_attempts",
    )
    op.drop_index(
        "ix_extraction_raw_attempts_pending_claim",
        table_name="extraction_raw_output_attempts",
    )
    op.drop_index(
        "ix_extraction_raw_attempts_unit_no",
        table_name="extraction_raw_output_attempts",
    )
    op.drop_table("extraction_raw_output_attempts")

    op.drop_index(
        "ix_extraction_context_snapshots_purge",
        table_name="extraction_context_snapshots",
    )
    op.drop_index(
        "ix_extraction_context_snapshots_job",
        table_name="extraction_context_snapshots",
    )
    op.drop_table("extraction_context_snapshots")

    op.drop_index(
        "ix_graph_extraction_units_job_status_ordinal",
        table_name="graph_extraction_units",
    )
    op.drop_index(
        "ix_graph_extraction_units_claimable",
        table_name="graph_extraction_units",
    )
    op.drop_table("graph_extraction_units")

    op.drop_index(
        "ix_graph_extraction_jobs_sensitive_purge",
        table_name="graph_extraction_jobs",
    )
    op.drop_index(
        "ix_graph_extraction_jobs_rerun_of",
        table_name="graph_extraction_jobs",
    )
    op.drop_index(
        "ix_graph_extraction_jobs_revision_created",
        table_name="graph_extraction_jobs",
    )
    op.drop_index(
        "ix_graph_extraction_jobs_library_status_created",
        table_name="graph_extraction_jobs",
    )
    op.drop_index(
        "ix_graph_extraction_jobs_status_created",
        table_name="graph_extraction_jobs",
    )
    op.drop_table("graph_extraction_jobs")

    op.execute(
        "DELETE FROM service_heartbeats WHERE service_type = 'graph_extractor'"
    )
    op.drop_constraint("ck_heartbeat_service_type",
        "service_heartbeats",
        type_="check",
    )
    op.create_check_constraint(
        "ck_heartbeat_service_type",
        "service_heartbeats",
        "service_type IN ('api','embedding_worker','cleanup_worker')",
    )

    op.drop_column("sys_libraries", "graph_extraction_allowed_security_levels")
    op.drop_column("sys_libraries", "external_llm_enabled")
    op.drop_column("sys_libraries", "graph_extraction_enabled")
