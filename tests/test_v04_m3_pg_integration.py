from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url

from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_occurrences import GraphEntityOccurrence, GraphRelationOccurrence
from app.models.graph_review import GraphEntityMergeCandidate, GraphExtractionConflict


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL admin connection",
)
_ADMIN_URL = make_url(_DSN).set(drivername="postgresql+asyncpg") if _DSN else None
_M3_TABLES = (
    GraphEntityCandidate.__table__,
    GraphRelationCandidate.__table__,
    GraphEntityOccurrence.__table__,
    GraphRelationOccurrence.__table__,
    GraphEntityCandidateEvidence.__table__,
    GraphRelationCandidateEvidence.__table__,
    GraphEntityMergeCandidate.__table__,
    GraphExtractionConflict.__table__,
)


def _admin_url() -> URL:
    assert _ADMIN_URL is not None
    return _ADMIN_URL


def _database_url(name: str) -> URL:
    return _admin_url().set(database=name)


async def _connect(url: URL):
    import asyncpg

    return await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database or "postgres",
    )


async def _admin(sql: str) -> None:
    connection = await _connect(_admin_url())
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


def _configure_alembic(monkeypatch, name: str) -> None:
    from app.config import settings

    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


async def _schema_snapshot(name: str) -> dict[str, set[tuple[str, ...]]]:
    connection = await _connect(_database_url(name))
    try:
        tables = await connection.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        columns = await connection.fetch(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema = 'public'"
        )
        constraints = await connection.fetch(
            "SELECT c.relname AS table_name, p.conname, p.contype::text "
            "FROM pg_constraint p JOIN pg_class c ON c.oid = p.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public'"
        )
        indexes = await connection.fetch(
            "SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'public'"
        )
        return {
            "tables": {(row["table_name"],) for row in tables},
            "columns": {
                (
                    row["table_name"],
                    row["column_name"],
                    row["data_type"],
                    row["is_nullable"],
                )
                for row in columns
            },
            "constraints": {
                (row["table_name"], row["conname"], row["contype"])
                for row in constraints
            },
            "indexes": {
                (row["tablename"], row["indexname"]) for row in indexes
            },
        }
    finally:
        await connection.close()


async def _assert_upgraded_schema(name: str) -> None:
    connection = await _connect(_database_url(name))
    try:
        constraint_rows = await connection.fetch(
            "SELECT c.relname AS table_name, p.conname, "
            "pg_get_constraintdef(p.oid) AS definition "
            "FROM pg_constraint p JOIN pg_class c ON c.oid = p.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public'"
        )
        constraints = {
            (row["table_name"], row["conname"]): row["definition"].upper()
            for row in constraint_rows
        }
        index_rows = await connection.fetch(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public'"
        )
        indexes = {
            (row["tablename"], row["indexname"]): row["indexdef"].upper()
            for row in index_rows
        }
        column_rows = await connection.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
        columns = {(row["table_name"], row["column_name"]) for row in column_rows}

        for table in _M3_TABLES:
            for column in table.c:
                assert (table.name, column.name) in columns
            for constraint in table.constraints:
                if constraint.name:
                    assert (table.name, constraint.name) in constraints
            for foreign_key in table.foreign_key_constraints:
                definition = constraints[(table.name, foreign_key.name)]
                assert f"ON DELETE {foreign_key.ondelete}" in definition
            for index in table.indexes:
                assert (table.name, index.name) in indexes

        for table, fk_name in (
            ("entities", "fk_entities_created_by_job"),
            ("entity_mentions", "fk_entity_mentions_created_by_job"),
            ("knowledge_relations", "fk_knowledge_relations_created_by_job"),
            ("relation_evidence", "fk_relation_evidence_created_by_job"),
        ):
            assert "ON DELETE SET NULL" in constraints[(table, fk_name)]
        for table, index_name in (
            ("entity_mentions", "uq_entity_mentions_extraction_key"),
            ("knowledge_relations", "uq_knowledge_relations_extraction_key"),
        ):
            definition = indexes[(table, index_name)]
            assert "UNIQUE INDEX" in definition
            assert "WHERE (EXTRACTION_KEY IS NOT NULL)" in definition
    finally:
        await connection.close()


def _ids() -> dict[str, uuid.UUID]:
    names = (
        "library",
        "document",
        "revision",
        "block",
        "evidence",
        "chunk",
        "ontology",
        "entity_type",
        "relation_type",
        "source_entity",
        "target_entity",
        "deletable_entity",
        "formal_relation",
        "deletable_relation",
        "job",
        "unit",
        "entity_candidate_a",
        "entity_candidate_b",
        "relation_candidate",
        "entity_occurrence_a",
        "entity_occurrence_b",
        "relation_occurrence",
        "valid_evidence",
        "ambiguous_evidence",
        "invalid_evidence",
        "relation_evidence_candidate",
        "merge",
        "conflict",
        "mention_a",
        "mention_b",
        "mention_keyed",
        "formal_relation_keyed",
        "formal_relation_evidence",
    )
    return {name: uuid.uuid4() for name in names}


async def _seed(name: str) -> dict[str, uuid.UUID]:
    connection = await _connect(_database_url(name))
    ids = _ids()
    try:
        await connection.execute(
            "INSERT INTO sys_libraries "
            "(id, slug, name, embedding_model, embedding_dim, vector_distance, "
            "chunk_size, chunk_overlap, retrieval_mode, qdrant_collection) "
            "VALUES ($1, 'm3-pg', 'M3 PostgreSQL', 'bge-m3', 1024, 'cosine', "
            "1000, 120, 'dense', 'm3_pg')",
            ids["library"],
        )
        await connection.execute(
            "INSERT INTO documents "
            "(id, library_id, title, content_hash, current_revision, status) "
            "VALUES ($1, $2, 'Policy', $3, 1, 'ready')",
            ids["document"],
            ids["library"],
            "1" * 64,
        )
        await connection.execute(
            "INSERT INTO document_revisions "
            "(id, document_id, library_id, revision_no, title, content_hash, "
            "normalized_text, parser_name, parser_version, chunking_strategy, "
            "chunking_strategy_version, status) "
            "VALUES ($1, $2, $3, 1, 'Policy', $4, $5, 'test', '1', 'test', '1', 'ready')",
            ids["revision"],
            ids["document"],
            ids["library"],
            "2" * 64,
            "HR approves onboarding.",
        )
        await connection.execute(
            "UPDATE documents SET current_revision_id=$1, latest_revision_id=$1 WHERE id=$2",
            ids["revision"],
            ids["document"],
        )
        await connection.execute(
            "INSERT INTO document_blocks "
            "(id, library_id, document_id, document_revision_id, seq, block_kind, "
            "text, parser_name, parser_version) "
            "VALUES ($1, $2, $3, $4, 0, 'paragraph', $5, 'test', '1')",
            ids["block"],
            ids["library"],
            ids["document"],
            ids["revision"],
            "HR approves onboarding.",
        )
        await connection.execute(
            "INSERT INTO evidence_units "
            "(id, library_id, document_id, document_revision_id, document_block_id, "
            "evidence_kind, source_start, source_end, text_quote, status) "
            "VALUES ($1, $2, $3, $4, $5, 'chunk', 0, 23, $6, 'active')",
            ids["evidence"],
            ids["library"],
            ids["document"],
            ids["revision"],
            ids["block"],
            "HR approves onboarding.",
        )
        await connection.execute(
            "INSERT INTO chunks "
            "(id, document_id, library_id, document_revision_id, block_id, evidence_id, "
            "seq, chunk_kind, text, token_count) "
            "VALUES ($1, $2, $3, $4, $5, $6, 0, 'text', $7, 4)",
            ids["chunk"],
            ids["document"],
            ids["library"],
            ids["revision"],
            ids["block"],
            ids["evidence"],
            "HR approves onboarding.",
        )
        await connection.execute(
            "INSERT INTO ontology_versions "
            "(id, library_id, version_key, version_no, status) "
            "VALUES ($1, $2, 'default', 1, 'active')",
            ids["ontology"],
            ids["library"],
        )
        await connection.execute(
            "INSERT INTO entity_types "
            "(id, library_id, ontology_version_id, key, label, is_seeded, status) "
            "VALUES ($1, $2, $3, 'department', 'Department', true, 'active')",
            ids["entity_type"],
            ids["library"],
            ids["ontology"],
        )
        await connection.execute(
            "INSERT INTO relation_types "
            "(id, library_id, ontology_version_id, key, label, direction, "
            "requires_evidence, default_review_policy, is_seeded, status) "
            "VALUES ($1, $2, $3, 'approves', 'Approves', 'directed', true, "
            "'auto_active', true, 'active')",
            ids["relation_type"],
            ids["library"],
            ids["ontology"],
        )
        for key, name_value in (
            ("source_entity", "HR"),
            ("target_entity", "Onboarding"),
            ("deletable_entity", "Temporary target"),
        ):
            await connection.execute(
                "INSERT INTO entities "
                "(id, library_id, ontology_version_id, entity_type_id, canonical_name, "
                "normalized_name, status, source_type) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'active', 'manual')",
                ids[key],
                ids["library"],
                ids["ontology"],
                ids["entity_type"],
                name_value,
                name_value.lower(),
            )
        for key in ("formal_relation", "deletable_relation"):
            await connection.execute(
                "INSERT INTO knowledge_relations "
                "(id, library_id, ontology_version_id, relation_type_id, "
                "source_entity_id, target_entity_id, status, source_type) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'draft', 'manual')",
                ids[key],
                ids["library"],
                ids["ontology"],
                ids["relation_type"],
                ids["source_entity"],
                ids["target_entity"],
            )

        await connection.execute(
            "INSERT INTO graph_extraction_jobs "
            "(id, library_id, document_id, document_revision_id, ontology_version_id, "
            "trigger_type, execution_mode, status, input_fingerprint, idempotency_key, "
            "retry_generation, model_provider, model_name, prompt_version, extractor_version, "
            "output_parser_version, context_policy_version, extraction_policy_version, "
            "normalization_rule_version, confidence_policy_version, document_parser_version, "
            "chunking_strategy_version, model_config_snapshot, policy_config_snapshot, "
            "model_config_hash, policy_config_hash, ontology_snapshot, ontology_snapshot_hash, "
            "prompt_content_hash) VALUES "
            "($1,$2,$3,$4,$5,'manual','production','processing',$6,'m3-pg-job',0,"
            "'dashscope','qwen-plus','v1','v1','v1','v1','v1','normalization-v1','v1',"
            "'1','1','{}'::jsonb,'{}'::jsonb,$7,$8,'{}'::jsonb,$9,$10)",
            ids["job"],
            ids["library"],
            ids["document"],
            ids["revision"],
            ids["ontology"],
            "3" * 64,
            "4" * 64,
            "5" * 64,
            "6" * 64,
            "7" * 64,
        )
        await connection.execute(
            "INSERT INTO graph_extraction_units "
            "(id, job_id, library_id, document_revision_id, ordinal, center_chunk_id, "
            "center_evidence_id, unit_fingerprint, status, model_attempt_count, retryable) "
            "VALUES ($1,$2,$3,$4,0,$5,$6,$7,'queued',0,false)",
            ids["unit"],
            ids["job"],
            ids["library"],
            ids["revision"],
            ids["chunk"],
            ids["evidence"],
            "8" * 64,
        )

        for key, candidate_key, canonical_name, matched in (
            ("entity_candidate_a", "a" * 64, "HR", ids["deletable_entity"]),
            ("entity_candidate_b", "b" * 64, "Onboarding", None),
        ):
            await connection.execute(
                "INSERT INTO graph_entity_candidates "
                "(id, job_id, library_id, ontology_version_id, entity_type_key, "
                "canonical_name, normalized_name, proposed_aliases, proposed_properties, "
                "external_mapping_hints, candidate_key, matched_entity_id, "
                "materialized_entity_id, status) "
                "VALUES ($1,$2,$3,$4,'department',$5,$6,'[]'::jsonb,'{}'::jsonb,"
                "'{}'::jsonb,$7,$8,$8,'extracted')",
                ids[key],
                ids["job"],
                ids["library"],
                ids["ontology"],
                canonical_name,
                canonical_name.lower(),
                candidate_key,
                matched,
            )
        await connection.execute(
            "INSERT INTO graph_relation_candidates "
            "(id, job_id, library_id, ontology_version_id, source_candidate_id, "
            "relation_type_key, target_candidate_id, proposed_properties, candidate_key, "
            "matched_relation_id, materialized_relation_id, evidence_support_mode, "
            "has_conflict, status) VALUES "
            "($1,$2,$3,$4,$5,'approves',$6,'{}'::jsonb,$7,$8,$8,'single_evidence',false,'extracted')",
            ids["relation_candidate"],
            ids["job"],
            ids["library"],
            ids["ontology"],
            ids["entity_candidate_a"],
            ids["entity_candidate_b"],
            "c" * 64,
            ids["deletable_relation"],
        )
        for key, local_ref, candidate in (
            ("entity_occurrence_a", "e1", "entity_candidate_a"),
            ("entity_occurrence_b", "e2", "entity_candidate_b"),
        ):
            await connection.execute(
                "INSERT INTO graph_entity_occurrences "
                "(id, job_id, extraction_unit_id, local_ref, entity_candidate_id, raw_payload) "
                "VALUES ($1,$2,$3,$4,$5,'{}'::jsonb)",
                ids[key],
                ids["job"],
                ids["unit"],
                local_ref,
                ids[candidate],
            )
        await connection.execute(
            "INSERT INTO graph_relation_occurrences "
            "(id, job_id, extraction_unit_id, ordinal, source_entity_occurrence_id, "
            "target_entity_occurrence_id, relation_candidate_id, raw_payload) "
            "VALUES ($1,$2,$3,0,$4,$5,$6,'{}'::jsonb)",
            ids["relation_occurrence"],
            ids["job"],
            ids["unit"],
            ids["entity_occurrence_a"],
            ids["entity_occurrence_b"],
            ids["relation_candidate"],
        )

        span = {"start": 0, "end": 2, "coordinate_system": "evidence_text_v1"}
        match = {
            "evidence_id": str(ids["evidence"]),
            "document_id": str(ids["document"]),
            "revision_id": str(ids["revision"]),
            "chunk_id": str(ids["chunk"]),
            "block_id": str(ids["block"]),
            "source_span": span,
        }
        valid_values = (
            ids["job"],
            ids["unit"],
            ids["entity_candidate_a"],
            ids["evidence"],
            ids["document"],
            ids["revision"],
            ids["chunk"],
            ids["block"],
            json.dumps(span),
            json.dumps([match]),
        )
        await connection.execute(
            "INSERT INTO graph_entity_candidate_evidence "
            "(id,job_id,extraction_unit_id,candidate_id,claim_key,context_ref,quote_text,quote_hash,"
            "resolved_evidence_id,resolved_document_id,resolved_document_revision_id,resolved_chunk_id,"
            "resolved_block_id,resolved_source_span,candidate_matches,evidence_type,evidence_quality_score,"
            "validation_status) VALUES "
            "($1,$2,$3,$4,$5,'c0','HR',$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,"
            "'direct_statement',1.0,'valid')",
            ids["valid_evidence"],
            *valid_values[:3],
            "d" * 64,
            "e" * 64,
            *valid_values[3:],
        )
        ambiguous_matches = [match, {**match, "source_span": {**span, "start": 3, "end": 5}}]
        await connection.execute(
            "INSERT INTO graph_entity_candidate_evidence "
            "(id,job_id,extraction_unit_id,candidate_id,claim_key,context_ref,quote_text,quote_hash,"
            "candidate_matches,validation_status,validation_error) VALUES "
            "($1,$2,$3,$4,$5,'c0','HR',$6,$7::jsonb,'ambiguous','multiple_quote_matches')",
            ids["ambiguous_evidence"],
            ids["job"],
            ids["unit"],
            ids["entity_candidate_a"],
            "f" * 64,
            "0" * 64,
            json.dumps(ambiguous_matches),
        )
        await connection.execute(
            "INSERT INTO graph_entity_candidate_evidence "
            "(id,job_id,extraction_unit_id,candidate_id,claim_key,context_ref,quote_text,quote_hash,"
            "candidate_matches,validation_status,validation_error) VALUES "
            "($1,$2,$3,$4,$5,'c0','missing',$6,'[]'::jsonb,'invalid','quote_not_found')",
            ids["invalid_evidence"],
            ids["job"],
            ids["unit"],
            ids["entity_candidate_a"],
            "1" * 64,
            "2" * 64,
        )
        await connection.execute(
            "INSERT INTO graph_relation_candidate_evidence "
            "(id,job_id,extraction_unit_id,candidate_id,claim_key,context_ref,quote_text,quote_hash,"
            "resolved_evidence_id,resolved_document_id,resolved_document_revision_id,resolved_chunk_id,"
            "resolved_block_id,resolved_source_span,candidate_matches,evidence_type,evidence_quality_score,"
            "validation_status) VALUES "
            "($1,$2,$3,$4,$5,'c0','HR',$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,"
            "'direct_statement',1.0,'valid')",
            ids["relation_evidence_candidate"],
            ids["job"],
            ids["unit"],
            ids["relation_candidate"],
            "3" * 64,
            "4" * 64,
            ids["evidence"],
            ids["document"],
            ids["revision"],
            ids["chunk"],
            ids["block"],
            json.dumps(span),
            json.dumps([match]),
        )
        await connection.execute(
            "INSERT INTO graph_entity_merge_candidates "
            "(id,job_id,library_id,entity_candidate_id,suggested_target_entity_id,merge_key,reason,"
            "status,details,description,evidence) VALUES "
            "($1,$2,$3,$4,$5,$6,'exact_alias','pending_review','{}'::jsonb,'review','[]'::jsonb)",
            ids["merge"],
            ids["job"],
            ids["library"],
            ids["entity_candidate_a"],
            ids["deletable_entity"],
            "5" * 64,
        )
        await connection.execute(
            "INSERT INTO graph_extraction_conflicts "
            "(id,job_id,library_id,conflict_key,conflict_type,entity_candidate_ids,"
            "relation_candidate_ids,conflicting_fields,status,details,description,evidence) VALUES "
            "($1,$2,$3,$4,'property_conflict',$5::jsonb,'[]'::jsonb,$6::jsonb,'open',"
            "'{}'::jsonb,'review','[]'::jsonb)",
            ids["conflict"],
            ids["job"],
            ids["library"],
            "6" * 64,
            json.dumps([str(ids["entity_candidate_a"])]),
            json.dumps([{"field": "owner", "value_hashes": ["7" * 64]}]),
        )

        for key in ("mention_a", "mention_b"):
            await connection.execute(
                "INSERT INTO entity_mentions "
                "(id,library_id,entity_id,evidence_id,document_id,document_revision_id,"
                "mention_text,source_type,status,created_by_job_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,'HR','extracted','active',$7)",
                ids[key],
                ids["library"],
                ids["source_entity"],
                ids["evidence"],
                ids["document"],
                ids["revision"],
                ids["job"],
            )
        await connection.execute(
            "INSERT INTO entity_mentions "
            "(id,library_id,entity_id,evidence_id,document_id,document_revision_id,"
            "mention_text,source_type,status,created_by_job_id,extraction_key) "
            "VALUES ($1,$2,$3,$4,$5,$6,'HR','extracted','active',$7,$8)",
            ids["mention_keyed"],
            ids["library"],
            ids["source_entity"],
            ids["evidence"],
            ids["document"],
            ids["revision"],
            ids["job"],
            "mention-key",
        )
        await connection.execute(
            "UPDATE entities SET created_by_job_id=$1 WHERE id=$2",
            ids["job"],
            ids["source_entity"],
        )
        await connection.execute(
            "UPDATE knowledge_relations SET created_by_job_id=$1, extraction_key='relation-key' "
            "WHERE id=$2",
            ids["job"],
            ids["formal_relation"],
        )
        await connection.execute(
            "INSERT INTO relation_evidence "
            "(id,library_id,relation_id,evidence_id,document_id,document_revision_id,"
            "support_type,status,created_by_job_id) "
            "VALUES ($1,$2,$3,$4,$5,$6,'supports','active',$7)",
            ids["formal_relation_evidence"],
            ids["library"],
            ids["formal_relation"],
            ids["evidence"],
            ids["document"],
            ids["revision"],
            ids["job"],
        )
        return ids
    finally:
        await connection.close()


async def _exercise_constraints(name: str, ids: dict[str, uuid.UUID]) -> None:
    import asyncpg

    connection = await _connect(_database_url(name))
    try:
        mismatched_match = {
            "evidence_id": str(uuid.uuid4()),
            "document_id": str(ids["document"]),
            "revision_id": str(ids["revision"]),
            "chunk_id": str(ids["chunk"]),
            "block_id": str(ids["block"]),
            "source_span": {
                "start": 0,
                "end": 2,
                "coordinate_system": "evidence_text_v1",
            },
        }
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE graph_relation_candidate_evidence "
                "SET candidate_matches=$1::jsonb WHERE id=$2",
                json.dumps([mismatched_match]),
                ids["relation_evidence_candidate"],
            )

        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                "INSERT INTO entity_mentions "
                "(id,library_id,entity_id,evidence_id,document_id,document_revision_id,"
                "mention_text,source_type,status,extraction_key) "
                "VALUES ($1,$2,$3,$4,$5,$6,'duplicate','extracted','active','mention-key')",
                uuid.uuid4(),
                ids["library"],
                ids["source_entity"],
                ids["evidence"],
                ids["document"],
                ids["revision"],
            )
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                "INSERT INTO knowledge_relations "
                "(id,library_id,ontology_version_id,relation_type_id,source_entity_id,"
                "target_entity_id,status,source_type,extraction_key) "
                "VALUES ($1,$2,$3,$4,$5,$6,'draft','extracted','relation-key')",
                ids["formal_relation_keyed"],
                ids["library"],
                ids["ontology"],
                ids["relation_type"],
                ids["source_entity"],
                ids["target_entity"],
            )

        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE graph_entity_candidates SET purged_at=now() WHERE id=$1",
                ids["entity_candidate_a"],
            )
        await connection.execute(
            "UPDATE graph_entity_candidates SET purged_at=now(),canonical_name=NULL,"
            "normalized_name=NULL,proposed_aliases=NULL,proposed_properties=NULL,"
            "external_mapping_hints=NULL,review_reason=NULL,validation_errors=NULL WHERE id=$1",
            ids["entity_candidate_a"],
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE graph_entity_occurrences SET purged_at=now() WHERE id=$1",
                ids["entity_occurrence_a"],
            )
        await connection.execute(
            "UPDATE graph_entity_occurrences SET purged_at=now(),raw_payload=NULL WHERE id=$1",
            ids["entity_occurrence_a"],
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE graph_entity_candidate_evidence SET purged_at=now() WHERE id=$1",
                ids["valid_evidence"],
            )
        await connection.execute(
            "UPDATE graph_entity_candidate_evidence SET purged_at=now(),quote_text=NULL,"
            "resolved_source_span=NULL,validation_error=NULL,candidate_matches='[]'::jsonb "
            "WHERE id=$1",
            ids["valid_evidence"],
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE graph_extraction_conflicts SET purged_at=now() WHERE id=$1",
                ids["conflict"],
            )
        await connection.execute(
            "UPDATE graph_extraction_conflicts SET purged_at=now(),details=NULL,"
            "description=NULL,evidence=NULL WHERE id=$1",
            ids["conflict"],
        )

        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE graph_entity_candidate_evidence SET candidate_matches='[]'::jsonb "
                "WHERE id=$1",
                ids["ambiguous_evidence"],
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE graph_entity_candidate_evidence SET candidate_matches=$1::jsonb "
                "WHERE id=$2",
                json.dumps([{"source_span": {"start": 0, "end": 1}}]),
                ids["invalid_evidence"],
            )

        for table, row_id in (
            ("evidence_units", ids["evidence"]),
            ("chunks", ids["chunk"]),
            ("document_blocks", ids["block"]),
            ("document_revisions", ids["revision"]),
        ):
            with pytest.raises(asyncpg.IntegrityConstraintViolationError):
                await connection.execute(f"DELETE FROM {table} WHERE id=$1", row_id)

        await connection.execute(
            "DELETE FROM knowledge_relations WHERE id=$1", ids["deletable_relation"]
        )
        relation_candidate = await connection.fetchrow(
            "SELECT matched_relation_id,materialized_relation_id "
            "FROM graph_relation_candidates WHERE id=$1",
            ids["relation_candidate"],
        )
        assert relation_candidate["matched_relation_id"] is None
        assert relation_candidate["materialized_relation_id"] is None

        await connection.execute(
            "DELETE FROM entities WHERE id=$1", ids["deletable_entity"]
        )
        entity_candidate = await connection.fetchrow(
            "SELECT matched_entity_id,materialized_entity_id "
            "FROM graph_entity_candidates WHERE id=$1",
            ids["entity_candidate_a"],
        )
        merge_target = await connection.fetchval(
            "SELECT suggested_target_entity_id FROM graph_entity_merge_candidates WHERE id=$1",
            ids["merge"],
        )
        assert entity_candidate["matched_entity_id"] is None
        assert entity_candidate["materialized_entity_id"] is None
        assert merge_target is None

        await connection.execute(
            "DELETE FROM graph_extraction_jobs WHERE id=$1", ids["job"]
        )
        for table in _M3_TABLES:
            assert await connection.fetchval(f"SELECT count(*) FROM {table.name}") == 0
        for table, row_id in (
            ("entities", ids["source_entity"]),
            ("entity_mentions", ids["mention_keyed"]),
            ("knowledge_relations", ids["formal_relation"]),
            ("relation_evidence", ids["formal_relation_evidence"]),
        ):
            value = await connection.fetchval(
                f"SELECT created_by_job_id FROM {table} WHERE id=$1", row_id
            )
            assert value is None
        assert (
            await connection.fetchval(
                "SELECT extraction_key FROM entity_mentions WHERE id=$1",
                ids["mention_keyed"],
            )
            == "mention-key"
        )
        assert (
            await connection.fetchval(
                "SELECT extraction_key FROM knowledge_relations WHERE id=$1",
                ids["formal_relation"],
            )
            == "relation-key"
        )
    finally:
        await connection.close()


def test_m3_upgrade_constraints_cascades_and_downgrade_on_postgresql(monkeypatch):
    name = "vkt_v04_m3_" + uuid.uuid4().hex[:8]
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0021")
        baseline = asyncio.run(_schema_snapshot(name))

        command.upgrade(config, "0022")
        asyncio.run(_assert_upgraded_schema(name))
        ids = asyncio.run(_seed(name))
        asyncio.run(_exercise_constraints(name, ids))

        command.downgrade(config, "0021")
        assert asyncio.run(_schema_snapshot(name)) == baseline
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
