from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import ForeignKeyConstraint


def _load_migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0064_canonical_entity_identity.py"
    spec = importlib.util.spec_from_file_location("migration_0064", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_canonical_entity_model_is_scoped_without_type_or_tenant_duplicates():
    from app.models.canonical_entity import CanonicalEntity

    table = CanonicalEntity.__table__
    assert set(table.c.keys()) == {
        "id",
        "library_id",
        "canonical_name",
        "normalized_name",
        "status",
        "created_at",
        "updated_at",
    }
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_canonical_entities_status",
        "uq_canonical_entities_id_library",
    }
    assert table.c.library_id.foreign_keys.pop().ondelete == "RESTRICT"


def test_entity_uses_nullable_scoped_canonical_projection():
    from app.models.entity import Entity

    column = Entity.__table__.c.canonical_entity_id
    assert column.nullable is True
    scoped_fk = next(
        constraint
        for constraint in Entity.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_entities_canonical_entity"
    )
    assert [column.name for column in scoped_fk.columns] == ["canonical_entity_id", "library_id"]
    assert [element.target_fullname for element in scoped_fk.elements] == [
        "canonical_entities.id",
        "canonical_entities.library_id",
    ]
    assert scoped_fk.ondelete == "RESTRICT"


def test_resolution_decision_model_has_append_only_outcomes_and_scope_indexes():
    from app.models.entity_resolution_decision import EntityResolutionDecision

    table = EntityResolutionDecision.__table__
    decision_kind_check = next(
        constraint
        for constraint in table.constraints
        if constraint.name == "ck_entity_resolution_decisions_kind"
    )
    decision_kind_sql = str(decision_kind_check.sqltext)
    assert all(value in decision_kind_sql for value in (
        "link_existing",
        "create_new",
        "pending_review",
        "rejected",
    ))
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_entity_resolution_decisions_fingerprints",
        "ck_entity_resolution_decisions_reason",
    }
    assert any(
        constraint.name == "uq_entity_resolution_decisions_library_fingerprint"
        for constraint in table.constraints
    )
    assert any(
        index.name == "uq_entity_resolution_decisions_library_subject_active" and index.unique
        for index in table.indexes
    )
    candidate_fk = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_entity_resolution_decisions_candidate"
    )
    assert candidate_fk.ondelete == "SET NULL"


def test_migration_0064_declares_backfill_and_revision_chain():
    migration = _load_migration()

    assert migration.revision == "0064"
    assert migration.down_revision == "0063"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "CREATE TEMP TABLE _p1_1_entity_canonical_backfill" in source
    assert "UPDATE entities AS entity" in source
    assert "canonical_entity_id" in source
