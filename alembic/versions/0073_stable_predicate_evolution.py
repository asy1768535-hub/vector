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
        sa.CheckConstraint("(target_ref_kind = 'existing' AND target_predicate_id = planned_target_predicate_id AND target_spec_fingerprint IS NULL AND target_spec_snapshot IS NULL) OR (target_ref_kind = 'new' AND target_spec_fingerprint IS NOT NULL AND target_spec_snapshot IS NOT NULL)", name="ck_stable_predicate_evolution_successors_shape"),
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
     AND OLD.lifecycle_status <> 'superseded' AND NEW.lifecycle_status = 'superseded'
     AND to_jsonb(NEW) - 'lifecycle_status' = to_jsonb(OLD) - 'lifecycle_status' THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME = 'stable_predicate_evolution_sources'
     AND OLD.evolution_status <> 'superseded' AND NEW.evolution_status = 'superseded'
     AND to_jsonb(NEW) - 'evolution_status' = to_jsonb(OLD) - 'evolution_status' THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME = 'stable_predicate_mapping_evolution_assignments'
     AND OLD.assignment_state <> 'superseded' AND NEW.assignment_state = 'superseded'
     AND to_jsonb(NEW) - 'assignment_state' = to_jsonb(OLD) - 'assignment_state' THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'predicate_evolution_append_only';
END $$;
""")
    for table in _AUDIT_TABLES:
        op.execute(f"CREATE TRIGGER trg_stable_predicate_evolution_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_evolution_append_only()")
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
DECLARE child_count bigint;
BEGIN
  IF NEW.lifecycle_status <> NEW.evaluated_outcome THEN RAISE EXCEPTION 'predicate_evolution_decision_status_mismatch'; END IF;
  IF (NEW.requested_effect='stage' AND NEW.evaluated_outcome<>'pending') OR (NEW.requested_effect='cancel' AND NEW.evaluated_outcome<>'cancelled')
     OR (NEW.requested_effect='apply' AND NEW.evaluated_outcome NOT IN ('pending','applied','rejected','stale'))
  THEN RAISE EXCEPTION 'predicate_evolution_requested_effect_mismatch'; END IF;
  SELECT (SELECT count(*) FROM stable_predicate_evolution_sources s WHERE s.evolution_decision_id=NEW.id)
       + (SELECT count(*) FROM stable_predicate_mapping_evolution_assignments a WHERE a.evolution_decision_id=NEW.id) INTO child_count;
  IF NEW.evaluated_outcome IN ('rejected','stale','cancelled') AND child_count <> 0 THEN RAISE EXCEPTION 'predicate_evolution_audit_only_has_children'; END IF;
  IF NEW.evaluated_outcome='applied' AND EXISTS (SELECT 1 FROM stable_predicate_mapping_evolution_assignments a WHERE a.evolution_decision_id=NEW.id AND (a.assignment_state<>'resolved' OR a.target_predicate_id IS NULL OR a.new_mapping_id IS NULL))
  THEN RAISE EXCEPTION 'predicate_evolution_applied_assignment_incomplete'; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ct_stable_predicate_evolution_decision_graph AFTER INSERT ON stable_predicate_evolution_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_evolution_decision_graph();
""")
    op.execute("""
CREATE FUNCTION fn_stable_predicate_evolution_chain_local() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME='stable_predicate_evolution_decisions' AND NOT EXISTS (SELECT 1 FROM stable_predicate_evolution_decisions n WHERE n.library_id=OLD.library_id AND n.command_id=OLD.command_id AND n.supersedes_decision_id=OLD.id) THEN RAISE EXCEPTION 'predicate_evolution_broken_decision_chain'; END IF;
  IF TG_TABLE_NAME='stable_predicate_evolution_sources' AND NOT EXISTS (SELECT 1 FROM stable_predicate_evolution_sources n WHERE n.library_id=OLD.library_id AND n.command_id=OLD.command_id AND n.source_predicate_id=OLD.source_predicate_id AND n.supersedes_source_transition_id=OLD.id) AND NOT EXISTS (SELECT 1 FROM stable_predicate_evolution_decisions d WHERE d.command_id=OLD.command_id AND d.lifecycle_status='cancelled') THEN RAISE EXCEPTION 'predicate_evolution_broken_source_chain'; END IF;
  IF TG_TABLE_NAME='stable_predicate_mapping_evolution_assignments' AND NOT EXISTS (SELECT 1 FROM stable_predicate_mapping_evolution_assignments n WHERE n.library_id=OLD.library_id AND n.command_id=OLD.command_id AND n.old_mapping_id=OLD.old_mapping_id AND n.supersedes_assignment_id=OLD.id) AND NOT EXISTS (SELECT 1 FROM stable_predicate_evolution_decisions d WHERE d.command_id=OLD.command_id AND d.lifecycle_status='cancelled') THEN RAISE EXCEPTION 'predicate_evolution_broken_assignment_chain'; END IF;
  RETURN NULL;
END $$;
""")
    for table in ("stable_predicate_evolution_decisions", "stable_predicate_evolution_sources", "stable_predicate_mapping_evolution_assignments"):
        op.execute(f"CREATE CONSTRAINT TRIGGER ct_stable_predicate_evolution_chain_local AFTER UPDATE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_evolution_chain_local()")
    op.execute("""
CREATE FUNCTION fn_stable_predicate_mapping_evolution_pair() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.evolution_assignment_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM stable_predicate_mapping_evolution_assignments a
      WHERE a.id=NEW.evolution_assignment_id AND a.library_id=NEW.library_id AND a.new_mapping_id=NEW.id
        AND a.target_predicate_id=NEW.stable_predicate_identity_id AND a.relation_type_id=NEW.relation_type_id)
  THEN RAISE EXCEPTION 'predicate_mapping_evolution_pair_mismatch'; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ct_stable_predicate_mapping_evolution_pair AFTER INSERT OR UPDATE ON stable_predicate_mappings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fn_stable_predicate_mapping_evolution_pair();
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


def upgrade() -> None:
    _set_timeouts()
    op.execute(_UPGRADE_LOCK_SQL)
    _create_tables()
    _link_mappings()
    _install_triggers()


def downgrade() -> None:
    _set_timeouts()
    op.execute(_DOWNGRADE_LOCK_SQL)
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
    op.execute("DROP FUNCTION fn_stable_predicate_mapping_evolution_pair()")
    op.drop_constraint("fk_stable_predicate_mappings_evolution_assignment", "stable_predicate_mappings", type_="foreignkey")
    op.drop_constraint("fk_stable_predicate_mapping_evolution_assignments_new_mapping", "stable_predicate_mapping_evolution_assignments", type_="foreignkey")
    op.drop_constraint("fk_stable_predicate_mapping_evolution_assignments_old_mapping", "stable_predicate_mapping_evolution_assignments", type_="foreignkey")
    op.drop_column("stable_predicate_mappings", "evolution_assignment_id")
    op.drop_constraint("uq_stable_predicate_mappings_id_library", "stable_predicate_mappings", type_="unique")
    for table in ("stable_predicate_evolution_decisions", "stable_predicate_evolution_sources", "stable_predicate_mapping_evolution_assignments"):
        op.execute(f"DROP TRIGGER ct_stable_predicate_evolution_chain_local ON {table}")
    op.execute("DROP FUNCTION fn_stable_predicate_evolution_chain_local()")
    op.execute("DROP TRIGGER ct_stable_predicate_evolution_decision_graph ON stable_predicate_evolution_decisions")
    op.execute("DROP FUNCTION fn_stable_predicate_evolution_decision_graph()")
    op.execute("DROP TRIGGER ct_stable_predicate_evolution_command_has_decision ON stable_predicate_evolution_commands")
    op.execute("DROP FUNCTION fn_stable_predicate_evolution_command_has_decision()")
    for table in _AUDIT_TABLES:
        op.execute(f"DROP TRIGGER trg_stable_predicate_evolution_append_only ON {table}")
    op.execute("DROP FUNCTION fn_stable_predicate_evolution_append_only()")
    for table in reversed(_AUDIT_TABLES):
        op.drop_table(table)
