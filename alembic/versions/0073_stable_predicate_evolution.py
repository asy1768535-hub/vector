"""Add append-only StablePredicate identity evolution lineage.

Revision 0073 is maintenance-only.  Its lock blocks are emitted unchanged in
online and offline mode so a failed admission cannot expose a partial schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIT_TABLES = (
    "stable_predicate_evolution_commands",
    "stable_predicate_evolution_decisions",
    "stable_predicate_evolution_sources",
    "stable_predicate_evolution_successors",
    "stable_predicate_mapping_evolution_assignments",
)

_UPGRADE_LOCK_SQL = """
DO $p3_2_migration_lock$
BEGIN
    IF NOT pg_try_advisory_xact_lock(356131050375775214) THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'migration_busy';
    END IF;
    BEGIN
        LOCK TABLE sys_libraries IN SHARE MODE NOWAIT;
        LOCK TABLE relation_types IN SHARE MODE NOWAIT;
        LOCK TABLE stable_predicate_identities IN SHARE ROW EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mappings IN ACCESS EXCLUSIVE MODE NOWAIT;
    EXCEPTION WHEN lock_not_available THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'migration_busy';
    END;
END
$p3_2_migration_lock$;
"""

_DOWNGRADE_LOCK_SQL = """
DO $p3_2_downgrade_lock$
BEGIN
    IF NOT pg_try_advisory_xact_lock(356131050375775214) THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'migration_busy';
    END IF;
    BEGIN
        LOCK TABLE sys_libraries IN SHARE MODE NOWAIT;
        LOCK TABLE relation_types IN SHARE MODE NOWAIT;
        LOCK TABLE stable_predicate_identities IN SHARE ROW EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mappings IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_commands IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_successors IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mapping_evolution_assignments IN ACCESS EXCLUSIVE MODE NOWAIT;
    EXCEPTION WHEN lock_not_available THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'migration_busy';
    END;
END
$p3_2_downgrade_lock$;
"""


def _assert_preconditions() -> None:
    op.execute("""
CREATE FUNCTION fn_p3_2_0073_assert_preconditions() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF to_regclass('sys_libraries') IS NULL
     OR to_regclass('relation_types') IS NULL
     OR to_regclass('stable_predicate_identities') IS NULL
     OR to_regclass('stable_predicate_mappings') IS NULL
     OR to_regclass('stable_predicate_evolution_commands') IS NOT NULL
     OR EXISTS (
       SELECT 1 FROM information_schema.columns
       WHERE table_schema=current_schema() AND table_name='stable_predicate_mappings'
         AND column_name='evolution_assignment_id'
     )
     OR NOT EXISTS (
       SELECT 1 FROM pg_constraint
       WHERE conname='uq_stable_predicate_identities_id_library'
         AND conrelid='stable_predicate_identities'::regclass
     )
     OR NOT EXISTS (
       SELECT 1 FROM pg_constraint
       WHERE conname='uq_relation_types_id_library'
         AND conrelid='relation_types'::regclass
     )
     OR EXISTS (
       SELECT 1 FROM pg_proc
       WHERE proname IN (
         'fn_stable_predicate_evolution_append_only',
         'fn_stable_predicate_evolution_child_guard',
         'fn_stable_predicate_evolution_command_has_decision',
         'fn_stable_predicate_evolution_decision_graph',
         'fn_stable_predicate_evolution_chain_local',
         'fn_stable_predicate_mapping_evolution_pair',
         'fn_stable_predicate_identity_evolution_guard'
       )
     )
  THEN
    RAISE EXCEPTION 'P3_2_0073_PRECONDITION_FAILED';
  END IF;
END $$;
SELECT fn_p3_2_0073_assert_preconditions();
DROP FUNCTION fn_p3_2_0073_assert_preconditions();
""")


def _set_timeouts() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")


def _create_tables() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "stable_predicate_evolution_commands",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("command_identity_fingerprint", sa.String(64), nullable=False),
        sa.Column("operation_kind", sa.String(32), nullable=False),
        sa.Column("command_payload_snapshot", jsonb, nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("operation_kind IN ('merge','split','reassign')", name="ck_stable_predicate_evolution_commands_operation"),
        sa.CheckConstraint("command_identity_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_stable_predicate_evolution_commands_fingerprint"),
        sa.CheckConstraint("jsonb_typeof(command_payload_snapshot) = 'object' AND octet_length(command_payload_snapshot::text) <= 262144", name="ck_stable_predicate_evolution_commands_payload"),
        sa.CheckConstraint("contract_version = 'p3_2_stable_predicate_evolution/v1'", name="ck_stable_predicate_evolution_commands_contract"),
        sa.ForeignKeyConstraint(["library_id"], ["sys_libraries.id"], ondelete="RESTRICT", name="fk_stable_predicate_evolution_commands_library"),
        sa.UniqueConstraint("id", "library_id", name="uq_stable_predicate_evolution_commands_id_library"),
        sa.UniqueConstraint("library_id", "idempotency_key", name="uq_stable_predicate_evolution_commands_idempotency"),
        sa.UniqueConstraint("library_id", "command_identity_fingerprint", name="uq_stable_predicate_evolution_commands_identity"),
    )
    op.create_table(
        "stable_predicate_evolution_decisions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("decision_payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("operation_kind", sa.String(32), nullable=False),
        sa.Column("requested_effect", sa.String(32), nullable=False),
        sa.Column("evaluated_outcome", sa.String(32), nullable=False),
        sa.Column("lifecycle_status", sa.String(32), nullable=False),
        sa.Column("operation_payload_snapshot", jsonb, nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("reason_text", sa.String(512), nullable=False),
        sa.Column("method", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Numeric(7, 6), nullable=True),
        sa.Column("evidence_refs", jsonb, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("expected_precondition_fingerprint", sa.String(64), nullable=False),
        sa.Column("observed_precondition_fingerprint", sa.String(64), nullable=False),
        sa.Column("supersedes_decision_id", uuid, nullable=True),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", uuid, nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("operation_kind IN ('merge','split','reassign')", name="ck_stable_predicate_evolution_decisions_operation"),
        sa.CheckConstraint("requested_effect IN ('stage','apply','cancel')", name="ck_stable_predicate_evolution_decisions_requested_effect"),
        sa.CheckConstraint("evaluated_outcome IN ('pending','applied','rejected','stale','cancelled')", name="ck_stable_predicate_evolution_decisions_outcome"),
        sa.CheckConstraint("lifecycle_status IN ('pending','applied','rejected','stale','cancelled','superseded')", name="ck_stable_predicate_evolution_decisions_lifecycle"),
        sa.CheckConstraint("lifecycle_status = 'superseded' OR lifecycle_status = evaluated_outcome", name="ck_stable_predicate_evolution_decisions_status_outcome"),
        sa.CheckConstraint("(requested_effect='stage' AND evaluated_outcome='pending') OR (requested_effect='cancel' AND evaluated_outcome='cancelled') OR (requested_effect='apply' AND evaluated_outcome IN ('pending','applied','rejected','stale'))", name="ck_stable_predicate_evolution_decisions_effect_outcome"),
        sa.CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_stable_predicate_evolution_decisions_confidence"),
        sa.CheckConstraint("decision_payload_fingerprint ~ '^[0-9a-f]{64}$' AND expected_precondition_fingerprint ~ '^[0-9a-f]{64}$' AND observed_precondition_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_stable_predicate_evolution_decisions_fingerprints"),
        sa.CheckConstraint("jsonb_typeof(operation_payload_snapshot) = 'object' AND octet_length(operation_payload_snapshot::text) <= 1048576 AND jsonb_typeof(evidence_refs) = 'array' AND octet_length(evidence_refs::text) <= 262144", name="ck_stable_predicate_evolution_decisions_json"),
        sa.CheckConstraint("actor_type IN ('user','service')", name="ck_stable_predicate_evolution_decisions_actor"),
        sa.CheckConstraint("supersedes_decision_id IS NULL OR supersedes_decision_id <> id", name="ck_stable_predicate_evolution_decisions_not_self_predecessor"),
        sa.ForeignKeyConstraint(["command_id", "library_id"], ["stable_predicate_evolution_commands.id", "stable_predicate_evolution_commands.library_id"], ondelete="RESTRICT", name="fk_stable_predicate_evolution_decisions_command"),
        sa.UniqueConstraint("id", "library_id", "command_id", name="uq_stable_predicate_evolution_decisions_owner"),
        sa.UniqueConstraint("library_id", "command_id", "decision_payload_fingerprint", name="uq_stable_predicate_evolution_decisions_payload"),
    )
    op.create_foreign_key("fk_stable_predicate_evolution_decisions_supersedes", "stable_predicate_evolution_decisions", "stable_predicate_evolution_decisions", ["supersedes_decision_id", "library_id", "command_id"], ["id", "library_id", "command_id"], ondelete="RESTRICT")
    op.create_index("uq_stable_predicate_evolution_decisions_current", "stable_predicate_evolution_decisions", ["library_id", "command_id"], unique=True, postgresql_where=sa.text("lifecycle_status <> 'superseded'"))
    op.create_index("uq_stable_predicate_evolution_decisions_superseded_by", "stable_predicate_evolution_decisions", ["library_id", "supersedes_decision_id"], unique=True, postgresql_where=sa.text("supersedes_decision_id IS NOT NULL"))

    op.create_table(
        "stable_predicate_evolution_sources",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("evolution_decision_id", uuid, nullable=False),
        sa.Column("source_predicate_id", uuid, nullable=False),
        sa.Column("supersedes_source_transition_id", uuid, nullable=True),
        sa.Column("evolution_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("evolution_status IN ('pending','applied','historical_only','superseded')", name="ck_stable_predicate_evolution_sources_status"),
        sa.CheckConstraint("supersedes_source_transition_id IS NULL OR supersedes_source_transition_id <> id", name="ck_stable_predicate_evolution_sources_not_self_predecessor"),
        sa.ForeignKeyConstraint(["evolution_decision_id", "library_id", "command_id"], ["stable_predicate_evolution_decisions.id", "stable_predicate_evolution_decisions.library_id", "stable_predicate_evolution_decisions.command_id"], ondelete="RESTRICT", name="fk_stable_predicate_evolution_sources_decision"),
        sa.ForeignKeyConstraint(["source_predicate_id", "library_id"], ["stable_predicate_identities.id", "stable_predicate_identities.library_id"], ondelete="RESTRICT", name="fk_stable_predicate_evolution_sources_predicate"),
        sa.UniqueConstraint("id", "library_id", "command_id", "source_predicate_id", name="uq_stable_predicate_evolution_sources_owner_source"),
        sa.UniqueConstraint("id", "library_id", "command_id", "evolution_decision_id", "source_predicate_id", name="uq_stable_predicate_evolution_sources_owner_decision_source"),
        sa.UniqueConstraint("library_id", "evolution_decision_id", "source_predicate_id", name="uq_stable_predicate_evolution_sources_decision_source"),
    )
    op.create_foreign_key("fk_stable_predicate_evolution_sources_supersedes", "stable_predicate_evolution_sources", "stable_predicate_evolution_sources", ["supersedes_source_transition_id", "library_id", "command_id", "source_predicate_id"], ["id", "library_id", "command_id", "source_predicate_id"], ondelete="RESTRICT")
    op.create_index("uq_stable_predicate_evolution_sources_root", "stable_predicate_evolution_sources", ["library_id", "command_id", "source_predicate_id"], unique=True, postgresql_where=sa.text("supersedes_source_transition_id IS NULL"))
    op.create_index("uq_stable_predicate_evolution_sources_superseded_by", "stable_predicate_evolution_sources", ["library_id", "supersedes_source_transition_id"], unique=True, postgresql_where=sa.text("supersedes_source_transition_id IS NOT NULL"))
    op.create_index("uq_stable_predicate_evolution_sources_live", "stable_predicate_evolution_sources", ["library_id", "source_predicate_id"], unique=True, postgresql_where=sa.text("evolution_status IN ('pending','applied','historical_only')"))

    op.create_table(
        "stable_predicate_evolution_successors",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("evolution_decision_id", uuid, nullable=False),
        sa.Column("source_transition_id", uuid, nullable=False),
        sa.Column("source_predicate_id", uuid, nullable=False),
        sa.Column("target_ref_kind", sa.String(32), nullable=False),
        sa.Column("planned_target_predicate_id", uuid, nullable=False),
        sa.Column("target_predicate_id", uuid, nullable=True),
        sa.Column("target_spec_fingerprint", sa.String(64), nullable=True),
        sa.Column("target_spec_snapshot", jsonb, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("target_ref_kind IN ('existing','new')", name="ck_stable_predicate_evolution_successors_kind"),
        sa.CheckConstraint("(target_ref_kind = 'existing' AND target_predicate_id = planned_target_predicate_id AND target_spec_fingerprint IS NULL AND target_spec_snapshot IS NULL) OR (target_ref_kind = 'new' AND (target_predicate_id IS NULL OR target_predicate_id = planned_target_predicate_id) AND target_spec_fingerprint ~ '^[0-9a-f]{64}$' AND target_spec_snapshot IS NOT NULL)", name="ck_stable_predicate_evolution_successors_shape"),
        sa.CheckConstraint("target_spec_snapshot IS NULL OR (jsonb_typeof(target_spec_snapshot) = 'object' AND octet_length(target_spec_snapshot::text) <= 65536)", name="ck_stable_predicate_evolution_successors_spec"),
        sa.ForeignKeyConstraint(["evolution_decision_id", "library_id", "command_id"], ["stable_predicate_evolution_decisions.id", "stable_predicate_evolution_decisions.library_id", "stable_predicate_evolution_decisions.command_id"], ondelete="RESTRICT", name="fk_stable_predicate_evolution_successors_decision"),
        sa.ForeignKeyConstraint(["source_transition_id", "library_id", "command_id", "evolution_decision_id", "source_predicate_id"], ["stable_predicate_evolution_sources.id", "stable_predicate_evolution_sources.library_id", "stable_predicate_evolution_sources.command_id", "stable_predicate_evolution_sources.evolution_decision_id", "stable_predicate_evolution_sources.source_predicate_id"], ondelete="RESTRICT", name="fk_stable_predicate_evolution_successors_source"),
        sa.ForeignKeyConstraint(["target_predicate_id", "library_id"], ["stable_predicate_identities.id", "stable_predicate_identities.library_id"], ondelete="RESTRICT", name="fk_stable_predicate_evolution_successors_target"),
        sa.UniqueConstraint("id", "library_id", "command_id", "evolution_decision_id", name="uq_stable_predicate_evolution_successors_owner_decision"),
        sa.UniqueConstraint("id", "library_id", "command_id", "evolution_decision_id", "source_transition_id", "source_predicate_id", name="uq_stable_predicate_evolution_successors_owner_source"),
        sa.UniqueConstraint("library_id", "source_transition_id", "planned_target_predicate_id", name="uq_stable_predicate_evolution_successors_target"),
    )
    op.create_index("ix_stable_predicate_evolution_successors_decision", "stable_predicate_evolution_successors", ["library_id", "command_id", "evolution_decision_id"])

    op.create_table(
        "stable_predicate_mapping_evolution_assignments",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("library_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("evolution_decision_id", uuid, nullable=False),
        sa.Column("source_transition_id", uuid, nullable=True),
        sa.Column("old_mapping_id", uuid, nullable=False),
        sa.Column("relation_type_id", uuid, nullable=False),
        sa.Column("source_predicate_id", uuid, nullable=False),
        sa.Column("target_successor_id", uuid, nullable=True),
        sa.Column("target_predicate_id", uuid, nullable=True),
        sa.Column("new_mapping_id", uuid, nullable=True),
        sa.Column("assignment_state", sa.String(32), nullable=False),
        sa.Column("partition_basis_snapshot", jsonb, nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("supersedes_assignment_id", uuid, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("assignment_state IN ('pending','resolved','superseded')", name="ck_stable_predicate_mapping_evolution_assignments_state"),
        sa.CheckConstraint("jsonb_typeof(partition_basis_snapshot) = 'object' AND octet_length(partition_basis_snapshot::text) <= 65536", name="ck_stable_predicate_mapping_evolution_assignments_basis"),
        sa.CheckConstraint("supersedes_assignment_id IS NULL OR supersedes_assignment_id <> id", name="ck_sp_mapping_evolution_assignments_not_self_predecessor"),
        sa.CheckConstraint("assignment_state <> 'pending' OR (target_successor_id IS NULL AND target_predicate_id IS NULL AND new_mapping_id IS NULL)", name="ck_stable_predicate_mapping_evolution_assignments_pending_shape"),
        sa.CheckConstraint("new_mapping_id IS NULL OR new_mapping_id <> old_mapping_id", name="ck_sp_mapping_evolution_assignments_not_self_mapping"),
        sa.ForeignKeyConstraint(["evolution_decision_id", "library_id", "command_id"], ["stable_predicate_evolution_decisions.id", "stable_predicate_evolution_decisions.library_id", "stable_predicate_evolution_decisions.command_id"], ondelete="RESTRICT", name="fk_stable_predicate_mapping_evolution_assignments_decision"),
        sa.ForeignKeyConstraint(["source_transition_id", "library_id", "command_id", "evolution_decision_id", "source_predicate_id"], ["stable_predicate_evolution_sources.id", "stable_predicate_evolution_sources.library_id", "stable_predicate_evolution_sources.command_id", "stable_predicate_evolution_sources.evolution_decision_id", "stable_predicate_evolution_sources.source_predicate_id"], ondelete="RESTRICT", name="fk_stable_predicate_mapping_evolution_assignments_source"),
        sa.ForeignKeyConstraint(["relation_type_id", "library_id"], ["relation_types.id", "relation_types.library_id"], ondelete="RESTRICT", name="fk_stable_predicate_mapping_evolution_assignments_relation_type"),
        sa.ForeignKeyConstraint(["source_predicate_id", "library_id"], ["stable_predicate_identities.id", "stable_predicate_identities.library_id"], ondelete="RESTRICT", name="fk_sp_mapping_evolution_assignments_source_predicate"),
        sa.ForeignKeyConstraint(["target_successor_id", "library_id", "command_id", "evolution_decision_id", "source_transition_id", "source_predicate_id"], ["stable_predicate_evolution_successors.id", "stable_predicate_evolution_successors.library_id", "stable_predicate_evolution_successors.command_id", "stable_predicate_evolution_successors.evolution_decision_id", "stable_predicate_evolution_successors.source_transition_id", "stable_predicate_evolution_successors.source_predicate_id"], ondelete="RESTRICT", name="fk_sp_mapping_evolution_assignments_target_successor"),
        sa.ForeignKeyConstraint(["target_predicate_id", "library_id"], ["stable_predicate_identities.id", "stable_predicate_identities.library_id"], ondelete="RESTRICT", name="fk_sp_mapping_evolution_assignments_target_predicate"),
        sa.UniqueConstraint("id", "library_id", name="uq_stable_predicate_mapping_evolution_assignments_id_library"),
        sa.UniqueConstraint("id", "library_id", "command_id", "old_mapping_id", name="uq_stable_predicate_mapping_evolution_assignments_owner_mapping"),
        sa.UniqueConstraint("library_id", "evolution_decision_id", "old_mapping_id", name="uq_sp_mapping_evolution_assignments_decision_mapping"),
    )
    op.create_foreign_key("fk_stable_predicate_mapping_evolution_assignments_supersedes", "stable_predicate_mapping_evolution_assignments", "stable_predicate_mapping_evolution_assignments", ["supersedes_assignment_id", "library_id", "command_id", "old_mapping_id"], ["id", "library_id", "command_id", "old_mapping_id"], ondelete="RESTRICT")
    op.create_index("uq_stable_predicate_mapping_evolution_assignments_root", "stable_predicate_mapping_evolution_assignments", ["library_id", "command_id", "old_mapping_id"], unique=True, postgresql_where=sa.text("supersedes_assignment_id IS NULL"))
    op.create_index("uq_stable_predicate_mapping_evolution_assignments_superseded_by", "stable_predicate_mapping_evolution_assignments", ["library_id", "supersedes_assignment_id"], unique=True, postgresql_where=sa.text("supersedes_assignment_id IS NOT NULL"))
    op.create_index("uq_stable_predicate_mapping_evolution_assignments_live", "stable_predicate_mapping_evolution_assignments", ["library_id", "old_mapping_id"], unique=True, postgresql_where=sa.text("assignment_state IN ('pending','resolved')"))
    op.create_index("ix_sp_mapping_evolution_assignments_decision", "stable_predicate_mapping_evolution_assignments", ["library_id", "command_id", "evolution_decision_id"])


def _link_mappings() -> None:
    op.create_unique_constraint("uq_stable_predicate_mappings_id_library", "stable_predicate_mappings", ["id", "library_id"])
    op.add_column("stable_predicate_mappings", sa.Column("evolution_assignment_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_stable_predicate_mapping_evolution_assignments_old_mapping", "stable_predicate_mapping_evolution_assignments", "stable_predicate_mappings", ["old_mapping_id", "library_id"], ["id", "library_id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_stable_predicate_mapping_evolution_assignments_new_mapping", "stable_predicate_mapping_evolution_assignments", "stable_predicate_mappings", ["new_mapping_id", "library_id"], ["id", "library_id"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED")
    op.create_foreign_key("fk_stable_predicate_mappings_evolution_assignment", "stable_predicate_mappings", "stable_predicate_mapping_evolution_assignments", ["evolution_assignment_id", "library_id"], ["id", "library_id"], ondelete="RESTRICT", deferrable=True, initially="DEFERRED")


def _install_triggers() -> None:
    op.execute("""
CREATE FUNCTION fn_stable_predicate_evolution_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'predicate_evolution_append_only'; END IF;
  IF TG_TABLE_NAME = 'stable_predicate_evolution_decisions'
     AND to_jsonb(OLD)->>'lifecycle_status' IN ('pending','rejected','stale')
     AND to_jsonb(NEW)->>'lifecycle_status' = 'superseded'
     AND to_jsonb(NEW) - 'lifecycle_status' = to_jsonb(OLD) - 'lifecycle_status'
     AND NOT EXISTS (
       SELECT 1 FROM stable_predicate_evolution_decisions child
       WHERE child.library_id=OLD.library_id AND child.command_id=OLD.command_id
         AND child.supersedes_decision_id=OLD.id
     ) THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME = 'stable_predicate_evolution_sources'
     AND to_jsonb(OLD)->>'evolution_status' = 'pending'
     AND to_jsonb(NEW)->>'evolution_status' = 'superseded'
     AND to_jsonb(NEW) - 'evolution_status' = to_jsonb(OLD) - 'evolution_status'
     AND NOT EXISTS (
       SELECT 1 FROM stable_predicate_evolution_sources child
       WHERE child.library_id=OLD.library_id AND child.command_id=OLD.command_id
         AND child.source_predicate_id=(to_jsonb(OLD)->>'source_predicate_id')::uuid
         AND child.supersedes_source_transition_id=OLD.id
     ) THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME = 'stable_predicate_mapping_evolution_assignments'
     AND to_jsonb(OLD)->>'assignment_state' IN ('pending','resolved')
     AND to_jsonb(NEW)->>'assignment_state' = 'superseded'
     AND to_jsonb(NEW) - 'assignment_state' = to_jsonb(OLD) - 'assignment_state'
     AND NOT EXISTS (
       SELECT 1 FROM stable_predicate_mapping_evolution_assignments child
       WHERE child.library_id=OLD.library_id AND child.command_id=OLD.command_id
         AND child.old_mapping_id=(to_jsonb(OLD)->>'old_mapping_id')::uuid
         AND child.supersedes_assignment_id=OLD.id
     ) THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'predicate_evolution_append_only';
END $$;
""")
    for table in _AUDIT_TABLES:
        op.execute(f"CREATE TRIGGER trg_stable_predicate_evolution_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_evolution_append_only()")
    op.execute("""
CREATE FUNCTION fn_stable_predicate_evolution_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  decision_row stable_predicate_evolution_decisions%ROWTYPE;
  command_row stable_predicate_evolution_commands%ROWTYPE;
  source_row stable_predicate_evolution_sources%ROWTYPE;
  successor_row stable_predicate_evolution_successors%ROWTYPE;
  mapping_row stable_predicate_mappings%ROWTYPE;
  member jsonb;
BEGIN
  SELECT * INTO decision_row FROM stable_predicate_evolution_decisions
  WHERE id=NEW.evolution_decision_id AND library_id=NEW.library_id
    AND command_id=NEW.command_id;
  IF NOT FOUND OR decision_row.lifecycle_status NOT IN ('pending','applied') THEN
    RAISE EXCEPTION 'predicate_evolution_child_decision_invalid';
  END IF;
  SELECT * INTO command_row FROM stable_predicate_evolution_commands
  WHERE id=NEW.command_id AND library_id=NEW.library_id;
  IF NOT FOUND OR command_row.operation_kind IS DISTINCT FROM decision_row.operation_kind THEN
    RAISE EXCEPTION 'predicate_evolution_child_command_invalid';
  END IF;

  IF TG_TABLE_NAME='stable_predicate_evolution_sources' THEN
    IF NEW.evolution_status='historical_only' THEN
      RAISE EXCEPTION 'predicate_evolution_historical_only_forbidden';
    END IF;
    IF NEW.evolution_status<>decision_row.lifecycle_status
       OR command_row.operation_kind NOT IN ('merge','split')
       OR (command_row.operation_kind='merge' AND NOT EXISTS (
         SELECT 1 FROM jsonb_array_elements_text(
           command_row.command_payload_snapshot->'source_predicate_ids'
         ) item WHERE item=NEW.source_predicate_id::text
       ))
       OR (command_row.operation_kind='split'
           AND command_row.command_payload_snapshot->>'source_predicate_id'
               IS DISTINCT FROM NEW.source_predicate_id::text)
    THEN RAISE EXCEPTION 'predicate_evolution_source_membership_invalid'; END IF;
    RETURN NEW;
  END IF;

  SELECT * INTO source_row FROM stable_predicate_evolution_sources
  WHERE id=NEW.source_transition_id AND library_id=NEW.library_id
    AND command_id=NEW.command_id AND evolution_decision_id=NEW.evolution_decision_id
    AND source_predicate_id=NEW.source_predicate_id;

  IF TG_TABLE_NAME='stable_predicate_evolution_successors' THEN
    IF NOT FOUND OR command_row.operation_kind NOT IN ('merge','split')
       OR NEW.planned_target_predicate_id=NEW.source_predicate_id
    THEN RAISE EXCEPTION 'predicate_evolution_successor_membership_invalid'; END IF;
    IF command_row.operation_kind='merge' THEN
      IF NEW.target_ref_kind<>'existing'
         OR NEW.planned_target_predicate_id<>((command_row.command_payload_snapshot->>'survivor_predicate_id')::uuid)
      THEN RAISE EXCEPTION 'predicate_evolution_successor_membership_invalid'; END IF;
    ELSE
      SELECT value INTO member
      FROM jsonb_array_elements(decision_row.operation_payload_snapshot->'successor_slots')
      WHERE value->>'predicate_id'=NEW.planned_target_predicate_id::text;
      IF member IS NULL OR member->>'kind' IS DISTINCT FROM NEW.target_ref_kind
         OR (SELECT count(*) FROM jsonb_object_keys(member)) <>
              (CASE WHEN NEW.target_ref_kind='existing' THEN 2 ELSE 4 END)
         OR (NEW.target_ref_kind='new' AND (
              member->>'target_spec_fingerprint' IS DISTINCT FROM NEW.target_spec_fingerprint
              OR member->'target_spec_snapshot' IS DISTINCT FROM NEW.target_spec_snapshot
         ))
      THEN RAISE EXCEPTION 'predicate_evolution_successor_membership_invalid'; END IF;
    END IF;
    RETURN NEW;
  END IF;

  SELECT * INTO mapping_row FROM stable_predicate_mappings
  WHERE id=NEW.old_mapping_id AND library_id=NEW.library_id;
  IF NOT FOUND OR mapping_row.relation_type_id<>NEW.relation_type_id
     OR mapping_row.stable_predicate_identity_id<>NEW.source_predicate_id
  THEN RAISE EXCEPTION 'predicate_evolution_assignment_mapping_invalid'; END IF;
  IF command_row.operation_kind='reassign' THEN
    member := decision_row.operation_payload_snapshot->'mapping_assignment';
    IF NEW.source_transition_id IS NOT NULL OR NEW.target_successor_id IS NOT NULL
       OR (command_row.command_payload_snapshot->>'mapping_id')::uuid<>NEW.old_mapping_id
       OR (command_row.command_payload_snapshot->>'from_predicate_id')::uuid<>NEW.source_predicate_id
       OR (command_row.command_payload_snapshot->>'to_predicate_id')::uuid IS DISTINCT FROM NEW.target_predicate_id
    THEN RAISE EXCEPTION 'predicate_evolution_reassign_graph_invalid'; END IF;
  ELSE
    IF source_row.id IS NULL THEN
      RAISE EXCEPTION 'predicate_evolution_assignment_source_invalid';
    END IF;
    SELECT value INTO member
    FROM jsonb_array_elements(decision_row.operation_payload_snapshot->'mapping_assignments')
    WHERE value->>'mapping_id'=NEW.old_mapping_id::text;
  END IF;
  IF member IS NULL OR (SELECT count(*) FROM jsonb_object_keys(member))<>6
     OR member->>'mapping_id' IS DISTINCT FROM NEW.old_mapping_id::text
     OR member->>'from_predicate_id' IS DISTINCT FROM NEW.source_predicate_id::text
     OR member->>'state' IS DISTINCT FROM NEW.assignment_state
     OR member->>'reason_code' IS DISTINCT FROM NEW.reason_code
     OR member->'partition_basis_snapshot' IS DISTINCT FROM NEW.partition_basis_snapshot
     OR (NEW.assignment_state='pending' AND member->'target_ref' IS DISTINCT FROM 'null'::jsonb)
     OR (NEW.assignment_state='resolved' AND (
          jsonb_typeof(member->'target_ref')<>'object'
          OR (SELECT count(*) FROM jsonb_object_keys(member->'target_ref'))<>2
     ))
  THEN RAISE EXCEPTION 'predicate_evolution_assignment_membership_invalid'; END IF;
  IF NEW.assignment_state='resolved' THEN
    IF command_row.operation_kind='reassign' THEN
      IF member->'target_ref' IS DISTINCT FROM jsonb_build_object(
           'kind','existing','predicate_id',NEW.target_predicate_id::text
         )
      THEN RAISE EXCEPTION 'predicate_evolution_reassign_graph_invalid'; END IF;
    ELSE
      SELECT * INTO successor_row FROM stable_predicate_evolution_successors
      WHERE id=NEW.target_successor_id AND library_id=NEW.library_id
        AND command_id=NEW.command_id AND evolution_decision_id=NEW.evolution_decision_id
        AND source_transition_id=NEW.source_transition_id
        AND source_predicate_id=NEW.source_predicate_id;
      IF NOT FOUND
         OR member->'target_ref' IS DISTINCT FROM jsonb_build_object(
              'kind',successor_row.target_ref_kind,
              'predicate_id',successor_row.planned_target_predicate_id::text
            )
         OR NEW.target_predicate_id IS DISTINCT FROM successor_row.target_predicate_id
      THEN RAISE EXCEPTION 'predicate_evolution_assignment_target_invalid'; END IF;
    END IF;
  END IF;
  RETURN NEW;
END $$;
""")
    for table in (
        "stable_predicate_evolution_sources",
        "stable_predicate_evolution_successors",
        "stable_predicate_mapping_evolution_assignments",
    ):
        op.execute(f"CREATE TRIGGER trg_stable_predicate_evolution_child_guard BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_evolution_child_guard()")
    op.execute("""
CREATE FUNCTION fn_stable_predicate_evolution_command_has_decision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (SELECT count(*) FROM stable_predicate_evolution_decisions d WHERE d.library_id=NEW.library_id AND d.command_id=NEW.id AND d.supersedes_decision_id IS NULL) <> 1
     OR (SELECT count(*) FROM stable_predicate_evolution_decisions d WHERE d.library_id=NEW.library_id AND d.command_id=NEW.id AND d.lifecycle_status <> 'superseded') <> 1
  THEN RAISE EXCEPTION 'predicate_evolution_command_decision_integrity'; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ct_stable_predicate_evolution_command_has_decision AFTER INSERT ON stable_predicate_evolution_commands DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_evolution_command_has_decision();
""")
    op.execute("""
CREATE FUNCTION fn_stable_predicate_evolution_decision_graph() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  command_row stable_predicate_evolution_commands%ROWTYPE;
  source_scope record;
  assignment_scope record;
  root_count bigint;
  head_count bigint;
  walked_count bigint;
  child_count bigint;
  source_count bigint;
  successor_count bigint;
  assignment_count bigint;
  chain_cycle boolean;
BEGIN
  SELECT * INTO command_row FROM stable_predicate_evolution_commands
  WHERE id=NEW.command_id AND library_id=NEW.library_id;
  IF NOT FOUND OR command_row.operation_kind<>NEW.operation_kind
     OR command_row.command_payload_snapshot->>'schema' IS DISTINCT FROM 'p3_2_stable_predicate_evolution_command_v1'
     OR command_row.command_payload_snapshot->>'operation' IS DISTINCT FROM command_row.operation_kind
     OR command_row.command_payload_snapshot->>'library_id' IS DISTINCT FROM command_row.library_id::text
     OR (SELECT count(*) FROM jsonb_object_keys(command_row.command_payload_snapshot))<>
          (CASE WHEN command_row.operation_kind='reassign' THEN 6 ELSE 5 END)
  THEN RAISE EXCEPTION 'predicate_evolution_command_snapshot_mismatch'; END IF;
  IF (command_row.operation_kind='merge' AND
        command_row.command_payload_snapshot - ARRAY[
          'schema','operation','library_id','source_predicate_ids','survivor_predicate_id'
        ]::text[] <> '{}'::jsonb)
     OR (command_row.operation_kind='split' AND
        command_row.command_payload_snapshot - ARRAY[
          'schema','operation','library_id','source_predicate_id','successor_slots'
        ]::text[] <> '{}'::jsonb)
     OR (command_row.operation_kind='reassign' AND
        command_row.command_payload_snapshot - ARRAY[
          'schema','operation','library_id','mapping_id','from_predicate_id','to_predicate_id'
        ]::text[] <> '{}'::jsonb)
  THEN RAISE EXCEPTION 'predicate_evolution_command_snapshot_mismatch'; END IF;
  IF NEW.lifecycle_status<>NEW.evaluated_outcome THEN
    RAISE EXCEPTION 'predicate_evolution_decision_status_mismatch';
  END IF;
  IF NEW.supersedes_decision_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM stable_predicate_evolution_decisions predecessor
       WHERE predecessor.id=NEW.supersedes_decision_id
         AND predecessor.library_id=NEW.library_id
         AND predecessor.command_id=NEW.command_id
         AND predecessor.lifecycle_status='superseded'
         AND predecessor.evaluated_outcome IN ('pending','rejected','stale')
     )
  THEN RAISE EXCEPTION 'predicate_evolution_predecessor_lifecycle_invalid'; END IF;
  IF NEW.requested_effect='cancel' AND NOT EXISTS (
       SELECT 1 FROM stable_predicate_evolution_decisions predecessor
       WHERE predecessor.id=NEW.supersedes_decision_id
         AND predecessor.library_id=NEW.library_id
         AND predecessor.command_id=NEW.command_id
         AND predecessor.evaluated_outcome='pending'
     )
  THEN RAISE EXCEPTION 'predicate_evolution_cancellation_predecessor_invalid'; END IF;
  IF (NEW.requested_effect='stage' AND NEW.evaluated_outcome<>'pending')
     OR (NEW.requested_effect='cancel' AND NEW.evaluated_outcome<>'cancelled')
     OR (NEW.requested_effect='apply' AND NEW.evaluated_outcome NOT IN ('pending','applied','rejected','stale'))
  THEN RAISE EXCEPTION 'predicate_evolution_requested_effect_mismatch'; END IF;
  IF (NEW.requested_effect='cancel' AND (
        NEW.method<>'authorized_cancellation'
        OR NEW.confidence IS NOT NULL
        OR NEW.operation_payload_snapshot->>'control_kind'<>'cancel_pending'
        OR (SELECT count(*) FROM jsonb_object_keys(NEW.operation_payload_snapshot))<>2
      ))
     OR (NEW.requested_effect<>'cancel' AND (
       (NEW.operation_kind='merge' AND (SELECT count(*) FROM jsonb_object_keys(NEW.operation_payload_snapshot))<>3)
       OR (NEW.operation_kind='split' AND (SELECT count(*) FROM jsonb_object_keys(NEW.operation_payload_snapshot))<>2)
       OR (NEW.operation_kind='reassign' AND (SELECT count(*) FROM jsonb_object_keys(NEW.operation_payload_snapshot))<>1)
     ))
  THEN RAISE EXCEPTION 'predicate_evolution_decision_snapshot_mismatch'; END IF;
  IF (NEW.requested_effect='cancel' AND
        NEW.operation_payload_snapshot - ARRAY[
          'control_kind','expected_pending_decision_id'
        ]::text[] <> '{}'::jsonb)
     OR (NEW.requested_effect<>'cancel' AND NEW.operation_kind='merge' AND
        NEW.operation_payload_snapshot - ARRAY[
          'mapping_assignments','policy_compatibility','survivor_predicate_id'
        ]::text[] <> '{}'::jsonb)
     OR (NEW.requested_effect<>'cancel' AND NEW.operation_kind='split' AND
        NEW.operation_payload_snapshot - ARRAY[
          'mapping_assignments','successor_slots'
        ]::text[] <> '{}'::jsonb)
     OR (NEW.requested_effect<>'cancel' AND NEW.operation_kind='reassign' AND
        NEW.operation_payload_snapshot - 'mapping_assignment' <> '{}'::jsonb)
  THEN RAISE EXCEPTION 'predicate_evolution_decision_snapshot_mismatch'; END IF;

  SELECT count(*) FILTER (WHERE supersedes_decision_id IS NULL),
         count(*) FILTER (WHERE lifecycle_status<>'superseded')
  INTO root_count, head_count FROM stable_predicate_evolution_decisions
  WHERE library_id=NEW.library_id AND command_id=NEW.command_id;
  IF root_count<>1 OR head_count<>1 THEN
    RAISE EXCEPTION 'predicate_evolution_decision_chain_invalid';
  END IF;
  WITH RECURSIVE walk(id, predecessor_id, path, cycle) AS (
    SELECT id, supersedes_decision_id, ARRAY[id], false
    FROM stable_predicate_evolution_decisions
    WHERE library_id=NEW.library_id AND command_id=NEW.command_id
      AND lifecycle_status<>'superseded'
    UNION ALL
    SELECT parent.id, parent.supersedes_decision_id, walk.path||parent.id,
           parent.id=ANY(walk.path)
    FROM stable_predicate_evolution_decisions parent
    JOIN walk ON parent.id=walk.predecessor_id
    WHERE parent.library_id=NEW.library_id AND parent.command_id=NEW.command_id
      AND NOT walk.cycle
  )
  SELECT count(*), COALESCE(bool_or(cycle),false) INTO walked_count,chain_cycle FROM walk;
  IF chain_cycle OR walked_count<>(
    SELECT count(*) FROM stable_predicate_evolution_decisions
    WHERE library_id=NEW.library_id AND command_id=NEW.command_id
  ) THEN RAISE EXCEPTION 'predicate_evolution_decision_chain_invalid'; END IF;

  FOR source_scope IN
    SELECT source_predicate_id FROM stable_predicate_evolution_sources
    WHERE library_id=NEW.library_id AND command_id=NEW.command_id
    GROUP BY source_predicate_id
  LOOP
    SELECT count(*) FILTER (WHERE supersedes_source_transition_id IS NULL),
           count(*) FILTER (WHERE evolution_status<>'superseded')
    INTO root_count,head_count FROM stable_predicate_evolution_sources
    WHERE library_id=NEW.library_id AND command_id=NEW.command_id
      AND source_predicate_id=source_scope.source_predicate_id;
    WITH RECURSIVE walk(id, predecessor_id, path, cycle) AS (
      SELECT id,supersedes_source_transition_id,ARRAY[id],false
      FROM stable_predicate_evolution_sources
      WHERE library_id=NEW.library_id AND command_id=NEW.command_id
        AND source_predicate_id=source_scope.source_predicate_id
        AND (
          evolution_status<>'superseded'
          OR (
            NEW.requested_effect='cancel'
            AND evolution_decision_id=NEW.supersedes_decision_id
          )
        )
      UNION ALL
      SELECT parent.id,parent.supersedes_source_transition_id,walk.path||parent.id,
             parent.id=ANY(walk.path)
      FROM stable_predicate_evolution_sources parent JOIN walk
        ON parent.id=walk.predecessor_id
      WHERE parent.library_id=NEW.library_id AND parent.command_id=NEW.command_id
        AND parent.source_predicate_id=source_scope.source_predicate_id AND NOT walk.cycle
    ) SELECT count(*),COALESCE(bool_or(cycle),false)
      INTO walked_count,chain_cycle FROM walk;
    IF root_count<>1 OR head_count>1 OR chain_cycle OR walked_count<>(
      SELECT count(*) FROM stable_predicate_evolution_sources
      WHERE library_id=NEW.library_id AND command_id=NEW.command_id
        AND source_predicate_id=source_scope.source_predicate_id
    ) THEN RAISE EXCEPTION 'predicate_evolution_source_chain_invalid'; END IF;
  END LOOP;

  FOR assignment_scope IN
    SELECT old_mapping_id FROM stable_predicate_mapping_evolution_assignments
    WHERE library_id=NEW.library_id AND command_id=NEW.command_id
    GROUP BY old_mapping_id
  LOOP
    SELECT count(*) FILTER (WHERE supersedes_assignment_id IS NULL),
           count(*) FILTER (WHERE assignment_state<>'superseded')
    INTO root_count,head_count FROM stable_predicate_mapping_evolution_assignments
    WHERE library_id=NEW.library_id AND command_id=NEW.command_id
      AND old_mapping_id=assignment_scope.old_mapping_id;
    WITH RECURSIVE walk(id, predecessor_id, path, cycle) AS (
      SELECT id,supersedes_assignment_id,ARRAY[id],false
      FROM stable_predicate_mapping_evolution_assignments
      WHERE library_id=NEW.library_id AND command_id=NEW.command_id
        AND old_mapping_id=assignment_scope.old_mapping_id
        AND (
          assignment_state<>'superseded'
          OR (
            NEW.requested_effect='cancel'
            AND evolution_decision_id=NEW.supersedes_decision_id
          )
        )
      UNION ALL
      SELECT parent.id,parent.supersedes_assignment_id,walk.path||parent.id,
             parent.id=ANY(walk.path)
      FROM stable_predicate_mapping_evolution_assignments parent JOIN walk
        ON parent.id=walk.predecessor_id
      WHERE parent.library_id=NEW.library_id AND parent.command_id=NEW.command_id
        AND parent.old_mapping_id=assignment_scope.old_mapping_id AND NOT walk.cycle
    ) SELECT count(*),COALESCE(bool_or(cycle),false)
      INTO walked_count,chain_cycle FROM walk;
    IF root_count<>1 OR head_count>1 OR chain_cycle OR walked_count<>(
      SELECT count(*) FROM stable_predicate_mapping_evolution_assignments
      WHERE library_id=NEW.library_id AND command_id=NEW.command_id
        AND old_mapping_id=assignment_scope.old_mapping_id
    ) THEN RAISE EXCEPTION 'predicate_evolution_assignment_chain_invalid'; END IF;
  END LOOP;

  SELECT count(*) INTO source_count FROM stable_predicate_evolution_sources
  WHERE library_id=NEW.library_id AND command_id=NEW.command_id
    AND evolution_decision_id=NEW.id;
  SELECT count(*) INTO successor_count FROM stable_predicate_evolution_successors
  WHERE library_id=NEW.library_id AND command_id=NEW.command_id
    AND evolution_decision_id=NEW.id;
  SELECT count(*) INTO assignment_count FROM stable_predicate_mapping_evolution_assignments
  WHERE library_id=NEW.library_id AND command_id=NEW.command_id
    AND evolution_decision_id=NEW.id;
  child_count:=source_count+successor_count+assignment_count;

  IF NEW.evaluated_outcome IN ('rejected','stale','cancelled') AND child_count<>0 THEN
    RAISE EXCEPTION 'predicate_evolution_audit_only_has_children';
  END IF;
  IF NEW.evaluated_outcome IN ('rejected','stale','cancelled') THEN RETURN NULL; END IF;
  IF jsonb_typeof(NEW.operation_payload_snapshot) IS DISTINCT FROM 'object'
     OR (NEW.operation_kind IN ('merge','split') AND (
       jsonb_typeof(NEW.operation_payload_snapshot->'mapping_assignments') IS DISTINCT FROM 'array'
       OR jsonb_array_length(NEW.operation_payload_snapshot->'mapping_assignments')>4096
       OR NEW.operation_payload_snapshot->'mapping_assignments' IS DISTINCT FROM
          COALESCE(
            (
              SELECT jsonb_agg(value ORDER BY value->>'mapping_id')
              FROM jsonb_array_elements(
                NEW.operation_payload_snapshot->'mapping_assignments'
              )
            ),
            '[]'::jsonb
          )
     ))
  THEN RAISE EXCEPTION 'predicate_evolution_assignment_limit'; END IF;

  IF NEW.operation_kind='merge' THEN
    IF jsonb_typeof(command_row.command_payload_snapshot->'source_predicate_ids') IS DISTINCT FROM 'array'
       OR jsonb_array_length(command_row.command_payload_snapshot->'source_predicate_ids')<1
       OR source_count<>jsonb_array_length(command_row.command_payload_snapshot->'source_predicate_ids')
       OR successor_count<>source_count
       OR assignment_count<>jsonb_array_length(NEW.operation_payload_snapshot->'mapping_assignments')
       OR NEW.operation_payload_snapshot->>'survivor_predicate_id'
            <>command_row.command_payload_snapshot->>'survivor_predicate_id'
       OR jsonb_typeof(NEW.operation_payload_snapshot->'policy_compatibility') IS DISTINCT FROM 'array'
       OR jsonb_array_length(NEW.operation_payload_snapshot->'policy_compatibility')<>source_count+1
       OR EXISTS (
         SELECT 1 FROM stable_predicate_identities predicate
         WHERE predicate.library_id=NEW.library_id
           AND (predicate.id=(command_row.command_payload_snapshot->>'survivor_predicate_id')::uuid
             OR predicate.id IN (
               SELECT s.source_predicate_id FROM stable_predicate_evolution_sources s
               WHERE s.evolution_decision_id=NEW.id
             ))
           AND NOT EXISTS (
             SELECT 1 FROM jsonb_array_elements(
               NEW.operation_payload_snapshot->'policy_compatibility'
             ) snapshot
             WHERE snapshot=jsonb_build_object(
               'contract_version',predicate.contract_version,
               'identity_policy_version',predicate.identity_policy_version,
               'key',predicate.key,'namespace',predicate.namespace,
               'predicate_id',predicate.id::text,
               'resolution_policy',predicate.resolution_policy,
               'temporal_class',predicate.temporal_class
             )
           )
       )
       OR EXISTS (
         SELECT 1 FROM stable_predicate_identities predicate
         JOIN stable_predicate_identities survivor
           ON survivor.id=(command_row.command_payload_snapshot->>'survivor_predicate_id')::uuid
          AND survivor.library_id=NEW.library_id
         WHERE predicate.library_id=NEW.library_id
           AND predicate.id IN (
             SELECT s.source_predicate_id FROM stable_predicate_evolution_sources s
             WHERE s.evolution_decision_id=NEW.id
           )
           AND (predicate.temporal_class,predicate.identity_policy_version,predicate.resolution_policy)
               IS DISTINCT FROM
               (survivor.temporal_class,survivor.identity_policy_version,survivor.resolution_policy)
       )
       OR EXISTS (
         SELECT 1 FROM stable_predicate_evolution_sources s
         LEFT JOIN stable_predicate_evolution_successors successor
           ON successor.source_transition_id=s.id AND successor.library_id=s.library_id
         WHERE s.evolution_decision_id=NEW.id
         GROUP BY s.id
         HAVING count(successor.id)<>1
            OR min(successor.planned_target_predicate_id::text)<>
               command_row.command_payload_snapshot->>'survivor_predicate_id'
       )
       OR EXISTS (
         SELECT 1 FROM stable_predicate_mappings mapping
         WHERE mapping.library_id=NEW.library_id
           AND mapping.stable_predicate_identity_id IN (
             SELECT s.source_predicate_id FROM stable_predicate_evolution_sources s
             WHERE s.evolution_decision_id=NEW.id
           )
           AND mapping.mapping_status=(CASE WHEN NEW.evaluated_outcome='pending' THEN 'active' ELSE 'superseded' END)
           AND NOT EXISTS (
             SELECT 1 FROM stable_predicate_mapping_evolution_assignments a
             WHERE a.evolution_decision_id=NEW.id AND a.old_mapping_id=mapping.id
           )
       )
    THEN RAISE EXCEPTION 'predicate_evolution_merge_graph_invalid'; END IF;
  ELSIF NEW.operation_kind='split' THEN
    IF source_count<>1
       OR jsonb_typeof(command_row.command_payload_snapshot->'successor_slots') IS DISTINCT FROM 'array'
       OR jsonb_array_length(command_row.command_payload_snapshot->'successor_slots')<2
       OR successor_count<>jsonb_array_length(command_row.command_payload_snapshot->'successor_slots')
       OR assignment_count<>jsonb_array_length(NEW.operation_payload_snapshot->'mapping_assignments')
       OR command_row.command_payload_snapshot->'successor_slots' IS DISTINCT FROM (
         SELECT jsonb_agg(
           CASE WHEN value->>'kind'='new'
             THEN value-'target_spec_snapshot' ELSE value END
           ORDER BY CASE value->>'kind' WHEN 'existing' THEN 0 ELSE 1 END,
                    value->>'predicate_id'
         ) FROM jsonb_array_elements(NEW.operation_payload_snapshot->'successor_slots')
       )
       OR NEW.operation_payload_snapshot->'successor_slots' IS DISTINCT FROM
          (SELECT jsonb_agg(value ORDER BY CASE value->>'kind' WHEN 'existing' THEN 0 ELSE 1 END, value->>'predicate_id')
           FROM jsonb_array_elements(NEW.operation_payload_snapshot->'successor_slots'))
       OR EXISTS (
         SELECT 1 FROM stable_predicate_mappings mapping
         JOIN stable_predicate_evolution_sources s
           ON s.evolution_decision_id=NEW.id
          AND s.source_predicate_id=mapping.stable_predicate_identity_id
         WHERE mapping.library_id=NEW.library_id
           AND mapping.mapping_status=(CASE WHEN NEW.evaluated_outcome='pending' THEN 'active' ELSE 'superseded' END)
           AND NOT EXISTS (
             SELECT 1 FROM stable_predicate_mapping_evolution_assignments a
             WHERE a.evolution_decision_id=NEW.id AND a.old_mapping_id=mapping.id
           )
       )
    THEN RAISE EXCEPTION 'predicate_evolution_split_graph_invalid'; END IF;
  ELSE
    IF source_count<>0 OR successor_count<>0 OR assignment_count<>1
       OR (command_row.command_payload_snapshot->>'mapping_id') IS NULL
       OR NEW.operation_payload_snapshot->'mapping_assignment' IS NULL
    THEN RAISE EXCEPTION 'predicate_evolution_reassign_graph_invalid'; END IF;
  END IF;

  IF NEW.evaluated_outcome='pending' AND (
       EXISTS (SELECT 1 FROM stable_predicate_mapping_evolution_assignments a
               WHERE a.evolution_decision_id=NEW.id AND a.new_mapping_id IS NOT NULL)
       OR EXISTS (
         SELECT 1 FROM stable_predicate_evolution_successors successor
         JOIN stable_predicate_identities predicate
           ON predicate.id=successor.planned_target_predicate_id
          AND predicate.library_id=successor.library_id
         WHERE successor.evolution_decision_id=NEW.id
           AND successor.target_ref_kind='new'
       )
  ) THEN RAISE EXCEPTION 'predicate_evolution_pending_has_effect'; END IF;

  IF NEW.evaluated_outcome='applied' AND EXISTS (
    SELECT 1 FROM stable_predicate_mapping_evolution_assignments a
    LEFT JOIN stable_predicate_mappings old_mapping
      ON old_mapping.id=a.old_mapping_id AND old_mapping.library_id=a.library_id
    LEFT JOIN stable_predicate_mappings replacement
      ON replacement.id=a.new_mapping_id AND replacement.library_id=a.library_id
    WHERE a.evolution_decision_id=NEW.id
      AND (a.assignment_state<>'resolved' OR a.target_predicate_id IS NULL
        OR a.new_mapping_id IS NULL OR a.new_mapping_id=a.old_mapping_id
        OR old_mapping.mapping_status<>'superseded' OR old_mapping.superseded_at IS NULL
        OR replacement.mapping_status<>'active' OR replacement.superseded_at IS NOT NULL
        OR replacement.stable_predicate_identity_id<>a.target_predicate_id
        OR replacement.relation_type_id<>a.relation_type_id
        OR replacement.evolution_assignment_id<>a.id)
  ) THEN RAISE EXCEPTION 'predicate_evolution_mapping_pair_mismatch'; END IF;

  IF EXISTS (
    SELECT 1 FROM stable_predicate_evolution_successors successor
    LEFT JOIN stable_predicate_identities predicate
      ON predicate.id=successor.target_predicate_id AND predicate.library_id=successor.library_id
    WHERE successor.evolution_decision_id=NEW.id AND successor.target_ref_kind='new'
      AND (NEW.evaluated_outcome='applied' AND (
        successor.target_predicate_id<>successor.planned_target_predicate_id
        OR predicate.id IS NULL
        OR successor.target_spec_snapshot IS DISTINCT FROM jsonb_build_object(
          'schema','p3_2_stable_predicate_target_v1',
          'namespace',predicate.namespace,'key',predicate.key,
          'contract_version',predicate.contract_version,
          'temporal_class',predicate.temporal_class,
          'identity_policy_version',predicate.identity_policy_version,
          'resolution_status',predicate.resolution_status,
          'resolution_policy',predicate.resolution_policy
        )
      ))
  ) THEN RAISE EXCEPTION 'predicate_evolution_target_spec_mismatch'; END IF;

  IF NEW.evaluated_outcome='applied' AND EXISTS (
    WITH RECURSIVE walk(origin,node,path,cycle) AS (
      SELECT s.source_predicate_id, successor.target_predicate_id,
             ARRAY[s.source_predicate_id,successor.target_predicate_id],
             successor.target_predicate_id=s.source_predicate_id
      FROM stable_predicate_evolution_sources s
      JOIN stable_predicate_evolution_successors successor ON successor.source_transition_id=s.id
      WHERE s.evolution_decision_id=NEW.id AND s.evolution_status='applied'
      UNION ALL
      SELECT walk.origin,next_successor.target_predicate_id,
             walk.path||next_successor.target_predicate_id,
             next_successor.target_predicate_id=ANY(walk.path)
      FROM walk
      JOIN stable_predicate_evolution_sources next_source
        ON next_source.library_id=NEW.library_id
       AND next_source.source_predicate_id=walk.node
       AND next_source.evolution_status='applied'
      JOIN stable_predicate_evolution_successors next_successor
        ON next_successor.source_transition_id=next_source.id
      WHERE NOT walk.cycle
    ) SELECT 1 FROM walk WHERE cycle
  ) THEN RAISE EXCEPTION 'predicate_evolution_lineage_cycle'; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ct_stable_predicate_evolution_decision_graph AFTER INSERT ON stable_predicate_evolution_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_evolution_decision_graph();
""")
    op.execute("""
CREATE FUNCTION fn_stable_predicate_evolution_chain_local() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME='stable_predicate_evolution_decisions' AND NOT EXISTS (SELECT 1 FROM stable_predicate_evolution_decisions n WHERE n.library_id=OLD.library_id AND n.command_id=OLD.command_id AND n.supersedes_decision_id=OLD.id) THEN RAISE EXCEPTION 'predicate_evolution_broken_decision_chain'; END IF;
  IF TG_TABLE_NAME='stable_predicate_evolution_sources' AND NOT EXISTS (SELECT 1 FROM stable_predicate_evolution_sources n WHERE n.library_id=OLD.library_id AND n.command_id=OLD.command_id AND n.source_predicate_id=(to_jsonb(OLD)->>'source_predicate_id')::uuid AND n.supersedes_source_transition_id=OLD.id) AND NOT EXISTS (SELECT 1 FROM stable_predicate_evolution_decisions d WHERE d.library_id=OLD.library_id AND d.command_id=OLD.command_id AND d.supersedes_decision_id=(to_jsonb(OLD)->>'evolution_decision_id')::uuid AND d.lifecycle_status='cancelled') THEN RAISE EXCEPTION 'predicate_evolution_broken_source_chain'; END IF;
  IF TG_TABLE_NAME='stable_predicate_mapping_evolution_assignments' AND NOT EXISTS (SELECT 1 FROM stable_predicate_mapping_evolution_assignments n WHERE n.library_id=OLD.library_id AND n.command_id=OLD.command_id AND n.old_mapping_id=(to_jsonb(OLD)->>'old_mapping_id')::uuid AND n.supersedes_assignment_id=OLD.id) AND NOT EXISTS (SELECT 1 FROM stable_predicate_evolution_decisions d WHERE d.library_id=OLD.library_id AND d.command_id=OLD.command_id AND d.supersedes_decision_id=(to_jsonb(OLD)->>'evolution_decision_id')::uuid AND d.lifecycle_status='cancelled') THEN RAISE EXCEPTION 'predicate_evolution_broken_assignment_chain'; END IF;
  RETURN NULL;
END $$;
""")
    for table in ("stable_predicate_evolution_decisions", "stable_predicate_evolution_sources", "stable_predicate_mapping_evolution_assignments"):
        op.execute(f"CREATE CONSTRAINT TRIGGER ct_stable_predicate_evolution_chain_local AFTER UPDATE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_evolution_chain_local()")
    op.execute("""
CREATE FUNCTION fn_stable_predicate_mapping_evolution_pair() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE assignment_row stable_predicate_mapping_evolution_assignments%ROWTYPE;
BEGIN
  IF TG_TABLE_NAME='stable_predicate_mappings' THEN
    IF TG_OP='DELETE' AND EXISTS (
      SELECT 1 FROM stable_predicate_mapping_evolution_assignments a
      WHERE a.library_id=OLD.library_id AND (a.old_mapping_id=OLD.id OR a.new_mapping_id=OLD.id)
    ) THEN RAISE EXCEPTION 'predicate_mapping_evolution_pair_mismatch'; END IF;
    IF TG_OP<>'DELETE' AND NEW.evolution_assignment_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM stable_predicate_mapping_evolution_assignments a
        JOIN stable_predicate_evolution_decisions d ON d.id=a.evolution_decision_id
        WHERE a.id=NEW.evolution_assignment_id AND a.library_id=NEW.library_id
          AND a.new_mapping_id=NEW.id AND a.target_predicate_id=NEW.stable_predicate_identity_id
          AND a.relation_type_id=NEW.relation_type_id AND a.assignment_state='resolved'
          AND d.lifecycle_status='applied')
    THEN RAISE EXCEPTION 'predicate_mapping_evolution_pair_mismatch'; END IF;
    IF TG_OP='UPDATE' AND (
         NEW.id IS DISTINCT FROM OLD.id
         OR NEW.library_id IS DISTINCT FROM OLD.library_id
         OR NEW.stable_predicate_identity_id IS DISTINCT FROM OLD.stable_predicate_identity_id
         OR NEW.relation_type_id IS DISTINCT FROM OLD.relation_type_id
         OR NEW.created_at IS DISTINCT FROM OLD.created_at
         OR NEW.evolution_assignment_id IS DISTINCT FROM OLD.evolution_assignment_id
         OR (NEW.mapping_status IS DISTINCT FROM OLD.mapping_status AND NOT (
           OLD.mapping_status='active' AND NEW.mapping_status='superseded'
           AND OLD.superseded_at IS NULL AND NEW.superseded_at IS NOT NULL
         ))
         OR (NEW.mapping_status IS NOT DISTINCT FROM OLD.mapping_status
             AND NEW.superseded_at IS DISTINCT FROM OLD.superseded_at)
       )
    THEN RAISE EXCEPTION 'predicate_mapping_evolution_pair_mismatch'; END IF;
    IF TG_OP='UPDATE' AND OLD.mapping_status='active' AND NEW.mapping_status='superseded'
       AND NOT EXISTS (
         SELECT 1 FROM stable_predicate_mapping_evolution_assignments a
         JOIN stable_predicate_evolution_decisions d ON d.id=a.evolution_decision_id
         WHERE a.library_id=NEW.library_id AND a.old_mapping_id=NEW.id
           AND a.assignment_state='resolved' AND d.lifecycle_status='applied'
       ) THEN RAISE EXCEPTION 'predicate_mapping_evolution_pair_mismatch'; END IF;
  ELSE
    SELECT * INTO assignment_row FROM stable_predicate_mapping_evolution_assignments
    WHERE id=NEW.id AND library_id=NEW.library_id;
    IF assignment_row.new_mapping_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM stable_predicate_mappings mapping
      WHERE mapping.id=assignment_row.new_mapping_id AND mapping.library_id=assignment_row.library_id
        AND mapping.evolution_assignment_id=assignment_row.id
        AND mapping.stable_predicate_identity_id=assignment_row.target_predicate_id
        AND mapping.relation_type_id=assignment_row.relation_type_id
    ) THEN RAISE EXCEPTION 'predicate_mapping_evolution_pair_mismatch'; END IF;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ct_stable_predicate_mapping_evolution_pair AFTER INSERT OR UPDATE OR DELETE ON stable_predicate_mappings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_mapping_evolution_pair();
CREATE CONSTRAINT TRIGGER ct_stable_predicate_mapping_evolution_pair AFTER INSERT OR UPDATE ON stable_predicate_mapping_evolution_assignments DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_mapping_evolution_pair();
""")
    op.execute("""
CREATE FUNCTION fn_stable_predicate_identity_evolution_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM stable_predicate_evolution_sources s WHERE s.source_predicate_id=OLD.id)
     OR EXISTS (SELECT 1 FROM stable_predicate_evolution_successors s WHERE s.planned_target_predicate_id=OLD.id OR s.target_predicate_id=OLD.id)
     OR EXISTS (SELECT 1 FROM stable_predicate_mapping_evolution_assignments a WHERE a.source_predicate_id=OLD.id OR a.target_predicate_id=OLD.id)
  THEN
    IF TG_OP='DELETE' OR (OLD.namespace,OLD.key,OLD.contract_version,OLD.temporal_class,OLD.identity_policy_version,OLD.resolution_status,OLD.resolution_policy) IS DISTINCT FROM (NEW.namespace,NEW.key,NEW.contract_version,NEW.temporal_class,NEW.identity_policy_version,NEW.resolution_status,NEW.resolution_policy)
    THEN RAISE EXCEPTION 'predicate_identity_evolution_participant_immutable'; END IF;
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ct_stable_predicate_identity_evolution_guard BEFORE UPDATE OR DELETE ON stable_predicate_identities FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_identity_evolution_guard();
""")


def _assert_catalog() -> None:
    op.execute("""
CREATE FUNCTION fn_p3_2_0073_assert_catalog() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF to_regclass('stable_predicate_evolution_commands') IS NULL
     OR to_regclass('stable_predicate_evolution_decisions') IS NULL
     OR to_regclass('stable_predicate_evolution_sources') IS NULL
     OR to_regclass('stable_predicate_evolution_successors') IS NULL
     OR to_regclass('stable_predicate_mapping_evolution_assignments') IS NULL
     OR NOT EXISTS (
       SELECT 1 FROM information_schema.columns
       WHERE table_schema=current_schema() AND table_name='stable_predicate_mappings'
         AND column_name='evolution_assignment_id' AND is_nullable='YES'
     )
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='trg_stable_predicate_evolution_append_only')<>5
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='trg_stable_predicate_evolution_child_guard')<>3
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_stable_predicate_evolution_chain_local')<>3
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_stable_predicate_mapping_evolution_pair')<>2
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_stable_predicate_identity_evolution_guard')<>1
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_stable_predicate_evolution_command_has_decision')<>1
     OR (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal
         AND tgname='ct_stable_predicate_evolution_decision_graph')<>1
  THEN RAISE EXCEPTION 'P3_2_0073_CATALOG_MISMATCH'; END IF;
END $$;
SELECT fn_p3_2_0073_assert_catalog();
DROP FUNCTION fn_p3_2_0073_assert_catalog();
""")


def upgrade() -> None:
    _set_timeouts()
    op.execute(_UPGRADE_LOCK_SQL)
    _assert_preconditions()
    _create_tables()
    _link_mappings()
    _install_triggers()
    _assert_catalog()


def downgrade() -> None:
    _set_timeouts()
    op.execute(_DOWNGRADE_LOCK_SQL)
    op.execute("DROP FUNCTION IF EXISTS fn_p3_2_0073_assert_catalog()")
    op.execute("DROP FUNCTION IF EXISTS fn_p3_2_0073_assert_preconditions()")
    op.execute("""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM stable_predicate_evolution_commands)
     OR EXISTS (SELECT 1 FROM stable_predicate_evolution_decisions)
     OR EXISTS (SELECT 1 FROM stable_predicate_evolution_sources)
     OR EXISTS (SELECT 1 FROM stable_predicate_evolution_successors)
     OR EXISTS (SELECT 1 FROM stable_predicate_mapping_evolution_assignments)
     OR EXISTS (SELECT 1 FROM stable_predicate_mappings WHERE evolution_assignment_id IS NOT NULL)
  THEN RAISE EXCEPTION 'P3_2_0073_NONEMPTY_AUDIT'; END IF;
END $$;
""")
    op.execute("DROP TRIGGER ct_stable_predicate_identity_evolution_guard ON stable_predicate_identities")
    op.execute("DROP FUNCTION fn_stable_predicate_identity_evolution_guard()")
    op.execute("DROP TRIGGER ct_stable_predicate_mapping_evolution_pair ON stable_predicate_mappings")
    op.execute("DROP TRIGGER ct_stable_predicate_mapping_evolution_pair ON stable_predicate_mapping_evolution_assignments")
    op.execute("DROP FUNCTION fn_stable_predicate_mapping_evolution_pair()")
    op.drop_constraint("fk_stable_predicate_mappings_evolution_assignment", "stable_predicate_mappings", type_="foreignkey")
    op.drop_constraint("fk_stable_predicate_mapping_evolution_assignments_new_mapping", "stable_predicate_mapping_evolution_assignments", type_="foreignkey")
    op.drop_constraint("fk_stable_predicate_mapping_evolution_assignments_old_mapping", "stable_predicate_mapping_evolution_assignments", type_="foreignkey")
    for table in ("stable_predicate_evolution_decisions", "stable_predicate_evolution_sources", "stable_predicate_mapping_evolution_assignments"):
        op.execute(f"DROP TRIGGER ct_stable_predicate_evolution_chain_local ON {table}")
    op.execute("DROP FUNCTION fn_stable_predicate_evolution_chain_local()")
    op.execute("DROP TRIGGER ct_stable_predicate_evolution_decision_graph ON stable_predicate_evolution_decisions")
    op.execute("DROP FUNCTION fn_stable_predicate_evolution_decision_graph()")
    op.execute("DROP TRIGGER ct_stable_predicate_evolution_command_has_decision ON stable_predicate_evolution_commands")
    op.execute("DROP FUNCTION fn_stable_predicate_evolution_command_has_decision()")
    for table in (
        "stable_predicate_evolution_sources",
        "stable_predicate_evolution_successors",
        "stable_predicate_mapping_evolution_assignments",
    ):
        op.execute(f"DROP TRIGGER trg_stable_predicate_evolution_child_guard ON {table}")
    op.execute("DROP FUNCTION fn_stable_predicate_evolution_child_guard()")
    for table in _AUDIT_TABLES:
        op.execute(f"DROP TRIGGER trg_stable_predicate_evolution_append_only ON {table}")
    op.execute("DROP FUNCTION fn_stable_predicate_evolution_append_only()")
    for table in reversed(_AUDIT_TABLES):
        op.drop_table(table)
    op.drop_column("stable_predicate_mappings", "evolution_assignment_id")
    op.drop_constraint("uq_stable_predicate_mappings_id_library", "stable_predicate_mappings", type_="unique")
