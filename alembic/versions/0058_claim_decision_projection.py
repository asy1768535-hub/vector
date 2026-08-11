"""Add immutable raw claim decision projections.

Revision ID: 0058
Revises: 0057
Create Date: 2026-08-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "graph_claim_decisions",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("decision_schema_version", sa.String(length=64), nullable=False),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_occurrence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decision_kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("created_by_kind", sa.String(length=16), nullable=False),
        sa.Column("producer_key", sa.String(length=128), nullable=False),
        sa.Column("producer_version", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "decision_schema_version = 'claim_decision_projection_v1'",
            name="ck_graph_claim_decisions_schema_version",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('mapping_candidate','schema_extension_candidate')",
            name="ck_graph_claim_decisions_kind",
        ),
        sa.CheckConstraint(
            "status = 'pending'",
            name="ck_graph_claim_decisions_status",
        ),
        sa.CheckConstraint(
            "decision_version >= 1 AND revision_no >= 1",
            name="ck_graph_claim_decisions_positive_versions",
        ),
        sa.CheckConstraint(
            "created_by_kind IN ('system','human','external')",
            name="ck_graph_claim_decisions_producer_kind",
        ),
        sa.CheckConstraint(
            "btrim(producer_key) <> ''",
            name="ck_graph_claim_decisions_producer_key",
        ),
        sa.CheckConstraint(
            "((decision_kind = 'mapping_candidate' AND reason_code IN "
            "('unknown_predicate','unknown_direction','ambiguous_mapping')) OR "
            "(decision_kind = 'schema_extension_candidate' AND reason_code IN "
            "('unknown_source_type','unknown_target_type'))) ",
            name="ck_graph_claim_decisions_reason_kind",
        ),
        sa.CheckConstraint(
            "reason_code <> 'unknown_direction' OR proposal->>'surface_direction' = 'unknown'",
            name="ck_graph_claim_decisions_unknown_direction",
        ),
        sa.CheckConstraint(
            "decision_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_graph_claim_decisions_fingerprint",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(proposal) = 'object'",
            name="ck_graph_claim_decisions_proposal_shape",
        ),
        sa.CheckConstraint(
            "pg_column_size(proposal) <= 8192",
            name="ck_graph_claim_decisions_proposal_size",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"], ["sys_libraries.id"], ondelete="RESTRICT", name="fk_graph_claim_decisions_library"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="RESTRICT", name="fk_graph_claim_decisions_document"
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id"],
            ["document_revisions.id"],
            ondelete="RESTRICT",
            name="fk_graph_claim_decisions_revision",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["graph_raw_claims.id"],
            ondelete="RESTRICT",
            name="fk_graph_claim_decisions_claim",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_occurrence_id"],
            ["graph_raw_claim_occurrences.extraction_occurrence_id"],
            ondelete="RESTRICT",
            name="fk_graph_claim_decisions_occurrence",
        ),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint(
            "library_id",
            "document_revision_id",
            "decision_fingerprint",
            name="uq_graph_claim_decisions_scope_fingerprint",
        ),
    )
    op.create_index(
        "ix_graph_claim_decisions_scope_claim",
        "graph_claim_decisions",
        ["library_id", "document_revision_id", "claim_id", "created_at"],
    )
    op.create_index(
        "ix_graph_claim_decisions_kind_status",
        "graph_claim_decisions",
        ["decision_kind", "status", "created_at"],
    )
    op.create_index(
        "ix_graph_claim_decisions_occurrence",
        "graph_claim_decisions",
        ["extraction_occurrence_id", "created_at"],
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_claim_decisions_immutable_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'claim decision projection rows are immutable';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER graph_claim_decisions_immutable_guard
            BEFORE UPDATE OR DELETE ON graph_claim_decisions
            FOR EACH ROW EXECUTE FUNCTION graph_claim_decisions_immutable_guard()
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_claim_decisions_immutable_guard "
            "ON graph_claim_decisions"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_claim_decisions_immutable_guard()"))
    op.drop_index("ix_graph_claim_decisions_occurrence", table_name="graph_claim_decisions")
    op.drop_index("ix_graph_claim_decisions_kind_status", table_name="graph_claim_decisions")
    op.drop_index("ix_graph_claim_decisions_scope_claim", table_name="graph_claim_decisions")
    op.drop_table("graph_claim_decisions")
