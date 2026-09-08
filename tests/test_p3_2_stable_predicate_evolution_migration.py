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
