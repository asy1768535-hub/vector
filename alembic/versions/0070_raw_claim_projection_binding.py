"""Add append-only explicit RawClaim-to-candidate projection bindings."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0070"
down_revision: Union[str, None] = "0069"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "graph_raw_claim_projection_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raw_claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raw_claim_occurrence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("graph_relation_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("binding_contract_version", sa.String(length=64), nullable=False),
        sa.Column("binding_method", sa.String(length=64), nullable=False),
        sa.Column("binding_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "binding_contract_version = 'raw_claim_projection_binding_v1'",
            name="ck_raw_claim_projection_bindings_contract_version",
        ),
        sa.CheckConstraint(
            "binding_method = 'explicit_shadow_projection_ref_v1'",
            name="ck_raw_claim_projection_bindings_method",
        ),
        sa.CheckConstraint(
            "binding_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_raw_claim_projection_bindings_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["raw_claim_id"],
            ["graph_raw_claims.id"],
            ondelete="RESTRICT",
            name="fk_raw_claim_projection_bindings_claim",
        ),
        sa.ForeignKeyConstraint(
            ["raw_claim_occurrence_id"],
            ["graph_raw_claim_occurrences.extraction_occurrence_id"],
            ondelete="RESTRICT",
            name="fk_raw_claim_projection_bindings_occurrence",
        ),
        sa.ForeignKeyConstraint(
            ["graph_relation_candidate_id"],
            ["graph_relation_candidates.id"],
            ondelete="RESTRICT",
            name="fk_raw_claim_projection_bindings_candidate",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "raw_claim_occurrence_id",
            "graph_relation_candidate_id",
            name="uq_raw_claim_projection_bindings_occurrence_candidate",
        ),
        sa.UniqueConstraint(
            "binding_fingerprint", name="uq_raw_claim_projection_bindings_fingerprint"
        ),
    )
    op.create_index(
        "ix_raw_claim_projection_bindings_claim_occurrence",
        "graph_raw_claim_projection_bindings",
        ["raw_claim_id", "raw_claim_occurrence_id", "created_at"],
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_raw_claim_projection_bindings_immutable_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'raw claim projection binding rows are immutable';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER graph_raw_claim_projection_bindings_immutable_guard
            BEFORE UPDATE OR DELETE ON graph_raw_claim_projection_bindings
            FOR EACH ROW EXECUTE FUNCTION graph_raw_claim_projection_bindings_immutable_guard()
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_raw_claim_projection_bindings_immutable_guard "
            "ON graph_raw_claim_projection_bindings"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_raw_claim_projection_bindings_immutable_guard()"))
    op.drop_index(
        "ix_raw_claim_projection_bindings_claim_occurrence",
        table_name="graph_raw_claim_projection_bindings",
    )
    op.drop_table("graph_raw_claim_projection_bindings")
