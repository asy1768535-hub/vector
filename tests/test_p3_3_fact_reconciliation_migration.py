"""Schema contracts for the P3.3-A Fact reconciliation foundation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models.fact_foundation import LogicalFact
from app.models.fact_reconciliation import (
    FactReconciliationAssertionAssignment,
    FactReconciliationCommand,
    FactReconciliationDecision,
    FactReconciliationSource,
    FactReconciliationSourceTargetEdge,
    FactReconciliationTargetSlot,
)


def test_p3_3_orm_exposes_the_frozen_lineage_tables_and_fact_pointer() -> None:
    assert [
        FactReconciliationCommand.__tablename__,
        FactReconciliationDecision.__tablename__,
        FactReconciliationSource.__tablename__,
        FactReconciliationTargetSlot.__tablename__,
        FactReconciliationSourceTargetEdge.__tablename__,
        FactReconciliationAssertionAssignment.__tablename__,
    ] == [
        "fact_reconciliation_commands",
        "fact_reconciliation_decisions",
        "fact_reconciliation_sources",
        "fact_reconciliation_target_slots",
        "fact_reconciliation_source_target_edges",
        "fact_reconciliation_assertion_assignments",
    ]
    pointer = LogicalFact.__table__.c.reconciliation_target_slot_id
    assert pointer.nullable
    assert pointer.foreign_keys


def test_0074_is_a_maintenance_only_reconciliation_migration() -> None:
    root = Path(__file__).parents[1]
    path = root / "alembic" / "versions" / "0074_fact_reconciliation.py"
    spec = importlib.util.spec_from_file_location("migration_0074", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = path.read_text(encoding="utf-8")

    assert module.revision == "0074"
    assert module.down_revision == "0073"
    assert "3410968108221770891" in source
    assert "migration_busy" in source
    assert "CREATE CONSTRAINT TRIGGER" in source
    assert "P3_3_0074_NONEMPTY_AUDIT" in source
    for table in (
        "fact_reconciliation_commands",
        "fact_reconciliation_decisions",
        "fact_reconciliation_sources",
        "fact_reconciliation_target_slots",
        "fact_reconciliation_source_target_edges",
        "fact_reconciliation_assertion_assignments",
    ):
        assert table in source
