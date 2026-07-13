from __future__ import annotations

import io
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_occurrences import GraphEntityOccurrence, GraphRelationOccurrence
from app.models.graph_review import GraphEntityMergeCandidate, GraphExtractionConflict


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0022_v04_m3_candidate_staging.py"
M3_TABLES = (
    GraphEntityCandidate.__table__,
    GraphRelationCandidate.__table__,
    GraphEntityOccurrence.__table__,
    GraphRelationOccurrence.__table__,
    GraphEntityCandidateEvidence.__table__,
    GraphRelationCandidateEvidence.__table__,
    GraphEntityMergeCandidate.__table__,
    GraphExtractionConflict.__table__,
)


def _offline(command_name: str, revision: str) -> str:
    output = io.StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    if command_name == "upgrade":
        command.upgrade(config, revision, sql=True)
    else:
        command.downgrade(config, revision, sql=True)
    return output.getvalue().lower()


def test_0022_is_the_single_alembic_head_and_has_exact_parent():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["0022"]
    migration = script.get_revision("0022")
    assert migration is not None
    assert migration.down_revision == "0021"
    assert Path(migration.path).resolve() == MIGRATION.resolve()
    assert not list((ROOT / "alembic" / "versions").glob("0023_*.py"))


def test_offline_upgrade_contains_every_orm_table_column_constraint_fk_and_index():
    sql = _offline("upgrade", "0021:0022")

    for table in M3_TABLES:
        assert f"create table {table.name}" in sql
        table_sql = sql.split(f"create table {table.name}", 1)[1].split(";", 1)[0]
        for column in table.c:
            assert re.search(rf"\b{re.escape(column.name.lower())}\b", table_sql)
        for constraint in table.constraints:
            if constraint.name:
                assert constraint.name.lower() in table_sql
        for foreign_key in table.foreign_key_constraints:
            assert foreign_key.name.lower() in table_sql
            assert f"on delete {foreign_key.ondelete.lower()}" in table_sql
        for index in table.indexes:
            assert index.name.lower() in sql

    for name in (
        "fk_entities_created_by_job",
        "fk_entity_mentions_created_by_job",
        "fk_knowledge_relations_created_by_job",
        "fk_relation_evidence_created_by_job",
        "uq_entity_mentions_extraction_key",
        "uq_knowledge_relations_extraction_key",
    ):
        assert name in sql
    assert sql.count("where extraction_key is not null") == 2


def test_offline_downgrade_removes_m3_without_formal_graph_dml():
    upgrade_sql = _offline("upgrade", "0021:0022")
    downgrade_sql = _offline("downgrade", "0022:0021")

    for table in reversed(M3_TABLES):
        assert f"drop table {table.name}" in downgrade_sql
    for table, column in (
        ("entities", "created_by_job_id"),
        ("entity_mentions", "created_by_job_id"),
        ("entity_mentions", "extraction_key"),
        ("knowledge_relations", "created_by_job_id"),
        ("knowledge_relations", "extraction_key"),
        ("relation_evidence", "created_by_job_id"),
    ):
        assert f"alter table {table} drop column {column}" in downgrade_sql

    combined = upgrade_sql + downgrade_sql
    assert "0023" not in combined
    assert not re.search(
        r"\b(?:insert\s+into|update|delete\s+from)\s+"
        r"(?:entities|entity_mentions|knowledge_relations|relation_evidence)\b",
        combined,
    )
