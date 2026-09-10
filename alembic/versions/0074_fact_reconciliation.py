"""Add append-only LogicalFact reconciliation lineage.

Revision 0074 is maintenance-only. The lock blocks are emitted unchanged in
online and offline mode so failed admission cannot expose a partial schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIT_TABLES = (
    "fact_reconciliation_commands",
    "fact_reconciliation_decisions",
    "fact_reconciliation_sources",
    "fact_reconciliation_target_slots",
    "fact_reconciliation_source_target_edges",
    "fact_reconciliation_assertion_assignments",
)

_UPGRADE_LOCK_SQL = """
DO $p3_3_migration_lock$
BEGIN
    IF NOT pg_try_advisory_xact_lock(3410968108221770891) THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'migration_busy';
    END IF;
    BEGIN
        LOCK TABLE sys_libraries IN SHARE MODE NOWAIT;
        LOCK TABLE canonical_entities IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE entities IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE entity_resolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_identities IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mappings IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE relation_types IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE logical_facts IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_assertions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_resolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE knowledge_relations IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE relation_evidence IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_commands IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_successors IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_projection_assignments IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_commands IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_successors IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mapping_evolution_assignments IN ACCESS EXCLUSIVE MODE NOWAIT;
    EXCEPTION WHEN lock_not_available THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'migration_busy';
    END;
END
$p3_3_migration_lock$;
"""

_DOWNGRADE_LOCK_SQL = """
DO $p3_3_downgrade_lock$
BEGIN
    IF NOT pg_try_advisory_xact_lock(3410968108221770891) THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'migration_busy';
    END IF;
    BEGIN
        LOCK TABLE sys_libraries IN SHARE MODE NOWAIT;
        LOCK TABLE canonical_entities IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE entities IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE entity_resolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_identities IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mappings IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE relation_types IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE logical_facts IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_assertions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_resolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE knowledge_relations IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE relation_evidence IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_commands IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_successors IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_projection_assignments IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_commands IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_successors IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mapping_evolution_assignments IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_reconciliation_commands IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_reconciliation_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_reconciliation_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_reconciliation_target_slots IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_reconciliation_source_target_edges IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_reconciliation_assertion_assignments IN ACCESS EXCLUSIVE MODE NOWAIT;
    EXCEPTION WHEN lock_not_available THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'migration_busy';
    END;
END
$p3_3_downgrade_lock$;
"""


def _set_timeouts() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")


def _assert_preconditions() -> None:
    op.execute("""
DO $$
BEGIN
  IF to_regclass('sys_libraries') IS NULL
     OR to_regclass('canonical_entities') IS NULL
     OR to_regclass('entities') IS NULL
     OR to_regclass('entity_resolution_decisions') IS NULL
     OR to_regclass('stable_predicate_identities') IS NULL
     OR to_regclass('stable_predicate_mappings') IS NULL
     OR to_regclass('relation_types') IS NULL
     OR to_regclass('logical_facts') IS NULL
     OR to_regclass('fact_assertions') IS NULL
     OR to_regclass('fact_resolution_decisions') IS NULL
     OR to_regclass('knowledge_relations') IS NULL
     OR to_regclass('relation_evidence') IS NULL
     OR to_regclass('canonical_entity_evolution_commands') IS NULL
     OR to_regclass('stable_predicate_evolution_commands') IS NULL
     OR to_regclass('fact_reconciliation_commands') IS NOT NULL
     OR to_regclass('fact_reconciliation_decisions') IS NOT NULL
     OR to_regclass('fact_reconciliation_sources') IS NOT NULL
     OR to_regclass('fact_reconciliation_target_slots') IS NOT NULL
     OR to_regclass('fact_reconciliation_source_target_edges') IS NOT NULL
     OR to_regclass('fact_reconciliation_assertion_assignments') IS NOT NULL
     OR EXISTS (
       SELECT 1 FROM information_schema.columns
       WHERE table_schema=current_schema() AND table_name='logical_facts'
         AND column_name='reconciliation_target_slot_id'
     )
  THEN RAISE EXCEPTION 'P3_3_0074_PRECONDITION_MISMATCH'; END IF;
END $$;
""")


def _create_tables() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB()
    op.create_table(
        "fact_reconciliation_commands",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("command_identity_fingerprint", sa.String(64), nullable=False),
        sa.Column("operation_kind", sa.String(32), nullable=False, server_default="reconcile"),
        sa.Column("command_payload_snapshot", jsonb, nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("operation_kind = 'reconcile'", name="ck_fact_reconciliation_commands_operation"),
        sa.CheckConstraint("command_identity_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_fact_reconciliation_commands_fingerprint"),
        sa.CheckConstraint("jsonb_typeof(command_payload_snapshot) = 'object' AND octet_length(command_payload_snapshot::text) <= 262144", name="ck_fact_reconciliation_commands_payload"),
        sa.CheckConstraint("contract_version = 'p3_3_fact_reconciliation/v1'", name="ck_fact_reconciliation_commands_contract"),
        sa.ForeignKeyConstraint(["library_id"], ["sys_libraries.id"], ondelete="RESTRICT", name="fk_fact_reconciliation_commands_library"),
        sa.UniqueConstraint("id", "library_id", name="uq_fact_reconciliation_commands_id_library"),
        sa.UniqueConstraint("library_id", "idempotency_key", name="uq_fact_reconciliation_commands_idempotency"),
        sa.UniqueConstraint("library_id", "command_identity_fingerprint", name="uq_fact_reconciliation_commands_identity"),
    )
    op.create_index("ix_fact_reconciliation_commands_library", "fact_reconciliation_commands", ["library_id"])

    op.create_table(
        "fact_reconciliation_decisions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("decision_payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("operation_kind", sa.String(32), nullable=False, server_default="reconcile"),
        sa.Column("requested_effect", sa.String(16), nullable=False),
        sa.Column("evaluated_outcome", sa.String(16), nullable=False),
        sa.Column("lifecycle_status", sa.String(16), nullable=False),
        sa.Column("operation_payload_snapshot", jsonb, nullable=False),
        sa.Column("expected_precondition_fingerprint", sa.String(64), nullable=False),
        sa.Column("observed_precondition_fingerprint", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("reason_text", sa.String(1024), nullable=False),
        sa.Column("method", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("evidence_refs", jsonb, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("supersedes_decision_id", uuid, nullable=True),
        sa.Column("actor_type", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(256), nullable=False),
        sa.Column("request_id", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("operation_kind = 'reconcile'", name="ck_fact_reconciliation_decisions_operation"),
        sa.CheckConstraint("requested_effect IN ('stage','apply','cancel')", name="ck_fact_reconciliation_decisions_effect"),
        sa.CheckConstraint("evaluated_outcome IN ('pending','applied','rejected','stale','cancelled')", name="ck_fact_reconciliation_decisions_outcome"),
        sa.CheckConstraint("lifecycle_status IN ('pending','applied','rejected','stale','cancelled','superseded')", name="ck_fact_reconciliation_decisions_lifecycle"),
        sa.CheckConstraint("decision_payload_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_fact_reconciliation_decisions_payload_fingerprint"),
        sa.CheckConstraint("expected_precondition_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_fact_reconciliation_decisions_expected_fingerprint"),
        sa.CheckConstraint("observed_precondition_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_fact_reconciliation_decisions_observed_fingerprint"),
        sa.CheckConstraint("jsonb_typeof(operation_payload_snapshot) = 'object' AND octet_length(operation_payload_snapshot::text) <= 262144", name="ck_fact_reconciliation_decisions_payload"),
        sa.CheckConstraint("jsonb_typeof(evidence_refs) = 'array' AND octet_length(evidence_refs::text) <= 65536", name="ck_fact_reconciliation_decisions_evidence_refs"),
        sa.CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_fact_reconciliation_decisions_confidence"),
        sa.CheckConstraint("lifecycle_status = evaluated_outcome OR lifecycle_status = 'superseded'", name="ck_fact_reconciliation_decisions_status_shape"),
        sa.ForeignKeyConstraint(["command_id", "library_id"], ["fact_reconciliation_commands.id", "fact_reconciliation_commands.library_id"], ondelete="RESTRICT", name="fk_fact_reconciliation_decisions_command"),
        sa.UniqueConstraint("id", "library_id", "command_id", name="uq_fact_reconciliation_decisions_owner"),
        sa.UniqueConstraint("library_id", "command_id", "decision_payload_fingerprint", name="uq_fact_reconciliation_decisions_payload"),
    )
    op.create_foreign_key(
        "fk_fact_reconciliation_decisions_supersedes",
        "fact_reconciliation_decisions",
        "fact_reconciliation_decisions",
        ["supersedes_decision_id", "library_id", "command_id"],
        ["id", "library_id", "command_id"],
        ondelete="RESTRICT",
    )
    op.create_index("uq_fact_reconciliation_decisions_root", "fact_reconciliation_decisions", ["library_id", "command_id"], unique=True, postgresql_where=sa.text("supersedes_decision_id IS NULL"))
    op.create_index("uq_fact_reconciliation_decisions_superseded_by", "fact_reconciliation_decisions", ["library_id", "supersedes_decision_id"], unique=True, postgresql_where=sa.text("supersedes_decision_id IS NOT NULL"))

    op.create_table(
        "fact_reconciliation_sources",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("evolution_decision_id", uuid, nullable=False),
        sa.Column("source_logical_fact_id", uuid, nullable=False),
        sa.Column("supersedes_source_transition_id", uuid, nullable=True),
        sa.Column("resolution_state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("resolution_state IN ('pending','applied','superseded','historical_only')", name="ck_fact_reconciliation_sources_state"),
        sa.CheckConstraint("supersedes_source_transition_id IS NULL OR supersedes_source_transition_id <> id", name="ck_fact_reconciliation_sources_not_self_predecessor"),
        sa.ForeignKeyConstraint(["evolution_decision_id", "library_id", "command_id"], ["fact_reconciliation_decisions.id", "fact_reconciliation_decisions.library_id", "fact_reconciliation_decisions.command_id"], ondelete="RESTRICT", name="fk_fact_reconciliation_sources_decision"),
        sa.ForeignKeyConstraint(["source_logical_fact_id", "library_id"], ["logical_facts.id", "logical_facts.library_id"], ondelete="RESTRICT", name="fk_fact_reconciliation_sources_logical_fact"),
        sa.UniqueConstraint("id", "library_id", "command_id", "source_logical_fact_id", name="uq_fact_reconciliation_sources_owner_source"),
        sa.UniqueConstraint("id", "library_id", "command_id", "evolution_decision_id", "source_logical_fact_id", name="uq_fact_reconciliation_sources_owner_decision_source"),
        sa.UniqueConstraint("library_id", "evolution_decision_id", "source_logical_fact_id", name="uq_fact_reconciliation_sources_decision_source"),
    )
    op.create_foreign_key("fk_fact_reconciliation_sources_supersedes", "fact_reconciliation_sources", "fact_reconciliation_sources", ["supersedes_source_transition_id", "library_id", "command_id", "source_logical_fact_id"], ["id", "library_id", "command_id", "source_logical_fact_id"], ondelete="RESTRICT")
    op.create_index("uq_fact_reconciliation_sources_root", "fact_reconciliation_sources", ["library_id", "command_id", "source_logical_fact_id"], unique=True, postgresql_where=sa.text("supersedes_source_transition_id IS NULL"))
    op.create_index("uq_fact_reconciliation_sources_superseded_by", "fact_reconciliation_sources", ["library_id", "supersedes_source_transition_id"], unique=True, postgresql_where=sa.text("supersedes_source_transition_id IS NOT NULL"))
    op.create_index("uq_fact_reconciliation_sources_live", "fact_reconciliation_sources", ["library_id", "source_logical_fact_id"], unique=True, postgresql_where=sa.text("resolution_state IN ('pending','applied')"))

    op.create_table(
        "fact_reconciliation_target_slots",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("evolution_decision_id", uuid, nullable=False),
        sa.Column("target_key", sa.String(256), nullable=False),
        sa.Column("target_ref_kind", sa.String(16), nullable=False),
        sa.Column("existing_logical_fact_id", uuid, nullable=True),
        sa.Column("target_spec_snapshot", jsonb, nullable=False),
        sa.Column("target_identity_fingerprint", sa.String(64), nullable=False),
        sa.Column("planned_target_logical_fact_id", uuid, nullable=True),
        sa.Column("target_logical_fact_id", uuid, nullable=True),
        sa.Column("slot_state", sa.String(16), nullable=False),
        sa.Column("supersedes_target_slot_id", uuid, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("target_ref_kind IN ('existing','new')", name="ck_fact_reconciliation_target_slots_kind"),
        sa.CheckConstraint("target_identity_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_fact_reconciliation_target_slots_fingerprint"),
        sa.CheckConstraint("jsonb_typeof(target_spec_snapshot) = 'object' AND octet_length(target_spec_snapshot::text) <= 65536", name="ck_fact_reconciliation_target_slots_spec"),
        sa.CheckConstraint("slot_state IN ('pending','applied','superseded')", name="ck_fact_reconciliation_target_slots_state"),
        sa.CheckConstraint("supersedes_target_slot_id IS NULL OR supersedes_target_slot_id <> id", name="ck_fact_reconciliation_target_slots_not_self_predecessor"),
        sa.CheckConstraint("(target_ref_kind = 'existing' AND existing_logical_fact_id IS NOT NULL AND planned_target_logical_fact_id IS NULL AND target_logical_fact_id = existing_logical_fact_id) OR (target_ref_kind = 'new' AND existing_logical_fact_id IS NULL AND planned_target_logical_fact_id IS NOT NULL AND (target_logical_fact_id IS NULL OR target_logical_fact_id = planned_target_logical_fact_id))", name="ck_fact_reconciliation_target_slots_shape"),
        sa.ForeignKeyConstraint(["evolution_decision_id", "library_id", "command_id"], ["fact_reconciliation_decisions.id", "fact_reconciliation_decisions.library_id", "fact_reconciliation_decisions.command_id"], ondelete="RESTRICT", name="fk_fact_reconciliation_target_slots_decision"),
        sa.ForeignKeyConstraint(["existing_logical_fact_id", "library_id"], ["logical_facts.id", "logical_facts.library_id"], ondelete="RESTRICT", name="fk_fact_reconciliation_target_slots_existing_fact"),
        sa.ForeignKeyConstraint(["target_logical_fact_id", "library_id"], ["logical_facts.id", "logical_facts.library_id"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED", name="fk_fact_reconciliation_target_slots_target_fact"),
        sa.UniqueConstraint("id", "library_id", name="uq_fact_reconciliation_target_slots_id_library"),
        sa.UniqueConstraint("id", "library_id", "command_id", "target_key", name="uq_fact_reconciliation_target_slots_owner_key"),
        sa.UniqueConstraint("id", "library_id", "command_id", "evolution_decision_id", "target_key", name="uq_fact_reconciliation_target_slots_owner_decision_key"),
        sa.UniqueConstraint("library_id", "evolution_decision_id", "target_key", name="uq_fact_reconciliation_target_slots_decision_key"),
    )
    op.create_foreign_key("fk_fact_reconciliation_target_slots_supersedes", "fact_reconciliation_target_slots", "fact_reconciliation_target_slots", ["supersedes_target_slot_id", "library_id", "command_id", "target_key"], ["id", "library_id", "command_id", "target_key"], ondelete="RESTRICT")
    op.create_index("uq_fact_reconciliation_target_slots_root", "fact_reconciliation_target_slots", ["library_id", "command_id", "target_key"], unique=True, postgresql_where=sa.text("supersedes_target_slot_id IS NULL"))
    op.create_index("uq_fact_reconciliation_target_slots_superseded_by", "fact_reconciliation_target_slots", ["library_id", "supersedes_target_slot_id"], unique=True, postgresql_where=sa.text("supersedes_target_slot_id IS NOT NULL"))

    op.create_table(
        "fact_reconciliation_source_target_edges",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("evolution_decision_id", uuid, nullable=False),
        sa.Column("source_logical_fact_id", uuid, nullable=False),
        sa.Column("source_transition_id", uuid, nullable=False),
        sa.Column("target_key", sa.String(256), nullable=False),
        sa.Column("target_slot_id", uuid, nullable=False),
        sa.Column("edge_state", sa.String(16), nullable=False),
        sa.Column("supersedes_source_target_edge_id", uuid, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("edge_state IN ('pending','applied','superseded')", name="ck_fact_reconciliation_source_target_edges_state"),
        sa.CheckConstraint("supersedes_source_target_edge_id IS NULL OR supersedes_source_target_edge_id <> id", name="ck_fact_reconciliation_source_target_edges_not_self_predecessor"),
        sa.ForeignKeyConstraint(["evolution_decision_id", "library_id", "command_id"], ["fact_reconciliation_decisions.id", "fact_reconciliation_decisions.library_id", "fact_reconciliation_decisions.command_id"], ondelete="RESTRICT", name="fk_fact_reconciliation_source_target_edges_decision"),
        sa.ForeignKeyConstraint(["source_transition_id", "library_id", "command_id", "evolution_decision_id", "source_logical_fact_id"], ["fact_reconciliation_sources.id", "fact_reconciliation_sources.library_id", "fact_reconciliation_sources.command_id", "fact_reconciliation_sources.evolution_decision_id", "fact_reconciliation_sources.source_logical_fact_id"], ondelete="RESTRICT", name="fk_fact_reconciliation_source_target_edges_source"),
        sa.ForeignKeyConstraint(["target_slot_id", "library_id", "command_id", "evolution_decision_id", "target_key"], ["fact_reconciliation_target_slots.id", "fact_reconciliation_target_slots.library_id", "fact_reconciliation_target_slots.command_id", "fact_reconciliation_target_slots.evolution_decision_id", "fact_reconciliation_target_slots.target_key"], ondelete="RESTRICT", name="fk_fact_reconciliation_source_target_edges_target_slot"),
        sa.UniqueConstraint("id", "library_id", "command_id", "evolution_decision_id", "source_logical_fact_id", "target_key", name="uq_fact_reconciliation_source_target_edges_owner"),
        sa.UniqueConstraint("id", "library_id", "command_id", "source_logical_fact_id", "target_key", name="uq_fact_reconciliation_source_target_edges_owner_key"),
        sa.UniqueConstraint("library_id", "evolution_decision_id", "source_transition_id", "target_slot_id", name="uq_fact_reconciliation_source_target_edges_decision_pair"),
    )
    op.create_foreign_key("fk_fact_reconciliation_source_target_edges_supersedes", "fact_reconciliation_source_target_edges", "fact_reconciliation_source_target_edges", ["supersedes_source_target_edge_id", "library_id", "command_id", "source_logical_fact_id", "target_key"], ["id", "library_id", "command_id", "source_logical_fact_id", "target_key"], ondelete="RESTRICT")
    op.create_index("uq_fact_reconciliation_source_target_edges_root", "fact_reconciliation_source_target_edges", ["library_id", "command_id", "source_logical_fact_id", "target_key"], unique=True, postgresql_where=sa.text("supersedes_source_target_edge_id IS NULL"))
    op.create_index("uq_fact_reconciliation_source_target_edges_superseded_by", "fact_reconciliation_source_target_edges", ["library_id", "supersedes_source_target_edge_id"], unique=True, postgresql_where=sa.text("supersedes_source_target_edge_id IS NOT NULL"))

    op.create_table(
        "fact_reconciliation_assertion_assignments",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("evolution_decision_id", uuid, nullable=False),
        sa.Column("source_logical_fact_id", uuid, nullable=False),
        sa.Column("source_transition_id", uuid, nullable=False),
        sa.Column("fact_assertion_id", uuid, nullable=False),
        sa.Column("source_group_fingerprint", sa.String(64), nullable=False),
        sa.Column("partition_basis_snapshot", jsonb, nullable=False),
        sa.Column("assignment_state", sa.String(16), nullable=False),
        sa.Column("target_key", sa.String(256), nullable=True),
        sa.Column("source_target_edge_id", uuid, nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("supersedes_assignment_id", uuid, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("assignment_state IN ('resolved','pending','superseded')", name="ck_fact_reconciliation_assertion_assignments_state"),
        sa.CheckConstraint("source_group_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_fact_reconciliation_assertion_assignments_group_fingerprint"),
        sa.CheckConstraint("jsonb_typeof(partition_basis_snapshot) = 'object' AND octet_length(partition_basis_snapshot::text) <= 65536", name="ck_fact_reconciliation_assertion_assignments_basis"),
        sa.CheckConstraint("supersedes_assignment_id IS NULL OR supersedes_assignment_id <> id", name="ck_fact_recon_assignments_not_self_predecessor"),
        sa.CheckConstraint("(assignment_state = 'pending' AND target_key IS NULL AND source_target_edge_id IS NULL) OR (assignment_state = 'resolved' AND target_key IS NOT NULL AND source_target_edge_id IS NOT NULL) OR assignment_state = 'superseded'", name="ck_fact_reconciliation_assertion_assignments_shape"),
        sa.ForeignKeyConstraint(["evolution_decision_id", "library_id", "command_id"], ["fact_reconciliation_decisions.id", "fact_reconciliation_decisions.library_id", "fact_reconciliation_decisions.command_id"], ondelete="RESTRICT", name="fk_fact_reconciliation_assertion_assignments_decision"),
        sa.ForeignKeyConstraint(["source_transition_id", "library_id", "command_id", "evolution_decision_id", "source_logical_fact_id"], ["fact_reconciliation_sources.id", "fact_reconciliation_sources.library_id", "fact_reconciliation_sources.command_id", "fact_reconciliation_sources.evolution_decision_id", "fact_reconciliation_sources.source_logical_fact_id"], ondelete="RESTRICT", name="fk_fact_reconciliation_assertion_assignments_source"),
        sa.ForeignKeyConstraint(["fact_assertion_id", "library_id"], ["fact_assertions.id", "fact_assertions.library_id"], ondelete="RESTRICT", name="fk_fact_reconciliation_assertion_assignments_assertion"),
        sa.ForeignKeyConstraint(["source_target_edge_id", "library_id", "command_id", "evolution_decision_id", "source_logical_fact_id", "target_key"], ["fact_reconciliation_source_target_edges.id", "fact_reconciliation_source_target_edges.library_id", "fact_reconciliation_source_target_edges.command_id", "fact_reconciliation_source_target_edges.evolution_decision_id", "fact_reconciliation_source_target_edges.source_logical_fact_id", "fact_reconciliation_source_target_edges.target_key"], ondelete="RESTRICT", name="fk_fact_reconciliation_assertion_assignments_edge"),
        sa.UniqueConstraint("id", "library_id", "command_id", "fact_assertion_id", name="uq_fact_reconciliation_assertion_assignments_owner_assertion"),
        sa.UniqueConstraint("library_id", "evolution_decision_id", "fact_assertion_id", name="uq_fact_reconciliation_assertion_assignments_decision_assertion"),
    )
    op.create_foreign_key("fk_fact_reconciliation_assertion_assignments_supersedes", "fact_reconciliation_assertion_assignments", "fact_reconciliation_assertion_assignments", ["supersedes_assignment_id", "library_id", "command_id", "fact_assertion_id"], ["id", "library_id", "command_id", "fact_assertion_id"], ondelete="RESTRICT")
    op.create_index("uq_fact_reconciliation_assertion_assignments_root", "fact_reconciliation_assertion_assignments", ["library_id", "command_id", "fact_assertion_id"], unique=True, postgresql_where=sa.text("supersedes_assignment_id IS NULL"))
    op.create_index("uq_fact_reconciliation_assertion_assignments_superseded_by", "fact_reconciliation_assertion_assignments", ["library_id", "supersedes_assignment_id"], unique=True, postgresql_where=sa.text("supersedes_assignment_id IS NOT NULL"))

    op.add_column("logical_facts", sa.Column("reconciliation_target_slot_id", uuid, nullable=True))
    op.create_foreign_key(
        "fk_logical_facts_reconciliation_target_slot",
        "logical_facts",
        "fact_reconciliation_target_slots",
        ["reconciliation_target_slot_id", "library_id"],
        ["id", "library_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )


def _install_triggers() -> None:
    op.execute("""
CREATE FUNCTION fn_fact_reconciliation_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' THEN
    RAISE EXCEPTION 'fact_reconciliation_append_only';
  END IF;
  IF TG_TABLE_NAME='fact_reconciliation_decisions' THEN
    IF (to_jsonb(NEW)-'lifecycle_status') IS DISTINCT FROM (to_jsonb(OLD)-'lifecycle_status')
       OR OLD.lifecycle_status='superseded' OR NEW.lifecycle_status<>'superseded'
    THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  ELSIF TG_TABLE_NAME='fact_reconciliation_sources' THEN
    IF (to_jsonb(NEW)-'resolution_state') IS DISTINCT FROM (to_jsonb(OLD)-'resolution_state')
       OR OLD.resolution_state='superseded' OR NEW.resolution_state<>'superseded'
    THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  ELSIF TG_TABLE_NAME='fact_reconciliation_target_slots' THEN
    IF (to_jsonb(NEW)-'slot_state') IS DISTINCT FROM (to_jsonb(OLD)-'slot_state')
       OR OLD.slot_state='superseded' OR NEW.slot_state<>'superseded'
    THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  ELSIF TG_TABLE_NAME='fact_reconciliation_source_target_edges' THEN
    IF (to_jsonb(NEW)-'edge_state') IS DISTINCT FROM (to_jsonb(OLD)-'edge_state')
       OR OLD.edge_state='superseded' OR NEW.edge_state<>'superseded'
    THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  ELSIF TG_TABLE_NAME='fact_reconciliation_assertion_assignments' THEN
    IF (to_jsonb(NEW)-'assignment_state') IS DISTINCT FROM (to_jsonb(OLD)-'assignment_state')
       OR OLD.assignment_state='superseded' OR NEW.assignment_state<>'superseded'
    THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  END IF;
  RETURN NEW;
END $$;
""")
    for table in _AUDIT_TABLES:
        op.execute(f"CREATE TRIGGER trg_fact_reconciliation_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION fn_fact_reconciliation_append_only()")

    op.execute("""
CREATE FUNCTION fn_fact_reconciliation_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE decision_state text;
BEGIN
  SELECT lifecycle_status INTO decision_state
  FROM fact_reconciliation_decisions
  WHERE id=NEW.evolution_decision_id AND library_id=NEW.library_id AND command_id=NEW.command_id;
  IF decision_state NOT IN ('pending','applied') THEN
    RAISE EXCEPTION 'fact_reconciliation_child_decision_invalid';
  END IF;
  IF TG_TABLE_NAME='fact_reconciliation_sources' AND NEW.resolution_state='historical_only' THEN
    RAISE EXCEPTION 'fact_reconciliation_historical_only_write_invalid';
  END IF;
  RETURN NULL;
END $$;
""")
    for table in _AUDIT_TABLES[2:]:
        op.execute(f"CREATE CONSTRAINT TRIGGER ct_fact_reconciliation_child_guard AFTER INSERT ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_fact_reconciliation_child_guard()")

    op.execute("""
CREATE FUNCTION fn_fact_reconciliation_command_has_decision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_decisions
    WHERE library_id=NEW.library_id AND command_id=NEW.id
  ) THEN RAISE EXCEPTION 'fact_reconciliation_command_without_decision'; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ct_fact_reconciliation_command_has_decision
AFTER INSERT ON fact_reconciliation_commands DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION fn_fact_reconciliation_command_has_decision();
""")

    op.execute("""
CREATE FUNCTION fn_fact_reconciliation_decision_graph() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE predecessor_outcome text;
BEGIN
  IF NEW.supersedes_decision_id IS NULL THEN
    IF NEW.evaluated_outcome='cancelled' THEN
      RAISE EXCEPTION 'fact_reconciliation_lifecycle_invalid';
    END IF;
  ELSE
    SELECT evaluated_outcome INTO predecessor_outcome
    FROM fact_reconciliation_decisions
    WHERE id=NEW.supersedes_decision_id AND library_id=NEW.library_id AND command_id=NEW.command_id;
    IF predecessor_outcome='pending' AND NEW.evaluated_outcome NOT IN ('pending','applied','cancelled') THEN
      RAISE EXCEPTION 'fact_reconciliation_lifecycle_invalid';
    ELSIF predecessor_outcome IN ('rejected','stale') AND NEW.evaluated_outcome='cancelled' THEN
      RAISE EXCEPTION 'fact_reconciliation_lifecycle_invalid';
    ELSIF predecessor_outcome IN ('applied','cancelled') THEN
      RAISE EXCEPTION 'fact_reconciliation_lifecycle_invalid';
    END IF;
  END IF;
  IF (NEW.requested_effect='cancel') <> (NEW.evaluated_outcome='cancelled')
     OR (NEW.evaluated_outcome='pending' AND NEW.requested_effect<>'stage')
     OR (NEW.evaluated_outcome='applied' AND NEW.requested_effect<>'apply')
  THEN RAISE EXCEPTION 'fact_reconciliation_decision_shape_invalid'; END IF;
  IF NEW.evaluated_outcome='cancelled' AND (
       NEW.operation_payload_snapshot <> jsonb_build_object(
         'control_kind','cancel_pending',
         'expected_pending_decision_id',NEW.supersedes_decision_id::text
       )
     ) THEN RAISE EXCEPTION 'fact_reconciliation_decision_shape_invalid'; END IF;
  IF NEW.evaluated_outcome IN ('rejected','stale','cancelled') AND EXISTS (
    SELECT 1 FROM fact_reconciliation_sources WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id
    UNION ALL SELECT 1 FROM fact_reconciliation_target_slots WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id
    UNION ALL SELECT 1 FROM fact_reconciliation_source_target_edges WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id
    UNION ALL SELECT 1 FROM fact_reconciliation_assertion_assignments WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id
  ) THEN RAISE EXCEPTION 'fact_reconciliation_terminal_has_children'; END IF;
  IF NEW.evaluated_outcome IN ('pending','applied') THEN
    IF NOT EXISTS (SELECT 1 FROM fact_reconciliation_sources WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id)
       OR NOT EXISTS (SELECT 1 FROM fact_reconciliation_target_slots WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id)
       OR NOT EXISTS (SELECT 1 FROM fact_reconciliation_source_target_edges WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id)
       OR NOT EXISTS (SELECT 1 FROM fact_reconciliation_assertion_assignments WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id)
    THEN RAISE EXCEPTION 'fact_reconciliation_incomplete_children'; END IF;
    IF NEW.evaluated_outcome='pending' AND (
      EXISTS (SELECT 1 FROM fact_reconciliation_sources WHERE evolution_decision_id=NEW.id AND resolution_state<>'pending')
      OR EXISTS (SELECT 1 FROM fact_reconciliation_target_slots WHERE evolution_decision_id=NEW.id AND slot_state<>'pending')
      OR EXISTS (SELECT 1 FROM fact_reconciliation_source_target_edges WHERE evolution_decision_id=NEW.id AND edge_state<>'pending')
      OR NOT EXISTS (SELECT 1 FROM fact_reconciliation_assertion_assignments WHERE evolution_decision_id=NEW.id AND assignment_state='pending')
      OR EXISTS (SELECT 1 FROM fact_reconciliation_target_slots WHERE evolution_decision_id=NEW.id AND target_ref_kind='new' AND target_logical_fact_id IS NOT NULL)
    ) THEN RAISE EXCEPTION 'fact_reconciliation_pending_graph_invalid'; END IF;
    IF NEW.evaluated_outcome='applied' AND (
      EXISTS (SELECT 1 FROM fact_reconciliation_sources WHERE evolution_decision_id=NEW.id AND resolution_state<>'applied')
      OR EXISTS (SELECT 1 FROM fact_reconciliation_target_slots WHERE evolution_decision_id=NEW.id AND slot_state<>'applied')
      OR EXISTS (SELECT 1 FROM fact_reconciliation_source_target_edges WHERE evolution_decision_id=NEW.id AND edge_state<>'applied')
      OR EXISTS (SELECT 1 FROM fact_reconciliation_assertion_assignments WHERE evolution_decision_id=NEW.id AND assignment_state<>'resolved')
      OR EXISTS (
        SELECT 1 FROM fact_reconciliation_target_slots
        WHERE evolution_decision_id=NEW.id AND target_ref_kind='new'
          AND (target_logical_fact_id IS NULL OR target_logical_fact_id<>planned_target_logical_fact_id)
      )
    ) THEN RAISE EXCEPTION 'fact_reconciliation_applied_graph_invalid'; END IF;
  END IF;
  IF EXISTS (
    SELECT 1 FROM fact_reconciliation_assertion_assignments a
    WHERE a.evolution_decision_id=NEW.id AND a.library_id=NEW.library_id
    GROUP BY a.source_logical_fact_id, a.source_group_fingerprint
    HAVING count(DISTINCT a.target_key) FILTER (WHERE a.assignment_state='resolved') > 1
  ) THEN RAISE EXCEPTION 'fact_reconciliation_partition_non_unique'; END IF;
  IF EXISTS (
    SELECT 1 FROM fact_reconciliation_source_target_edges e
    JOIN fact_reconciliation_target_slots target ON target.id=e.target_slot_id AND target.library_id=e.library_id
    WHERE e.evolution_decision_id=NEW.id AND e.library_id=NEW.library_id
      AND e.edge_state='applied' AND e.source_logical_fact_id=target.target_logical_fact_id
  ) THEN RAISE EXCEPTION 'fact_reconciliation_source_equals_target'; END IF;
  IF NEW.evaluated_outcome='applied' AND EXISTS (
    WITH RECURSIVE walk(origin,node,path,cycle) AS (
      SELECT source.source_logical_fact_id, target.target_logical_fact_id,
             ARRAY[source.source_logical_fact_id,target.target_logical_fact_id],
             source.source_logical_fact_id=target.target_logical_fact_id
      FROM fact_reconciliation_sources source
      JOIN fact_reconciliation_source_target_edges edge ON edge.source_transition_id=source.id AND edge.library_id=source.library_id
      JOIN fact_reconciliation_target_slots target ON target.id=edge.target_slot_id AND target.library_id=edge.library_id
      WHERE source.library_id=NEW.library_id AND source.resolution_state='applied'
        AND edge.edge_state='applied' AND target.slot_state='applied'
      UNION ALL
      SELECT walk.origin,target.target_logical_fact_id,
             walk.path||target.target_logical_fact_id,
             target.target_logical_fact_id=ANY(walk.path)
      FROM walk
      JOIN fact_reconciliation_sources source ON source.library_id=NEW.library_id
        AND source.source_logical_fact_id=walk.node AND source.resolution_state='applied'
      JOIN fact_reconciliation_source_target_edges edge ON edge.source_transition_id=source.id AND edge.library_id=source.library_id AND edge.edge_state='applied'
      JOIN fact_reconciliation_target_slots target ON target.id=edge.target_slot_id AND target.library_id=edge.library_id AND target.slot_state='applied'
      WHERE NOT walk.cycle
    ) SELECT 1 FROM walk WHERE cycle
  ) THEN RAISE EXCEPTION 'fact_reconciliation_lineage_cycle'; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ct_fact_reconciliation_decision_graph
AFTER INSERT OR UPDATE ON fact_reconciliation_decisions DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION fn_fact_reconciliation_decision_graph();
""")

    op.execute("""
CREATE FUNCTION fn_fact_reconciliation_chain_local() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME='fact_reconciliation_decisions' AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_decisions next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_decision_id=OLD.id
  ) THEN RAISE EXCEPTION 'fact_reconciliation_broken_decision_chain'; END IF;
  IF TG_TABLE_NAME='fact_reconciliation_sources' AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_sources next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_source_transition_id=OLD.id
  ) AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_decisions next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_decision_id=OLD.evolution_decision_id AND next.lifecycle_status='cancelled'
  ) THEN RAISE EXCEPTION 'fact_reconciliation_broken_source_chain'; END IF;
  IF TG_TABLE_NAME='fact_reconciliation_target_slots' AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_target_slots next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_target_slot_id=OLD.id
  ) AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_decisions next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_decision_id=OLD.evolution_decision_id AND next.lifecycle_status='cancelled'
  ) THEN RAISE EXCEPTION 'fact_reconciliation_broken_target_slot_chain'; END IF;
  IF TG_TABLE_NAME='fact_reconciliation_source_target_edges' AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_source_target_edges next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_source_target_edge_id=OLD.id
  ) AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_decisions next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_decision_id=OLD.evolution_decision_id AND next.lifecycle_status='cancelled'
  ) THEN RAISE EXCEPTION 'fact_reconciliation_broken_edge_chain'; END IF;
  IF TG_TABLE_NAME='fact_reconciliation_assertion_assignments' AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_assertion_assignments next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_assignment_id=OLD.id
  ) AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_decisions next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_decision_id=OLD.evolution_decision_id AND next.lifecycle_status='cancelled'
  ) THEN RAISE EXCEPTION 'fact_reconciliation_broken_assignment_chain'; END IF;
  RETURN NULL;
END $$;
""")
    for table in _AUDIT_TABLES[1:]:
        op.execute(f"CREATE CONSTRAINT TRIGGER ct_fact_reconciliation_chain_local AFTER UPDATE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_fact_reconciliation_chain_local()")

    op.execute("""
CREATE FUNCTION fn_fact_reconciliation_logical_fact_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' THEN
    IF OLD.reconciliation_target_slot_id IS NOT NULL THEN
      RAISE EXCEPTION 'fact_reconciliation_target_fact_immutable';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.reconciliation_target_slot_id IS NULL AND NEW.reconciliation_target_slot_id IS NOT NULL THEN
    RAISE EXCEPTION 'fact_reconciliation_old_fact_claim';
  END IF;
  IF OLD.reconciliation_target_slot_id IS NOT NULL AND (
    (to_jsonb(NEW)-'status'-'updated_at') IS DISTINCT FROM (to_jsonb(OLD)-'status'-'updated_at')
    OR NEW.reconciliation_target_slot_id IS DISTINCT FROM OLD.reconciliation_target_slot_id
  ) THEN RAISE EXCEPTION 'fact_reconciliation_target_fact_immutable'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_fact_reconciliation_logical_fact_guard
BEFORE UPDATE OR DELETE ON logical_facts
FOR EACH ROW EXECUTE FUNCTION fn_fact_reconciliation_logical_fact_guard();
""")

    op.execute("""
CREATE FUNCTION fn_fact_reconciliation_logical_fact_pair() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE slot_row fact_reconciliation_target_slots%ROWTYPE;
DECLARE fact_row logical_facts%ROWTYPE;
BEGIN
  IF TG_TABLE_NAME='logical_facts' THEN
    IF TG_OP='DELETE' THEN RETURN NULL; END IF;
    IF NEW.reconciliation_target_slot_id IS NOT NULL THEN
      SELECT * INTO slot_row FROM fact_reconciliation_target_slots
      WHERE id=NEW.reconciliation_target_slot_id AND library_id=NEW.library_id;
      IF NOT FOUND OR slot_row.target_ref_kind<>'new'
         OR slot_row.planned_target_logical_fact_id<>NEW.id
         OR slot_row.target_logical_fact_id<>NEW.id
         OR slot_row.target_identity_fingerprint<>NEW.identity_fingerprint
         OR (slot_row.target_spec_snapshot->>'stable_predicate_identity_id') IS DISTINCT FROM NEW.stable_predicate_identity_id::text
         OR (slot_row.target_spec_snapshot->>'subject_canonical_entity_id') IS DISTINCT FROM NEW.subject_canonical_entity_id::text
         OR (slot_row.target_spec_snapshot->>'object_kind') IS DISTINCT FROM NEW.object_kind
         OR (slot_row.target_spec_snapshot->>'object_canonical_entity_id') IS DISTINCT FROM NEW.object_canonical_entity_id::text
         OR (slot_row.target_spec_snapshot->'object_value') IS DISTINCT FROM COALESCE(NEW.object_value, 'null'::jsonb)
         OR (slot_row.target_spec_snapshot->'identity_qualifiers') IS DISTINCT FROM NEW.identity_qualifiers
         OR (slot_row.target_spec_snapshot->>'temporal_identity_key') IS DISTINCT FROM NEW.temporal_identity_key
         OR (slot_row.target_spec_snapshot->>'identity_policy_version') IS DISTINCT FROM NEW.identity_policy_version
      THEN RAISE EXCEPTION 'fact_reconciliation_target_pair_mismatch'; END IF;
    END IF;
  ELSE
    IF TG_OP='DELETE' THEN RETURN NULL; END IF;
    IF NEW.target_ref_kind='new' AND NEW.target_logical_fact_id IS NOT NULL THEN
      SELECT * INTO fact_row FROM logical_facts
      WHERE id=NEW.target_logical_fact_id AND library_id=NEW.library_id;
      IF NOT FOUND OR fact_row.reconciliation_target_slot_id<>NEW.id THEN
        RAISE EXCEPTION 'fact_reconciliation_target_pair_mismatch';
      END IF;
    END IF;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ct_fact_reconciliation_logical_fact_pair
AFTER INSERT OR UPDATE OR DELETE ON logical_facts DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION fn_fact_reconciliation_logical_fact_pair();
CREATE CONSTRAINT TRIGGER ct_fact_reconciliation_target_slot_pair
AFTER INSERT OR UPDATE OR DELETE ON fact_reconciliation_target_slots DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION fn_fact_reconciliation_logical_fact_pair();
""")


def _assert_catalog() -> None:
    op.execute("""
DO $$
BEGIN
  IF to_regclass('fact_reconciliation_commands') IS NULL
     OR to_regclass('fact_reconciliation_decisions') IS NULL
     OR to_regclass('fact_reconciliation_sources') IS NULL
     OR to_regclass('fact_reconciliation_target_slots') IS NULL
     OR to_regclass('fact_reconciliation_source_target_edges') IS NULL
     OR to_regclass('fact_reconciliation_assertion_assignments') IS NULL
     OR NOT EXISTS (
       SELECT 1 FROM information_schema.columns
       WHERE table_schema=current_schema() AND table_name='logical_facts'
         AND column_name='reconciliation_target_slot_id' AND is_nullable='YES'
     )
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='trg_fact_reconciliation_append_only')<>6
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_fact_reconciliation_child_guard')<>4
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_fact_reconciliation_chain_local')<>5
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_fact_reconciliation_logical_fact_pair')<>1
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_fact_reconciliation_target_slot_pair')<>1
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='trg_fact_reconciliation_logical_fact_guard')<>1
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_fact_reconciliation_command_has_decision')<>1
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_fact_reconciliation_decision_graph')<>1
  THEN RAISE EXCEPTION 'P3_3_0074_CATALOG_MISMATCH'; END IF;
END $$;
""")


def upgrade() -> None:
    _set_timeouts()
    op.execute(_UPGRADE_LOCK_SQL)
    _assert_preconditions()
    _create_tables()
    _install_triggers()
    _assert_catalog()


def downgrade() -> None:
    _set_timeouts()
    op.execute(_DOWNGRADE_LOCK_SQL)
    op.execute("""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM fact_reconciliation_commands)
     OR EXISTS (SELECT 1 FROM fact_reconciliation_decisions)
     OR EXISTS (SELECT 1 FROM fact_reconciliation_sources)
     OR EXISTS (SELECT 1 FROM fact_reconciliation_target_slots)
     OR EXISTS (SELECT 1 FROM fact_reconciliation_source_target_edges)
     OR EXISTS (SELECT 1 FROM fact_reconciliation_assertion_assignments)
     OR EXISTS (SELECT 1 FROM logical_facts WHERE reconciliation_target_slot_id IS NOT NULL)
  THEN RAISE EXCEPTION 'P3_3_0074_NONEMPTY_AUDIT'; END IF;
END $$;
""")
    op.execute("DROP TRIGGER ct_fact_reconciliation_target_slot_pair ON fact_reconciliation_target_slots")
    op.execute("DROP TRIGGER ct_fact_reconciliation_logical_fact_pair ON logical_facts")
    op.execute("DROP FUNCTION fn_fact_reconciliation_logical_fact_pair()")
    op.execute("DROP TRIGGER trg_fact_reconciliation_logical_fact_guard ON logical_facts")
    op.execute("DROP FUNCTION fn_fact_reconciliation_logical_fact_guard()")
    for table in _AUDIT_TABLES[1:]:
        op.execute(f"DROP TRIGGER ct_fact_reconciliation_chain_local ON {table}")
    op.execute("DROP FUNCTION fn_fact_reconciliation_chain_local()")
    op.execute("DROP TRIGGER ct_fact_reconciliation_decision_graph ON fact_reconciliation_decisions")
    op.execute("DROP FUNCTION fn_fact_reconciliation_decision_graph()")
    op.execute("DROP TRIGGER ct_fact_reconciliation_command_has_decision ON fact_reconciliation_commands")
    op.execute("DROP FUNCTION fn_fact_reconciliation_command_has_decision()")
    for table in _AUDIT_TABLES[2:]:
        op.execute(f"DROP TRIGGER ct_fact_reconciliation_child_guard ON {table}")
    op.execute("DROP FUNCTION fn_fact_reconciliation_child_guard()")
    for table in _AUDIT_TABLES:
        op.execute(f"DROP TRIGGER trg_fact_reconciliation_append_only ON {table}")
    op.execute("DROP FUNCTION fn_fact_reconciliation_append_only()")
    op.drop_constraint("fk_logical_facts_reconciliation_target_slot", "logical_facts", type_="foreignkey")
    op.drop_column("logical_facts", "reconciliation_target_slot_id")
    for table in reversed(_AUDIT_TABLES):
        op.drop_table(table)
