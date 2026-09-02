from __future__ import annotations

import io
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint

from app.models.fact_foundation import (
    FactAssertion,
    FactResolutionDecision,
    LogicalFact,
    StablePredicateIdentity,
    StablePredicateMapping,
)
from app.models.knowledge_relation import KnowledgeRelation
from app.models.relation_evidence import RelationEvidence


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0066_p2_2_fact_foundation.py"
MIGRATION_0069 = ROOT / "alembic" / "versions" / "0069_fact_resolution_active_decisions.py"


def _unique_names(table) -> set[str]:
    return {c.name for c in table.constraints if isinstance(c, UniqueConstraint) and c.name}


def _check_names(table) -> set[str]:
    return {c.name for c in table.constraints if isinstance(c, CheckConstraint) and c.name}


def _fk(table, name: str) -> ForeignKeyConstraint:
    return next(c for c in table.foreign_key_constraints if c.name == name)


def _offline(revision: str = "0065:0066") -> str:
    output = io.StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    command.upgrade(config, revision, sql=True)
    return output.getvalue().lower()


def test_stable_predicate_identity_and_mapping_contracts():
    identity = StablePredicateIdentity.__table__
    mapping = StablePredicateMapping.__table__

    assert {
        "id",
        "library_id",
        "namespace",
        "key",
        "contract_version",
        "temporal_class",
        "identity_policy_version",
        "resolution_status",
        "created_at",
        "updated_at",
    } <= set(identity.c.keys())
    assert ("library_id", "namespace", "key", "contract_version") in {
        tuple(c.columns.keys()) for c in identity.constraints if isinstance(c, UniqueConstraint)
    }
    assert "uq_stable_predicate_mappings_library_relation_type_active" in {
        i.name for i in mapping.indexes if isinstance(i, Index)
    }
    active = next(i for i in mapping.indexes if i.name == "uq_stable_predicate_mappings_library_relation_type_active")
    assert active.unique is True
    assert str(active.dialect_options["postgresql"]["where"]) == "mapping_status = 'active'"
    assert _fk(mapping, "fk_stable_predicate_mappings_identity").ondelete == "RESTRICT"
    assert _fk(mapping, "fk_stable_predicate_mappings_relation_type").ondelete == "RESTRICT"


def test_logical_fact_identity_is_indexed_but_not_semantically_unique():
    table = LogicalFact.__table__
    assert "uq_logical_facts_id_library" in _unique_names(table)
    fingerprint_indexes = [i for i in table.indexes if "fingerprint" in i.name]
    assert len(fingerprint_indexes) == 1
    assert fingerprint_indexes[0].unique is False
    assert "ck_logical_facts_status" in _check_names(table)
    assert _fk(table, "fk_logical_facts_stable_predicate").ondelete == "RESTRICT"
    assert _fk(table, "fk_logical_facts_subject_canonical").ondelete == "RESTRICT"


def test_assertion_and_decision_status_and_nullable_source_contracts():
    assertion = FactAssertion.__table__
    decision = FactResolutionDecision.__table__

    assert assertion.c.knowledge_relation_id.nullable is True
    assert assertion.c.asserted_value.nullable is True
    assert {"polarity", "modality", "status"} <= {name.removeprefix("ck_fact_assertions_") for name in _check_names(assertion)}
    assert _fk(assertion, "fk_fact_assertions_knowledge_relation").ondelete == "SET NULL"
    assert _fk(assertion, "fk_fact_assertions_logical_fact").ondelete == "RESTRICT"
    assert decision.c.graph_relation_candidate_id.nullable is True
    assert decision.c.raw_claim_id.nullable is True
    assert decision.c.source_snapshot.nullable is False
    assert decision.c.candidate_snapshot.nullable is False
    assert decision.c.evidence_refs.nullable is False
    assert _fk(decision, "fk_fact_resolution_decisions_candidate").ondelete == "SET NULL"
    assert _fk(decision, "fk_fact_resolution_decisions_raw_claim").ondelete == "SET NULL"
    assert _fk(decision, "fk_fact_resolution_decisions_assertion").ondelete == "SET NULL"


def test_0069_keeps_fact_resolution_decision_uniqueness_current_only():
    decision = FactResolutionDecision.__table__
    indexes = {index.name: index for index in decision.indexes}

    fingerprint = indexes["uq_fact_resolution_decisions_library_fingerprint_active"]
    subject = indexes["uq_fact_resolution_decisions_library_subject_active"]
    assert fingerprint.unique is True
    assert subject.unique is True
    assert tuple(fingerprint.columns.keys()) == ("library_id", "decision_fingerprint")
    assert tuple(subject.columns.keys()) == ("library_id", "source_kind", "subject_fingerprint")
    assert "superseded" in str(fingerprint.dialect_options["postgresql"]["where"])
    assert "superseded" in str(subject.dialect_options["postgresql"]["where"])
    assert "uq_fact_resolution_decisions_library_fingerprint" not in _unique_names(decision)

    migration = MIGRATION_0069.read_text(encoding="utf-8")
    offline_sql = _offline("0068:0069")
    assert 'revision: str = "0069"' in migration
    assert 'down_revision: Union[str, None] = "0068"' in migration
    assert "uq_fact_resolution_decisions_library_fingerprint_active" in offline_sql
    assert "uq_fact_resolution_decisions_library_subject_active" in offline_sql
    assert "where status <> 'superseded'" in offline_sql


def test_projection_evidence_bridges_are_nullable_and_preserve_old_owners():
    relation = KnowledgeRelation.__table__
    evidence = RelationEvidence.__table__

    assert relation.c.logical_fact_id.nullable is True
    assert evidence.c.fact_assertion_id.nullable is True
    assert any(fk.target_fullname == "knowledge_relations.id" for fk in evidence.c.relation_id.foreign_keys)
    assert _fk(relation, "fk_knowledge_relations_logical_fact").ondelete == "RESTRICT"
    assert _fk(evidence, "fk_relation_evidence_fact_assertion").ondelete == "SET NULL"
    assert "uq_knowledge_relations_id_library" in _unique_names(relation)
    assert "uq_relation_evidence_id_library" in _unique_names(evidence)


def test_0066_offline_sql_contains_parent_keys_and_conservative_backfill():
    sql = _offline()
    migration = MIGRATION.read_text(encoding="utf-8").lower()

    for name in (
        "uq_relation_types_id_library",
        "uq_knowledge_relations_id_library",
        "uq_relation_evidence_id_library",
        "uq_stable_predicate_mappings_library_relation_type_active",
        "fk_fact_assertions_knowledge_relation",
        "fk_relation_evidence_fact_assertion",
        "fk_fact_resolution_decisions_candidate",
    ):
        assert name in sql

    assert "create table stable_predicate_identities" in sql
    assert "create table stable_predicate_mappings" in sql
    assert "create table logical_facts" in sql
    assert "create table fact_assertions" in sql
    assert "create table fact_resolution_decisions" in sql
    assert "insert into stable_predicate_identities" in migration
    assert "insert into stable_predicate_mappings" in migration
    assert "insert into logical_facts" in migration
    assert "insert into fact_assertions" in migration
    assert "group by" not in migration
    assert "insert into fact_resolution_decisions" not in migration
    assert "canonical_entity_id is not null" in migration
    assert "|| 'null'" not in sql
