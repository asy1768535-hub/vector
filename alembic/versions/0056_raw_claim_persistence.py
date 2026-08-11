"""Add immutable raw claim core and extraction occurrence storage.

Revision ID: 0056
Revises: 0055
Create Date: 2026-08-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "graph_raw_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "claim_schema_version",
            sa.String(length=32),
            server_default="raw_claim_v1",
            nullable=False,
        ),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("content_scoped_claim_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_mention", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_predicate", sa.String(length=256), nullable=False),
        sa.Column("target_mention", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("surface_direction", sa.String(length=32), nullable=False),
        sa.Column("negation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("modality", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("qualifiers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("valid_time", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("effective_time", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "claim_schema_version = 'raw_claim_v1'",
            name="ck_graph_raw_claims_schema_version",
        ),
        sa.CheckConstraint(
            "surface_direction IN ('source_to_target','target_to_source','undirected','unknown')",
            name="ck_graph_raw_claims_direction",
        ),
        sa.CheckConstraint(
            "content_scoped_claim_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_graph_raw_claims_content_fingerprint",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_mention) = 'object' AND "
            "jsonb_typeof(target_mention) = 'object' AND "
            "jsonb_typeof(negation) = 'object' AND "
            "jsonb_typeof(modality) = 'object' AND "
            "jsonb_typeof(qualifiers) = 'array' AND "
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_graph_raw_claims_json_shapes",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            ondelete="RESTRICT",
            name="fk_graph_raw_claims_library",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="RESTRICT",
            name="fk_graph_raw_claims_document",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id"],
            ["document_revisions.id"],
            ondelete="RESTRICT",
            name="fk_graph_raw_claims_revision",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "library_id",
            "document_revision_id",
            "content_scoped_claim_fingerprint",
            name="uq_graph_raw_claims_scope_content_fingerprint",
        ),
    )
    op.create_index(
        "ix_graph_raw_claims_library_revision",
        "graph_raw_claims",
        ["library_id", "document_revision_id", "created_at"],
    )

    op.create_table(
        "graph_raw_claim_occurrences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_occurrence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_occurrence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extractor_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("model_provider", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("model_config_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_content_hash", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("normalization_rule_version", sa.String(length=64), nullable=False),
        sa.Column("ontology_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "extraction_occurrence_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_graph_raw_claim_occurrences_fingerprint",
        ),
        sa.CheckConstraint(
            "model_config_hash ~ '^[0-9a-f]{64}$' AND "
            "prompt_content_hash ~ '^[0-9a-f]{64}$' AND "
            "(ontology_snapshot_hash IS NULL OR ontology_snapshot_hash ~ '^[0-9a-f]{64}$')",
            name="ck_graph_raw_claim_occurrences_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["graph_raw_claims.id"],
            ondelete="RESTRICT",
            name="fk_graph_raw_claim_occurrences_claim",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["graph_extraction_jobs.id"],
            ondelete="RESTRICT",
            name="fk_graph_raw_claim_occurrences_job",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_unit_id"],
            ["graph_extraction_units.id"],
            ondelete="RESTRICT",
            name="fk_graph_raw_claim_occurrences_unit",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_occurrence_id",
            name="uq_graph_raw_claim_occurrences_occurrence_id",
        ),
        sa.UniqueConstraint(
            "extraction_occurrence_fingerprint",
            name="uq_graph_raw_claim_occurrences_fingerprint",
        ),
    )
    op.create_index(
        "ix_graph_raw_claim_occurrences_job_unit",
        "graph_raw_claim_occurrences",
        ["job_id", "extraction_unit_id", "created_at"],
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_raw_claims_immutable_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'raw claim persistence rows are immutable';
            END;
            $$
            """
        )
    )
    for table in ("graph_raw_claims", "graph_raw_claim_occurrences"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER {table}_immutable_guard "
                f"BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION graph_raw_claims_immutable_guard()"
            )
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_raw_claim_occurrences_immutable_guard "
            "ON graph_raw_claim_occurrences"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_raw_claims_immutable_guard ON graph_raw_claims"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_raw_claims_immutable_guard()"))
    op.drop_index(
        "ix_graph_raw_claim_occurrences_job_unit",
        table_name="graph_raw_claim_occurrences",
    )
    op.drop_table("graph_raw_claim_occurrences")
    op.drop_index("ix_graph_raw_claims_library_revision", table_name="graph_raw_claims")
    op.drop_table("graph_raw_claims")
