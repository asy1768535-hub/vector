"""Static contracts for the maintenance-only P3.1 corrective migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_0072_is_forward_only_and_fail_closed_for_both_audit_shapes():
    root = Path(__file__).parents[1]
    path = root / "alembic" / "versions" / "0072_canonical_entity_evolution_correction.py"
    spec = importlib.util.spec_from_file_location("migration_0072", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = path.read_text(encoding="utf-8")

    assert module.revision == "0072"
    assert module.down_revision == "0071"
    assert "P3_1_0072_NONEMPTY_LEGACY_AUDIT" in source
    assert "P3_1_0072_NONEMPTY_TARGET_AUDIT" in source
    assert "P3_1_0072_MAINTENANCE_LOCK_UNAVAILABLE" in source
    for table in (
        "sys_libraries",
        "canonical_entities",
        "entities",
        "entity_resolution_decisions",
        "canonical_entity_evolution_commands",
        "canonical_entity_evolution_decisions",
        "canonical_entity_evolution_sources",
        "canonical_entity_evolution_successors",
        "canonical_entity_projection_assignments",
    ):
        assert table in source


def test_0072_declares_target_lineage_and_immutable_audit_triggers():
    root = Path(__file__).parents[1]
    source = (
        root / "alembic" / "versions" / "0072_canonical_entity_evolution_correction.py"
    ).read_text(encoding="utf-8")

    for column in (
        "command_identity_fingerprint",
        "decision_payload_fingerprint",
        "evaluated_outcome",
        "supersedes_source_transition_id",
        "target_ref_kind",
        "target_successor_id",
        "supersedes_assignment_id",
        "evolution_assignment_id",
    ):
        assert column in source
    assert "CREATE CONSTRAINT TRIGGER" in source
    assert "canonical_entity_evolution_append_only" in source
    assert "canonical_entity_evolution_chain" in source
