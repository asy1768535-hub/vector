"""Add append-only CanonicalEntity evolution lineage."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_entity_resolution_decisions_id_library",
        "entity_resolution_decisions",
        ["id", "library_id"],
    )
    op.create_table(
        "canonical_entity_evolution_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("operation_kind", sa.String(length=32), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("supersedes_command_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "operation_kind IN ('merge','split','reassign')",
            name="ck_canonical_entity_evolution_commands_operation",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_canonical_entity_evolution_commands_fingerprint",
        ),
        sa.ForeignKeyConstraint(["library_id"], ["sys_libraries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_command_id", "library_id"],
            ["canonical_entity_evolution_commands.id", "canonical_entity_evolution_commands.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_commands_supersedes",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "library_id", name="uq_canonical_entity_evolution_commands_id_library"),
        sa.UniqueConstraint("library_id", "idempotency_key", name="uq_canonical_entity_evolution_commands_idempotency"),
        sa.UniqueConstraint("library_id", "supersedes_command_id", name="uq_canonical_entity_evolution_commands_supersedes"),
    )
    op.create_index(
        "ix_canonical_entity_evolution_commands_library_id",
        "canonical_entity_evolution_commands",
        ["library_id"],
    )
    op.create_table(
        "canonical_entity_evolution_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_kind", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason_text", sa.String(length=512), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("precondition_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("supersedes_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "operation_kind IN ('merge','split','reassign')",
            name="ck_canonical_entity_evolution_decisions_operation",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('pending','applied','rejected','stale','superseded')",
            name="ck_canonical_entity_evolution_decisions_lifecycle",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_canonical_entity_evolution_decisions_confidence",
        ),
        sa.CheckConstraint(
            "precondition_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_canonical_entity_evolution_decisions_precondition",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_canonical_entity_evolution_decisions_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["command_id", "library_id"],
            ["canonical_entity_evolution_commands.id", "canonical_entity_evolution_commands.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_decisions_command",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id", "library_id", "command_id"],
            [
                "canonical_entity_evolution_decisions.id",
                "canonical_entity_evolution_decisions.library_id",
                "canonical_entity_evolution_decisions.command_id",
            ],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_decisions_supersedes",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "library_id", name="uq_canonical_entity_evolution_decisions_id_library"),
        sa.UniqueConstraint(
            "id", "library_id", "command_id", name="uq_canonical_entity_evolution_decisions_id_library_command"
        ),
        sa.UniqueConstraint("library_id", "supersedes_decision_id", name="uq_canonical_entity_evolution_decisions_supersedes"),
    )
    op.create_index(
        "ix_canonical_entity_evolution_decisions_library_id",
        "canonical_entity_evolution_decisions",
        ["library_id"],
    )
    op.create_index(
        "uq_canonical_entity_evolution_decisions_pending",
        "canonical_entity_evolution_decisions",
        ["library_id", "command_id"],
        unique=True,
        postgresql_where=sa.text("lifecycle_status = 'pending'"),
    )
    op.create_index(
        "uq_canonical_entity_evolution_decisions_applied",
        "canonical_entity_evolution_decisions",
        ["library_id", "command_id"],
        unique=True,
        postgresql_where=sa.text("lifecycle_status = 'applied'"),
    )
    op.create_table(
        "canonical_entity_evolution_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evolution_decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_canonical_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resolution_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "resolution_state IN ('pending','applied','superseded','historical_only')",
            name="ck_canonical_entity_evolution_sources_state",
        ),
        sa.ForeignKeyConstraint(
            ["evolution_decision_id", "library_id"],
            ["canonical_entity_evolution_decisions.id", "canonical_entity_evolution_decisions.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_sources_decision",
        ),
        sa.ForeignKeyConstraint(
            ["source_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_sources_canonical",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "library_id", name="uq_canonical_entity_evolution_sources_id_library"),
        sa.UniqueConstraint(
            "library_id", "evolution_decision_id", "source_canonical_entity_id",
            name="uq_canonical_entity_evolution_sources_decision_source",
        ),
    )
    op.create_index(
        "ix_canonical_entity_evolution_sources_library_id",
        "canonical_entity_evolution_sources",
        ["library_id"],
    )
    op.create_index(
        "uq_canonical_entity_evolution_sources_current",
        "canonical_entity_evolution_sources",
        ["library_id", "source_canonical_entity_id"],
        unique=True,
        postgresql_where=sa.text("resolution_state IN ('pending','applied')"),
    )
    op.create_table(
        "canonical_entity_evolution_successors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_transition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_canonical_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_spec_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "target_spec_snapshot IS NULL OR jsonb_typeof(target_spec_snapshot) = 'object'",
            name="ck_canonical_entity_evolution_successors_target_spec",
        ),
        sa.ForeignKeyConstraint(
            ["source_transition_id", "library_id"],
            ["canonical_entity_evolution_sources.id", "canonical_entity_evolution_sources.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_successors_source",
        ),
        sa.ForeignKeyConstraint(
            ["target_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_successors_target",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "library_id", "source_transition_id", "target_canonical_entity_id",
            name="uq_canonical_entity_evolution_successors_target",
        ),
    )
    op.create_index(
        "ix_canonical_entity_evolution_successors_library_id",
        "canonical_entity_evolution_successors",
        ["library_id"],
    )
    op.create_table(
        "canonical_entity_projection_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evolution_decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_canonical_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_canonical_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assignment_state", sa.String(length=32), nullable=False),
        sa.Column("partition_basis_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("previous_entity_resolution_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("new_entity_resolution_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "assignment_state IN ('resolved','pending','rejected')",
            name="ck_canonical_entity_projection_assignments_state",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(partition_basis_snapshot) = 'object'",
            name="ck_canonical_entity_projection_assignments_basis",
        ),
        sa.ForeignKeyConstraint(
            ["evolution_decision_id", "library_id"],
            ["canonical_entity_evolution_decisions.id", "canonical_entity_evolution_decisions.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_decision",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id", "library_id"],
            ["entities.id", "entities.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_entity",
        ),
        sa.ForeignKeyConstraint(
            ["from_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_from",
        ),
        sa.ForeignKeyConstraint(
            ["target_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_target",
        ),
        sa.ForeignKeyConstraint(
            ["previous_entity_resolution_decision_id", "library_id"],
            ["entity_resolution_decisions.id", "entity_resolution_decisions.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_previous_decision",
        ),
        sa.ForeignKeyConstraint(
            ["new_entity_resolution_decision_id", "library_id"],
            ["entity_resolution_decisions.id", "entity_resolution_decisions.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_new_decision",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "library_id", "evolution_decision_id", "entity_id",
            name="uq_canonical_entity_projection_assignments_decision_entity",
        ),
    )
    op.create_index(
        "ix_canonical_entity_projection_assignments_library_id",
        "canonical_entity_projection_assignments",
        ["library_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_canonical_entity_projection_assignments_library_id", table_name="canonical_entity_projection_assignments")
    op.drop_table("canonical_entity_projection_assignments")
    op.drop_index("ix_canonical_entity_evolution_successors_library_id", table_name="canonical_entity_evolution_successors")
    op.drop_table("canonical_entity_evolution_successors")
    op.drop_index("uq_canonical_entity_evolution_sources_current", table_name="canonical_entity_evolution_sources")
    op.drop_index("ix_canonical_entity_evolution_sources_library_id", table_name="canonical_entity_evolution_sources")
    op.drop_table("canonical_entity_evolution_sources")
    op.drop_index("uq_canonical_entity_evolution_decisions_applied", table_name="canonical_entity_evolution_decisions")
    op.drop_index("uq_canonical_entity_evolution_decisions_pending", table_name="canonical_entity_evolution_decisions")
    op.drop_index("ix_canonical_entity_evolution_decisions_library_id", table_name="canonical_entity_evolution_decisions")
    op.drop_table("canonical_entity_evolution_decisions")
    op.drop_index("ix_canonical_entity_evolution_commands_library_id", table_name="canonical_entity_evolution_commands")
    op.drop_table("canonical_entity_evolution_commands")
    op.drop_constraint(
        "uq_entity_resolution_decisions_id_library",
        "entity_resolution_decisions",
        type_="unique",
    )
