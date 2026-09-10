from __future__ import annotations

from pathlib import Path

from app.models.fact_foundation import StablePredicateMapping
from app.models.stable_predicate_evolution import (
    StablePredicateEvolutionCommand,
    StablePredicateEvolutionDecision,
    StablePredicateEvolutionSource,
    StablePredicateEvolutionSuccessor,
    StablePredicateMappingEvolutionAssignment,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0073_stable_predicate_evolution.py"


def _constraint_names(table) -> set[str]:
    return {constraint.name for constraint in table.constraints if constraint.name}


def test_predicate_evolution_orm_declares_frozen_tables_and_pairing() -> None:
    assert StablePredicateEvolutionCommand.__tablename__ == (
        "stable_predicate_evolution_commands"
    )
    assert StablePredicateEvolutionDecision.__tablename__ == (
        "stable_predicate_evolution_decisions"
    )
    assert StablePredicateEvolutionSource.__tablename__ == (
        "stable_predicate_evolution_sources"
    )
    assert StablePredicateEvolutionSuccessor.__tablename__ == (
        "stable_predicate_evolution_successors"
    )
    assignment = StablePredicateMappingEvolutionAssignment.__table__
    assert assignment.name == "stable_predicate_mapping_evolution_assignments"
    assert {
        "command_id",
        "evolution_decision_id",
        "source_transition_id",
        "old_mapping_id",
        "new_mapping_id",
        "supersedes_assignment_id",
    } <= set(assignment.c.keys())
    assert "evolution_assignment_id" in StablePredicateMapping.__table__.c
    assert "uq_stable_predicate_mappings_id_library" in _constraint_names(
        StablePredicateMapping.__table__
    )
    assert (
        "fk_sp_mapping_evolution_assignments_target_successor"
        in _constraint_names(assignment)
    )


def test_0073_is_transactional_fail_closed_and_uses_frozen_lock() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "0073"' in migration
    assert 'down_revision = "0072"' in migration
    assert "356131050375775214" in migration
    assert "pg_try_advisory_xact_lock" in migration
    assert "SET LOCAL lock_timeout = '5s'" in migration
    assert "SET LOCAL statement_timeout = '300s'" in migration
    assert "CREATE INDEX CONCURRENTLY" not in migration
    assert "autocommit_block" not in migration
    assert "COMMIT" not in migration
    assert "stable_predicate_mapping_evolution_assignments" in migration
    assert "ct_stable_predicate_evolution_decision_graph" in migration
    assert "P3_2_0073_NONEMPTY_AUDIT" in migration


def test_0073_installs_frozen_membership_and_aggregate_guards() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")

    assert "fn_stable_predicate_evolution_child_guard" in migration
    assert "trg_stable_predicate_evolution_child_guard" in migration
    for table in (
        "stable_predicate_evolution_sources",
        "stable_predicate_evolution_successors",
        "stable_predicate_mapping_evolution_assignments",
    ):
        assert f'"{table}",' in migration
    assert "BEFORE INSERT ON {table}" in migration

    required_integrity_markers = {
        "predicate_evolution_command_snapshot_mismatch",
        "predicate_evolution_decision_snapshot_mismatch",
        "predicate_evolution_decision_chain_invalid",
        "predicate_evolution_source_chain_invalid",
        "predicate_evolution_assignment_chain_invalid",
        "predicate_evolution_predecessor_lifecycle_invalid",
        "predicate_evolution_cancellation_predecessor_invalid",
        "predicate_evolution_lineage_cycle",
        "predicate_evolution_merge_graph_invalid",
        "predicate_evolution_split_graph_invalid",
        "predicate_evolution_reassign_graph_invalid",
        "predicate_evolution_target_spec_mismatch",
        "predicate_evolution_mapping_pair_mismatch",
        "predicate_evolution_historical_only_forbidden",
        "predicate_evolution_assignment_limit",
    }
    assert required_integrity_markers <= set(migration.split("'"))
    assert "WITH RECURSIVE" in migration
    assert "jsonb_array_length" in migration
    assert ">4096" in migration.replace(" ", "")
    assert "COALESCE(" in migration
    assert "NEW.requested_effect='cancel'" in migration
    assert "evolution_decision_id=NEW.supersedes_decision_id" in migration


def test_0073_has_schema_prechecks_and_final_catalog_assertions() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")

    assert "fn_p3_2_0073_assert_preconditions" in migration
    assert "fn_p3_2_0073_assert_catalog" in migration
    assert "P3_2_0073_PRECONDITION_FAILED" in migration
    assert "P3_2_0073_CATALOG_MISMATCH" in migration
    assert migration.index("fn_p3_2_0073_assert_preconditions") < migration.index(
        "_create_tables()"
    )
    assert migration.index("_install_triggers()") < migration.rindex(
        "fn_p3_2_0073_assert_catalog"
    )


def test_0073_downgrade_drops_every_new_trigger_and_function() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    downgrade = migration[migration.index("def downgrade()") :]

    assert "trg_stable_predicate_evolution_child_guard" in downgrade
    assert "fn_stable_predicate_evolution_child_guard()" in downgrade
    assert "fn_p3_2_0073_assert_catalog()" in downgrade
    assert downgrade.index("op.drop_table(table)") < downgrade.index(
        'op.drop_column("stable_predicate_mappings", "evolution_assignment_id")'
    )
