"""Add canonical entity identity and resolution decision foundations."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0064"
down_revision: Union[str, None] = "0063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "canonical_entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "library_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("canonical_name", sa.String(length=512), nullable=False),
        sa.Column("normalized_name", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','pending_review','disabled')",
            name="ck_canonical_entities_status",
        ),
        sa.UniqueConstraint("id", "library_id", name="uq_canonical_entities_id_library"),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            ondelete="RESTRICT",
            name="fk_canonical_entities_library",
        ),
    )
    op.create_index("ix_canonical_entities_library_id", "canonical_entities", ["library_id"])
    op.create_index(
        "ix_canonical_entities_library_normalized_status",
        "canonical_entities",
        ["library_id", "normalized_name", "status"],
    )
    op.create_index(
        "ix_canonical_entities_library_status",
        "canonical_entities",
        ["library_id", "status"],
    )

    op.create_unique_constraint("uq_entities_id_library", "entities", ["id", "library_id"])
    op.add_column(
        "entities",
        sa.Column("canonical_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_entities_canonical_entity",
        "entities",
        "canonical_entities",
        ["canonical_entity_id", "library_id"],
        ["id", "library_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_entities_canonical_entity_id", "entities", ["canonical_entity_id"])

    op.create_table(
        "entity_resolution_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("decision_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("graph_entity_candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("canonical_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("observed_name", sa.String(length=512), nullable=False),
        sa.Column("observed_normalized_name", sa.String(length=512), nullable=False),
        sa.Column("observed_type_key", sa.String(length=128), nullable=True),
        sa.Column("identifier_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column(
            "candidate_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("decision_kind", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("method", sa.String(length=64), server_default="none", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column(
            "resolver_version",
            sa.String(length=64),
            server_default="entity_resolution_v1",
            nullable=False,
        ),
        sa.Column("supersedes_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "decision_kind IN ('link_existing','create_new','pending_review','rejected')",
            name="ck_entity_resolution_decisions_kind",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('active','superseded')",
            name="ck_entity_resolution_decisions_lifecycle",
        ),
        sa.CheckConstraint(
            "(decision_kind IN ('link_existing','create_new') AND canonical_entity_id IS NOT NULL) "
            "OR (decision_kind IN ('pending_review','rejected') AND canonical_entity_id IS NULL)",
            name="ck_entity_resolution_decisions_target",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_entity_resolution_decisions_confidence",
        ),
        sa.CheckConstraint(
            "subject_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "decision_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_entity_resolution_decisions_fingerprints",
        ),
        sa.CheckConstraint(
            "decision_kind IN ('link_existing','create_new') OR reason_code IS NOT NULL",
            name="ck_entity_resolution_decisions_reason",
        ),
        sa.CheckConstraint(
            "identifier_snapshot IS NULL OR jsonb_typeof(identifier_snapshot) = 'object'",
            name="ck_entity_resolution_decisions_identifier_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(candidate_snapshot) = 'array' AND jsonb_typeof(evidence_refs) = 'array'",
            name="ck_entity_resolution_decisions_snapshot_json",
        ),
        sa.UniqueConstraint(
            "library_id",
            "decision_fingerprint",
            name="uq_entity_resolution_decisions_library_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            ondelete="RESTRICT",
            name="fk_entity_resolution_decisions_library",
        ),
        sa.ForeignKeyConstraint(
            ["graph_entity_candidate_id"],
            ["graph_entity_candidates.id"],
            ondelete="SET NULL",
            name="fk_entity_resolution_decisions_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            ondelete="SET NULL",
            name="fk_entity_resolution_decisions_entity",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_entity_resolution_decisions_canonical_entity",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id"],
            ["entity_resolution_decisions.id"],
            ondelete="SET NULL",
            name="fk_entity_resolution_decisions_supersedes",
        ),
    )
    op.create_index(
        "ix_entity_resolution_decisions_library_id",
        "entity_resolution_decisions",
        ["library_id"],
    )
    op.create_index(
        "ix_entity_resolution_decisions_library_canonical",
        "entity_resolution_decisions",
        ["library_id", "canonical_entity_id"],
    )
    op.create_index(
        "ix_entity_resolution_decisions_library_subject",
        "entity_resolution_decisions",
        ["library_id", "subject_fingerprint"],
    )
    op.create_index(
        "uq_entity_resolution_decisions_library_subject_active",
        "entity_resolution_decisions",
        ["library_id", "subject_fingerprint"],
        unique=True,
        postgresql_where=sa.text("lifecycle_status = 'active'"),
    )

    # Conservative 1:1 backfill: every historical Entity gets its own
    # CanonicalEntity.  Names, aliases, types, and ontology versions are not
    # used to merge rows.
    op.execute(
        sa.text(
            """
            CREATE TEMP TABLE _p1_1_entity_canonical_backfill (
                entity_id uuid PRIMARY KEY,
                canonical_entity_id uuid NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO _p1_1_entity_canonical_backfill (entity_id, canonical_entity_id)
            SELECT id, gen_random_uuid()
            FROM entities
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO canonical_entities (
                id, library_id, canonical_name, normalized_name, status, created_at, updated_at
            )
            SELECT mapping.canonical_entity_id,
                   entity.library_id,
                   entity.canonical_name,
                   entity.normalized_name,
                   CASE
                       WHEN entity.status = 'pending_review' THEN 'pending_review'
                       WHEN entity.status IN ('rejected', 'stale', 'disabled', 'deleted') THEN 'disabled'
                       ELSE 'active'
                   END,
                   entity.created_at,
                   entity.updated_at
            FROM entities AS entity
            JOIN _p1_1_entity_canonical_backfill AS mapping
              ON mapping.entity_id = entity.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE entities AS entity
            SET canonical_entity_id = mapping.canonical_entity_id
            FROM _p1_1_entity_canonical_backfill AS mapping
            WHERE mapping.entity_id = entity.id
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "uq_entity_resolution_decisions_library_subject_active",
        table_name="entity_resolution_decisions",
    )
    op.drop_index(
        "ix_entity_resolution_decisions_library_subject",
        table_name="entity_resolution_decisions",
    )
    op.drop_index(
        "ix_entity_resolution_decisions_library_canonical",
        table_name="entity_resolution_decisions",
    )
    op.drop_index("ix_entity_resolution_decisions_library_id", table_name="entity_resolution_decisions")
    op.drop_table("entity_resolution_decisions")

    op.drop_index("ix_entities_canonical_entity_id", table_name="entities")
    op.drop_constraint("fk_entities_canonical_entity", "entities", type_="foreignkey")
    op.drop_column("entities", "canonical_entity_id")
    op.drop_constraint("uq_entities_id_library", "entities", type_="unique")

    op.drop_index("ix_canonical_entities_library_status", table_name="canonical_entities")
    op.drop_index(
        "ix_canonical_entities_library_normalized_status",
        table_name="canonical_entities",
    )
    op.drop_index("ix_canonical_entities_library_id", table_name="canonical_entities")
    op.drop_table("canonical_entities")
