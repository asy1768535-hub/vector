from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint


def _constraint_names(table) -> set[str]:
    return {item.name for item in table.constraints if item.name}


def _index_names(table) -> set[str]:
    return {item.name for item in table.indexes if item.name}


def _check_sql(table, name: str) -> str:
    for item in table.constraints:
        if isinstance(item, CheckConstraint) and item.name == name:
            return str(item.sqltext).lower()
    raise AssertionError(f"missing check constraint {name}")


def _unique_columns(table, name: str) -> tuple[str, ...]:
    for item in table.constraints:
        if isinstance(item, UniqueConstraint) and item.name == name:
            return tuple(column.name for column in item.columns)
    raise AssertionError(f"missing unique constraint {name}")


def _fk(table, column_name: str):
    foreign_keys = list(table.c[column_name].foreign_keys)
    assert len(foreign_keys) == 1
    return foreign_keys[0]


def _assert_fk(table, column: str, target: str, name: str, ondelete: str) -> None:
    foreign_key = _fk(table, column)
    assert foreign_key.target_fullname == target
    assert foreign_key.constraint.name == name
    assert foreign_key.ondelete == ondelete


def _assert_confidence_check(table, name: str, columns: tuple[str, ...]) -> None:
    sql = _check_sql(table, name)
    for column in columns:
        assert f"{column} is null" in sql
        assert f"{column} >= 0" in sql
        assert f"{column} <= 1" in sql


def test_candidate_tables_fields_constraints_indexes_and_foreign_keys():
    from app.models.graph_candidates import (
        GraphEntityCandidate,
        GraphRelationCandidate,
    )

    entity = GraphEntityCandidate.__table__
    relation = GraphRelationCandidate.__table__
    assert entity.name == "graph_entity_candidates"
    assert relation.name == "graph_relation_candidates"

    assert set(entity.c.keys()) == {
        "id",
        "job_id",
        "library_id",
        "ontology_version_id",
        "entity_type_key",
        "canonical_name",
        "normalized_name",
        "proposed_aliases",
        "proposed_properties",
        "external_mapping_hints",
        "candidate_key",
        "matched_entity_id",
        "materialized_entity_id",
        "normalization_method",
        "model_confidence",
        "evidence_quality_score",
        "schema_validation_score",
        "final_confidence",
        "status",
        "review_reason",
        "validation_errors",
        "created_at",
        "updated_at",
        "purged_at",
    }
    assert set(relation.c.keys()) == {
        "id",
        "job_id",
        "library_id",
        "ontology_version_id",
        "source_candidate_id",
        "relation_type_key",
        "target_candidate_id",
        "proposed_properties",
        "candidate_key",
        "matched_relation_id",
        "materialized_relation_id",
        "evidence_support_mode",
        "model_confidence",
        "evidence_quality_score",
        "schema_validation_score",
        "normalization_score",
        "final_confidence",
        "ontology_validation_status",
        "has_conflict",
        "status",
        "review_reason",
        "validation_errors",
        "created_at",
        "updated_at",
        "purged_at",
    }

    assert _unique_columns(entity, "uq_graph_entity_candidates_job_key") == (
        "job_id",
        "candidate_key",
    )
    assert _unique_columns(relation, "uq_graph_relation_candidates_job_key") == (
        "job_id",
        "candidate_key",
    )
    assert {
        "ck_graph_entity_candidates_status",
        "ck_graph_entity_candidates_confidence",
        "ck_graph_entity_candidates_payload_or_purged",
    } <= _constraint_names(entity)
    assert {
        "ck_graph_relation_candidates_status",
        "ck_graph_relation_candidates_support_mode",
        "ck_graph_relation_candidates_ontology_status",
        "ck_graph_relation_candidates_confidence",
        "ck_graph_relation_candidates_payload_or_purged",
    } <= _constraint_names(relation)
    assert {
        "ix_graph_entity_candidates_job_status_key",
        "ix_graph_entity_candidates_purge",
    } <= _index_names(entity)
    assert {
        "ix_graph_relation_candidates_job_status_key",
        "ix_graph_relation_candidates_source",
        "ix_graph_relation_candidates_target",
        "ix_graph_relation_candidates_purge",
    } <= _index_names(relation)

    entity_status = _check_sql(entity, "ck_graph_entity_candidates_status")
    relation_status = _check_sql(relation, "ck_graph_relation_candidates_status")
    for status in (
        "extracted",
        "aggregated",
        "validated",
        "pending_review",
        "rejected",
        "materialized",
        "superseded",
    ):
        assert status in entity_status
        assert status in relation_status
    _assert_confidence_check(
        entity,
        "ck_graph_entity_candidates_confidence",
        (
            "model_confidence",
            "evidence_quality_score",
            "schema_validation_score",
            "final_confidence",
        ),
    )
    _assert_confidence_check(
        relation,
        "ck_graph_relation_candidates_confidence",
        (
            "model_confidence",
            "evidence_quality_score",
            "schema_validation_score",
            "normalization_score",
            "final_confidence",
        ),
    )
    entity_purge = _check_sql(entity, "ck_graph_entity_candidates_payload_or_purged")
    relation_purge = _check_sql(
        relation, "ck_graph_relation_candidates_payload_or_purged"
    )
    for column in (
        "canonical_name",
        "normalized_name",
        "proposed_aliases",
        "proposed_properties",
        "external_mapping_hints",
        "review_reason",
        "validation_errors",
    ):
        assert f"{column} is null" in entity_purge
    for column in ("proposed_properties", "review_reason", "validation_errors"):
        assert f"{column} is null" in relation_purge
    assert "single_evidence" in _check_sql(
        relation, "ck_graph_relation_candidates_support_mode"
    )
    assert "evidence_group" in _check_sql(
        relation, "ck_graph_relation_candidates_support_mode"
    )
    ontology_status = _check_sql(
        relation, "ck_graph_relation_candidates_ontology_status"
    )
    for value in ("valid", "warning", "invalid", "boundary_unclear"):
        assert value in ontology_status

    _assert_fk(
        entity,
        "job_id",
        "graph_extraction_jobs.id",
        "fk_graph_entity_candidates_job",
        "CASCADE",
    )
    _assert_fk(
        entity,
        "library_id",
        "sys_libraries.id",
        "fk_graph_entity_candidates_library",
        "CASCADE",
    )
    _assert_fk(
        entity,
        "ontology_version_id",
        "ontology_versions.id",
        "fk_graph_entity_candidates_ontology",
        "RESTRICT",
    )
    _assert_fk(
        entity,
        "matched_entity_id",
        "entities.id",
        "fk_graph_entity_candidates_matched_entity",
        "SET NULL",
    )
    _assert_fk(
        entity,
        "materialized_entity_id",
        "entities.id",
        "fk_graph_entity_candidates_materialized_entity",
        "SET NULL",
    )
    for column, name in (
        ("job_id", "fk_graph_relation_candidates_job"),
        ("library_id", "fk_graph_relation_candidates_library"),
    ):
        target = "graph_extraction_jobs.id" if column == "job_id" else "sys_libraries.id"
        _assert_fk(relation, column, target, name, "CASCADE")
    _assert_fk(
        relation,
        "ontology_version_id",
        "ontology_versions.id",
        "fk_graph_relation_candidates_ontology",
        "RESTRICT",
    )
    for column, name in (
        ("source_candidate_id", "fk_graph_relation_candidates_source_candidate"),
        ("target_candidate_id", "fk_graph_relation_candidates_target_candidate"),
    ):
        _assert_fk(relation, column, "graph_entity_candidates.id", name, "CASCADE")
    for column, name in (
        ("matched_relation_id", "fk_graph_relation_candidates_matched_relation"),
        (
            "materialized_relation_id",
            "fk_graph_relation_candidates_materialized_relation",
        ),
    ):
        _assert_fk(relation, column, "knowledge_relations.id", name, "SET NULL")


def test_occurrence_tables_fields_constraints_indexes_and_foreign_keys():
    from app.models.graph_occurrences import (
        GraphEntityOccurrence,
        GraphRelationOccurrence,
    )

    entity = GraphEntityOccurrence.__table__
    relation = GraphRelationOccurrence.__table__
    assert entity.name == "graph_entity_occurrences"
    assert relation.name == "graph_relation_occurrences"
    assert set(entity.c.keys()) == {
        "id",
        "job_id",
        "extraction_unit_id",
        "local_ref",
        "entity_candidate_id",
        "model_confidence",
        "raw_payload",
        "created_at",
        "purged_at",
    }
    assert set(relation.c.keys()) == {
        "id",
        "job_id",
        "extraction_unit_id",
        "ordinal",
        "source_entity_occurrence_id",
        "target_entity_occurrence_id",
        "relation_candidate_id",
        "model_confidence",
        "raw_payload",
        "created_at",
        "purged_at",
    }
    assert _unique_columns(entity, "uq_graph_entity_occurrences_unit_ref") == (
        "extraction_unit_id",
        "local_ref",
    )
    assert _unique_columns(
        relation, "uq_graph_relation_occurrences_unit_ordinal"
    ) == ("extraction_unit_id", "ordinal")
    assert {
        "ck_graph_entity_occurrences_confidence",
        "ck_graph_entity_occurrences_payload_or_purged",
    } <= _constraint_names(entity)
    assert {
        "ck_graph_relation_occurrences_ordinal",
        "ck_graph_relation_occurrences_confidence",
        "ck_graph_relation_occurrences_payload_or_purged",
    } <= _constraint_names(relation)
    assert "ordinal >= 0" in _check_sql(
        relation, "ck_graph_relation_occurrences_ordinal"
    )
    for table, name in (
        (entity, "ck_graph_entity_occurrences_confidence"),
        (relation, "ck_graph_relation_occurrences_confidence"),
    ):
        sql = _check_sql(table, name)
        assert "model_confidence >= 0" in sql
        assert "model_confidence <= 1" in sql
    assert {
        "ix_graph_entity_occurrences_job_candidate",
        "ix_graph_entity_occurrences_purge",
    } <= _index_names(entity)
    assert {
        "ix_graph_relation_occurrences_job_candidate",
        "ix_graph_relation_occurrences_purge",
    } <= _index_names(relation)

    _assert_fk(
        entity,
        "job_id",
        "graph_extraction_jobs.id",
        "fk_graph_entity_occurrences_job",
        "CASCADE",
    )
    _assert_fk(
        entity,
        "extraction_unit_id",
        "graph_extraction_units.id",
        "fk_graph_entity_occurrences_unit",
        "CASCADE",
    )
    _assert_fk(
        entity,
        "entity_candidate_id",
        "graph_entity_candidates.id",
        "fk_graph_entity_occurrences_candidate",
        "CASCADE",
    )
    _assert_fk(
        relation,
        "job_id",
        "graph_extraction_jobs.id",
        "fk_graph_relation_occurrences_job",
        "CASCADE",
    )
    _assert_fk(
        relation,
        "extraction_unit_id",
        "graph_extraction_units.id",
        "fk_graph_relation_occurrences_unit",
        "CASCADE",
    )
    for column, name in (
        (
            "source_entity_occurrence_id",
            "fk_graph_relation_occurrences_source_occurrence",
        ),
        (
            "target_entity_occurrence_id",
            "fk_graph_relation_occurrences_target_occurrence",
        ),
    ):
        _assert_fk(relation, column, "graph_entity_occurrences.id", name, "CASCADE")
    _assert_fk(
        relation,
        "relation_candidate_id",
        "graph_relation_candidates.id",
        "fk_graph_relation_occurrences_candidate",
        "CASCADE",
    )


def _assert_candidate_evidence_table(table, prefix: str, candidate_target: str) -> None:
    assert set(table.c.keys()) == {
        "id",
        "job_id",
        "extraction_unit_id",
        "candidate_id",
        "claim_key",
        "context_ref",
        "quote_text",
        "quote_hash",
        "resolved_evidence_id",
        "resolved_document_id",
        "resolved_document_revision_id",
        "resolved_chunk_id",
        "resolved_block_id",
        "resolved_source_span",
        "candidate_matches",
        "evidence_type",
        "evidence_quality_score",
        "validation_status",
        "validation_error",
        "created_at",
        "purged_at",
    }
    assert _unique_columns(table, f"uq_{prefix}_claim") == (
        "candidate_id",
        "extraction_unit_id",
        "claim_key",
    )
    assert {
        f"ck_{prefix}_status",
        f"ck_{prefix}_type",
        f"ck_{prefix}_score",
        f"ck_{prefix}_resolution",
    } <= _constraint_names(table)
    assert {
        f"ix_{prefix}_job_status",
        f"ix_{prefix}_candidate",
        f"ix_{prefix}_unit",
        f"ix_{prefix}_purge",
    } <= _index_names(table)
    resolution = _check_sql(table, f"ck_{prefix}_resolution")
    for value in ("valid", "ambiguous", "invalid"):
        assert value in resolution
    for column in (
        "resolved_evidence_id",
        "resolved_document_id",
        "resolved_document_revision_id",
        "resolved_chunk_id",
        "resolved_source_span",
        "evidence_type",
        "evidence_quality_score",
    ):
        assert column in resolution
    assert "jsonb_array_length(candidate_matches) = 1" in resolution
    assert "jsonb_array_length(candidate_matches) >= 2" in resolution
    assert "candidate_matches = '[]'::jsonb" in resolution
    assert "purged_at is not null" in resolution
    assert "quote_text is null" in resolution

    _assert_fk(
        table,
        "job_id",
        "graph_extraction_jobs.id",
        f"fk_{prefix}_job",
        "CASCADE",
    )
    _assert_fk(
        table,
        "extraction_unit_id",
        "graph_extraction_units.id",
        f"fk_{prefix}_unit",
        "CASCADE",
    )
    _assert_fk(
        table,
        "candidate_id",
        candidate_target,
        f"fk_{prefix}_candidate",
        "CASCADE",
    )
    for column, target, suffix in (
        ("resolved_evidence_id", "evidence_units.id", "evidence"),
        ("resolved_document_id", "documents.id", "document"),
        (
            "resolved_document_revision_id",
            "document_revisions.id",
            "revision",
        ),
        ("resolved_chunk_id", "chunks.id", "chunk"),
        ("resolved_block_id", "document_blocks.id", "block"),
    ):
        _assert_fk(table, column, target, f"fk_{prefix}_{suffix}", "RESTRICT")


def test_typed_candidate_evidence_tables_are_separate_and_strict():
    from app.models.graph_candidate_evidence import (
        GraphEntityCandidateEvidence,
        GraphRelationCandidateEvidence,
    )

    entity = GraphEntityCandidateEvidence.__table__
    relation = GraphRelationCandidateEvidence.__table__
    assert entity.name == "graph_entity_candidate_evidence"
    assert relation.name == "graph_relation_candidate_evidence"
    _assert_candidate_evidence_table(
        entity,
        "graph_entity_candidate_evidence",
        "graph_entity_candidates.id",
    )
    _assert_candidate_evidence_table(
        relation,
        "graph_relation_candidate_evidence",
        "graph_relation_candidates.id",
    )
    assert "candidate_type" not in entity.c
    assert "candidate_type" not in relation.c


def test_review_tables_fields_constraints_indexes_and_foreign_keys():
    from app.models.graph_review import (
        GraphEntityMergeCandidate,
        GraphExtractionConflict,
    )

    merge = GraphEntityMergeCandidate.__table__
    conflict = GraphExtractionConflict.__table__
    assert merge.name == "graph_entity_merge_candidates"
    assert conflict.name == "graph_extraction_conflicts"
    assert set(merge.c.keys()) == {
        "id",
        "job_id",
        "library_id",
        "entity_candidate_id",
        "suggested_target_entity_id",
        "merge_key",
        "reason",
        "status",
        "details",
        "description",
        "evidence",
        "created_at",
        "updated_at",
        "purged_at",
    }
    assert set(conflict.c.keys()) == {
        "id",
        "job_id",
        "library_id",
        "conflict_key",
        "conflict_type",
        "entity_candidate_ids",
        "relation_candidate_ids",
        "conflicting_fields",
        "status",
        "details",
        "description",
        "evidence",
        "created_at",
        "updated_at",
        "purged_at",
    }
    assert _unique_columns(
        merge, "uq_graph_entity_merge_candidates_job_key"
    ) == ("job_id", "merge_key")
    assert _unique_columns(conflict, "uq_graph_extraction_conflicts_job_key") == (
        "job_id",
        "conflict_key",
    )
    assert {
        "ck_graph_entity_merge_candidates_status",
        "ck_graph_entity_merge_candidates_payload_or_purged",
    } <= _constraint_names(merge)
    assert {
        "ck_graph_extraction_conflicts_status",
        "ck_graph_extraction_conflicts_members",
        "ck_graph_extraction_conflicts_payload_or_purged",
    } <= _constraint_names(conflict)
    assert "pending_review" in _check_sql(
        merge, "ck_graph_entity_merge_candidates_status"
    )
    assert "open" in _check_sql(conflict, "ck_graph_extraction_conflicts_status")
    members = _check_sql(conflict, "ck_graph_extraction_conflicts_members")
    assert "jsonb_array_length(entity_candidate_ids) > 0" in members
    assert "jsonb_array_length(relation_candidate_ids) > 0" in members
    assert {
        "ix_graph_entity_merge_candidates_job_status",
        "ix_graph_entity_merge_candidates_purge",
    } <= _index_names(merge)
    assert {
        "ix_graph_extraction_conflicts_job_status",
        "ix_graph_extraction_conflicts_purge",
    } <= _index_names(conflict)
    _assert_fk(
        merge,
        "job_id",
        "graph_extraction_jobs.id",
        "fk_graph_entity_merge_candidates_job",
        "CASCADE",
    )
    _assert_fk(
        merge,
        "library_id",
        "sys_libraries.id",
        "fk_graph_entity_merge_candidates_library",
        "CASCADE",
    )
    _assert_fk(
        merge,
        "entity_candidate_id",
        "graph_entity_candidates.id",
        "fk_graph_entity_merge_candidates_candidate",
        "CASCADE",
    )
    _assert_fk(
        merge,
        "suggested_target_entity_id",
        "entities.id",
        "fk_graph_entity_merge_candidates_target_entity",
        "SET NULL",
    )
    _assert_fk(
        conflict,
        "job_id",
        "graph_extraction_jobs.id",
        "fk_graph_extraction_conflicts_job",
        "CASCADE",
    )
    _assert_fk(
        conflict,
        "library_id",
        "sys_libraries.id",
        "fk_graph_extraction_conflicts_library",
        "CASCADE",
    )


def test_all_m3_models_are_exported_for_alembic_discovery():
    import app.models as models

    for name in (
        "GraphEntityCandidate",
        "GraphRelationCandidate",
        "GraphEntityOccurrence",
        "GraphRelationOccurrence",
        "GraphEntityCandidateEvidence",
        "GraphRelationCandidateEvidence",
        "GraphEntityMergeCandidate",
        "GraphExtractionConflict",
    ):
        assert getattr(models, name).__table__ is not None
