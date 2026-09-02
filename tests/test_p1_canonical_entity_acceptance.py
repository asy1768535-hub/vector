from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import uuid

from app.models.entity_resolution_decision import EntityResolutionDecision
from app.models.graph_candidates import GraphEntityCandidate
from app.services import graph_extraction_purge


ROOT = Path(__file__).parents[1]


class _Result:
    def __init__(self, rows=(), *, rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _PurgeDB:
    def __init__(self):
        self.statements = []
        self.results = [_Result([uuid.uuid4()])] + [_Result(rowcount=1) for _ in range(15)]

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)


def _load_migration(revision: str):
    path = ROOT / "alembic" / "versions" / f"{revision}_canonical_entity_identity.py"
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidate_purge_scrubs_payload_and_keeps_candidate_rows_and_decisions():
    db = _PurgeDB()
    asyncio.run(
        graph_extraction_purge.purge_document_graph_extraction_payloads(
            db,
            library_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
        )
    )

    sql = "\n".join(str(statement).lower() for statement in db.statements)
    assert "update graph_entity_candidates" in sql
    assert "delete from graph_entity_candidates" not in sql
    assert "entity_resolution_decisions" not in sql
    assert "canonical_entities" not in sql
    candidate_sql = next(
        str(statement).lower()
        for statement in db.statements
        if "update graph_entity_candidates" in str(statement).lower()
    )
    assert "canonical_name" in candidate_sql
    assert "normalized_name" in candidate_sql
    assert "purged_at" in candidate_sql


def test_candidate_physical_delete_fk_is_set_null_contract_only():
    decision_fk = next(
        constraint
        for constraint in EntityResolutionDecision.__table__.constraints
        if getattr(constraint, "name", None) == "fk_entity_resolution_decisions_candidate"
    )
    assert decision_fk.ondelete == "SET NULL"

    migration_source = (_load_migration("0064").__file__)
    source = Path(migration_source).read_text(encoding="utf-8")
    assert 'name="fk_entity_resolution_decisions_candidate"' in source
    assert 'ondelete="SET NULL"' in source


def test_historical_backfill_is_one_to_one_and_status_snapshot_is_conservative():
    source = Path(_load_migration("0064").__file__).read_text(encoding="utf-8")
    assert "SELECT id, gen_random_uuid()" in source
    assert "CREATE TEMP TABLE _p1_1_entity_canonical_backfill" in source
    assert "UPDATE entities AS entity" in source
    assert "WHEN entity.status = 'pending_review' THEN 'pending_review'" in source
    assert "WHEN entity.status IN ('rejected', 'stale', 'disabled', 'deleted') THEN 'disabled'" in source
    assert "ELSE 'active'" in source
    assert "GROUP BY" not in source.upper()


def test_decision_candidate_fk_remains_nullable_for_audit_snapshots():
    column = EntityResolutionDecision.__table__.c.graph_entity_candidate_id
    assert column.nullable is True
    assert "candidate_snapshot" in EntityResolutionDecision.__table__.c
    assert "identifier_snapshot" in EntityResolutionDecision.__table__.c
    assert "evidence_refs" in EntityResolutionDecision.__table__.c
    assert GraphEntityCandidate.__table__.c.purged_at.nullable is True
