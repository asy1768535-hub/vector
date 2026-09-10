"""Repair P3.3 polymorphic trigger record access without rewriting 0074.

Revision 0074 is retained as immutable migration history. This maintenance-only
repair replaces shared trigger functions so fields that exist only on one of
their trigger tables are read from JSON records rather than typed NEW/OLD rows.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCK_SQL = """
DO $p3_3_0075_migration_lock$
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
$p3_3_0075_migration_lock$;
"""


def _set_timeouts() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")


def _assert_preconditions() -> None:
    op.execute(
        """
DO $$
BEGIN
  IF to_regclass('fact_reconciliation_commands') IS NULL
     OR to_regclass('fact_reconciliation_decisions') IS NULL
     OR to_regclass('fact_reconciliation_sources') IS NULL
     OR to_regclass('fact_reconciliation_target_slots') IS NULL
     OR to_regclass('fact_reconciliation_source_target_edges') IS NULL
     OR to_regclass('fact_reconciliation_assertion_assignments') IS NULL
     OR to_regprocedure('fn_fact_reconciliation_append_only()') IS NULL
     OR to_regprocedure('fn_fact_reconciliation_child_guard()') IS NULL
     OR to_regprocedure('fn_fact_reconciliation_decision_graph()') IS NULL
     OR to_regprocedure('fn_fact_reconciliation_chain_local()') IS NULL
     OR to_regprocedure('fn_fact_reconciliation_logical_fact_pair()') IS NULL
  THEN RAISE EXCEPTION 'P3_3_0075_PRECONDITION_MISMATCH'; END IF;
END $$;
"""
    )


def _install_safe_trigger_functions() -> None:
    op.execute(
        """
CREATE OR REPLACE FUNCTION fn_fact_reconciliation_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_row jsonb;
DECLARE new_row jsonb;
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  old_row := to_jsonb(OLD);
  new_row := to_jsonb(NEW);
  IF TG_TABLE_NAME='fact_reconciliation_decisions' THEN
    IF (new_row-'lifecycle_status') IS DISTINCT FROM (old_row-'lifecycle_status')
       OR old_row->>'lifecycle_status'='superseded' OR new_row->>'lifecycle_status'<>'superseded'
    THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  ELSIF TG_TABLE_NAME='fact_reconciliation_sources' THEN
    IF (new_row-'resolution_state') IS DISTINCT FROM (old_row-'resolution_state')
       OR old_row->>'resolution_state'='superseded' OR new_row->>'resolution_state'<>'superseded'
    THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  ELSIF TG_TABLE_NAME='fact_reconciliation_target_slots' THEN
    IF (new_row-'slot_state') IS DISTINCT FROM (old_row-'slot_state')
       OR old_row->>'slot_state'='superseded' OR new_row->>'slot_state'<>'superseded'
    THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  ELSIF TG_TABLE_NAME='fact_reconciliation_source_target_edges' THEN
    IF (new_row-'edge_state') IS DISTINCT FROM (old_row-'edge_state')
       OR old_row->>'edge_state'='superseded' OR new_row->>'edge_state'<>'superseded'
    THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  ELSIF TG_TABLE_NAME='fact_reconciliation_assertion_assignments' THEN
    IF (new_row-'assignment_state') IS DISTINCT FROM (old_row-'assignment_state')
       OR old_row->>'assignment_state'='superseded' OR new_row->>'assignment_state'<>'superseded'
    THEN RAISE EXCEPTION 'fact_reconciliation_append_only'; END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION fn_fact_reconciliation_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE decision_state text;
BEGIN
  SELECT lifecycle_status INTO decision_state
  FROM fact_reconciliation_decisions
  WHERE id=NEW.evolution_decision_id AND library_id=NEW.library_id AND command_id=NEW.command_id;
  IF decision_state NOT IN ('pending','applied','superseded') THEN
    RAISE EXCEPTION 'fact_reconciliation_child_decision_invalid';
  END IF;
  IF TG_TABLE_NAME='fact_reconciliation_sources'
     AND to_jsonb(NEW)->>'resolution_state'='historical_only'
  THEN RAISE EXCEPTION 'fact_reconciliation_historical_only_write_invalid'; END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION fn_fact_reconciliation_decision_graph() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE predecessor_outcome text;
DECLARE current_lifecycle_status text;
BEGIN
  SELECT lifecycle_status INTO current_lifecycle_status
  FROM fact_reconciliation_decisions
  WHERE id=NEW.id AND library_id=NEW.library_id AND command_id=NEW.command_id;
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
  IF current_lifecycle_status IN ('pending','applied') THEN
    IF NOT EXISTS (SELECT 1 FROM fact_reconciliation_sources WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id)
       OR NOT EXISTS (SELECT 1 FROM fact_reconciliation_target_slots WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id)
       OR NOT EXISTS (SELECT 1 FROM fact_reconciliation_source_target_edges WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id)
       OR NOT EXISTS (SELECT 1 FROM fact_reconciliation_assertion_assignments WHERE evolution_decision_id=NEW.id AND library_id=NEW.library_id)
    THEN RAISE EXCEPTION 'fact_reconciliation_incomplete_children'; END IF;
    IF current_lifecycle_status='pending' AND (
      EXISTS (SELECT 1 FROM fact_reconciliation_sources WHERE evolution_decision_id=NEW.id AND resolution_state<>'pending')
      OR EXISTS (SELECT 1 FROM fact_reconciliation_target_slots WHERE evolution_decision_id=NEW.id AND slot_state<>'pending')
      OR EXISTS (SELECT 1 FROM fact_reconciliation_source_target_edges WHERE evolution_decision_id=NEW.id AND edge_state<>'pending')
      OR NOT EXISTS (SELECT 1 FROM fact_reconciliation_assertion_assignments WHERE evolution_decision_id=NEW.id AND assignment_state='pending')
      OR EXISTS (SELECT 1 FROM fact_reconciliation_target_slots WHERE evolution_decision_id=NEW.id AND target_ref_kind='new' AND target_logical_fact_id IS NOT NULL)
    ) THEN RAISE EXCEPTION 'fact_reconciliation_pending_graph_invalid'; END IF;
    IF current_lifecycle_status='applied' AND (
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
  IF current_lifecycle_status='applied' AND EXISTS (
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

CREATE OR REPLACE FUNCTION fn_fact_reconciliation_chain_local() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_row jsonb := to_jsonb(OLD);
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
      AND next.supersedes_decision_id=(old_row->>'evolution_decision_id')::uuid
        AND next.lifecycle_status='cancelled'
  ) THEN RAISE EXCEPTION 'fact_reconciliation_broken_source_chain'; END IF;
  IF TG_TABLE_NAME='fact_reconciliation_target_slots' AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_target_slots next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_target_slot_id=OLD.id
  ) AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_decisions next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_decision_id=(old_row->>'evolution_decision_id')::uuid
        AND next.lifecycle_status='cancelled'
  ) THEN RAISE EXCEPTION 'fact_reconciliation_broken_target_slot_chain'; END IF;
  IF TG_TABLE_NAME='fact_reconciliation_source_target_edges' AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_source_target_edges next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_source_target_edge_id=OLD.id
  ) AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_decisions next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_decision_id=(old_row->>'evolution_decision_id')::uuid
        AND next.lifecycle_status='cancelled'
  ) THEN RAISE EXCEPTION 'fact_reconciliation_broken_edge_chain'; END IF;
  IF TG_TABLE_NAME='fact_reconciliation_assertion_assignments' AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_assertion_assignments next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_assignment_id=OLD.id
  ) AND NOT EXISTS (
    SELECT 1 FROM fact_reconciliation_decisions next
    WHERE next.library_id=OLD.library_id AND next.command_id=OLD.command_id
      AND next.supersedes_decision_id=(old_row->>'evolution_decision_id')::uuid
        AND next.lifecycle_status='cancelled'
  ) THEN RAISE EXCEPTION 'fact_reconciliation_broken_assignment_chain'; END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION fn_fact_reconciliation_logical_fact_pair() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE slot_row fact_reconciliation_target_slots%ROWTYPE;
DECLARE fact_row logical_facts%ROWTYPE;
DECLARE new_row jsonb;
BEGIN
  IF TG_OP='DELETE' THEN RETURN NULL; END IF;
  new_row := to_jsonb(NEW);
  IF TG_TABLE_NAME='logical_facts' THEN
    IF new_row->>'reconciliation_target_slot_id' IS NOT NULL THEN
      SELECT * INTO slot_row FROM fact_reconciliation_target_slots
      WHERE id=(new_row->>'reconciliation_target_slot_id')::uuid
        AND library_id=(new_row->>'library_id')::uuid;
      IF NOT FOUND OR slot_row.target_ref_kind<>'new'
         OR slot_row.planned_target_logical_fact_id<>(new_row->>'id')::uuid
         OR slot_row.target_logical_fact_id<>(new_row->>'id')::uuid
         OR slot_row.target_identity_fingerprint<>new_row->>'identity_fingerprint'
         OR (slot_row.target_spec_snapshot->>'stable_predicate_identity_id')
              IS DISTINCT FROM new_row->>'stable_predicate_identity_id'
         OR (slot_row.target_spec_snapshot->>'subject_canonical_entity_id')
              IS DISTINCT FROM new_row->>'subject_canonical_entity_id'
         OR (slot_row.target_spec_snapshot->>'object_kind') IS DISTINCT FROM new_row->>'object_kind'
         OR (slot_row.target_spec_snapshot->>'object_canonical_entity_id')
              IS DISTINCT FROM new_row->>'object_canonical_entity_id'
         OR (slot_row.target_spec_snapshot->'object_value')
              IS DISTINCT FROM COALESCE(new_row->'object_value', 'null'::jsonb)
         OR (slot_row.target_spec_snapshot->'identity_qualifiers')
              IS DISTINCT FROM new_row->'identity_qualifiers'
         OR (slot_row.target_spec_snapshot->>'temporal_identity_key')
              IS DISTINCT FROM new_row->>'temporal_identity_key'
         OR (slot_row.target_spec_snapshot->>'identity_policy_version')
              IS DISTINCT FROM new_row->>'identity_policy_version'
      THEN RAISE EXCEPTION 'fact_reconciliation_target_pair_mismatch'; END IF;
    END IF;
  ELSE
    IF TG_OP='DELETE' THEN RETURN NULL; END IF;
    IF new_row->>'target_ref_kind'='new' AND new_row->>'target_logical_fact_id' IS NOT NULL THEN
      SELECT * INTO fact_row FROM logical_facts
      WHERE id=(new_row->>'target_logical_fact_id')::uuid
        AND library_id=(new_row->>'library_id')::uuid;
      IF NOT FOUND OR fact_row.reconciliation_target_slot_id<>(new_row->>'id')::uuid THEN
        RAISE EXCEPTION 'fact_reconciliation_target_pair_mismatch';
      END IF;
    END IF;
  END IF;
  RETURN NULL;
END $$;
"""
    )


def _restore_0074_trigger_functions() -> None:
    """Restore the exact 0074 trigger bodies for an Alembic round trip."""

    op.execute(
        """
CREATE OR REPLACE FUNCTION fn_fact_reconciliation_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE OR REPLACE FUNCTION fn_fact_reconciliation_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE OR REPLACE FUNCTION fn_fact_reconciliation_decision_graph() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE OR REPLACE FUNCTION fn_fact_reconciliation_chain_local() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE OR REPLACE FUNCTION fn_fact_reconciliation_logical_fact_pair() RETURNS trigger LANGUAGE plpgsql AS $$
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
"""
    )


def upgrade() -> None:
    _set_timeouts()
    op.execute(_LOCK_SQL)
    _assert_preconditions()
    _install_safe_trigger_functions()


def downgrade() -> None:
    _set_timeouts()
    op.execute(_LOCK_SQL)
    _assert_preconditions()
    _restore_0074_trigger_functions()
