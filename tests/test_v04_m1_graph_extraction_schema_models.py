from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB


def _constraint_names(table, kind) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, kind) and constraint.name is not None
    }


def _check_sql(table, name: str) -> str:
    constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == name
    )
    return str(constraint.sqltext).lower()


def _index_names(table) -> set[str]:
    return {index.name for index in table.indexes}


def _foreign_key(column) -> tuple[str, str | None, str | None]:
    fk = next(iter(column.foreign_keys))
    return fk.target_fullname, fk.ondelete, fk.name


def test_v04_m1_models_are_exported():
    from app.models import (
        ExtractionContextSnapshot,
        ExtractionRawOutputAttempt,
        GraphExtractionJob,
        GraphExtractionUnit,
    )

    assert GraphExtractionJob.__tablename__ == "graph_extraction_jobs"
    assert GraphExtractionUnit.__tablename__ == "graph_extraction_units"
    assert ExtractionContextSnapshot.__tablename__ == "extraction_context_snapshots"
    assert ExtractionRawOutputAttempt.__tablename__ == "extraction_raw_output_attempts"


def test_library_has_fail_closed_graph_extraction_fields():
    from app.models.library import Library

    cols = Library.__table__.c
    assert cols.graph_extraction_enabled.nullable is False
    assert str(cols.graph_extraction_enabled.server_default.arg) == "false"
    assert cols.external_llm_enabled.nullable is False
    assert str(cols.external_llm_enabled.server_default.arg) == "false"
    assert cols.graph_extraction_allowed_security_levels.nullable is False
    assert isinstance(cols.graph_extraction_allowed_security_levels.type, JSONB)
    assert "[]" in str(cols.graph_extraction_allowed_security_levels.server_default.arg)


def test_heartbeat_check_accepts_graph_extractor_under_existing_name():
    from app.models.service_heartbeat import ServiceHeartbeat

    sql = _check_sql(ServiceHeartbeat.__table__, "ck_heartbeat_service_type")
    for value in ("api", "embedding_worker", "cleanup_worker", "graph_extractor"):
        assert value in sql


def test_graph_extraction_job_contract():
    from app.models.graph_extraction_job import GraphExtractionJob

    table = GraphExtractionJob.__table__
    cols = table.c
    required = (
        "id",
        "library_id",
        "document_id",
        "document_revision_id",
        "ontology_version_id",
        "trigger_type",
        "execution_mode",
        "status",
        "current_stage",
        "input_fingerprint",
        "idempotency_key",
        "rerun_of_job_id",
        "retry_generation",
        "model_provider",
        "model_name",
        "prompt_version",
        "extractor_version",
        "output_parser_version",
        "context_policy_version",
        "extraction_policy_version",
        "normalization_rule_version",
        "confidence_policy_version",
        "document_parser_version",
        "chunking_strategy_version",
        "model_config_snapshot",
        "policy_config_snapshot",
        "model_config_hash",
        "policy_config_hash",
        "ontology_snapshot",
        "ontology_snapshot_hash",
        "prompt_content_hash",
        "requested_by",
        "sensitive_payload_purged_at",
        "counts",
        "statistics",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    )
    assert set(required) <= set(cols.keys())
    assert _foreign_key(cols.library_id) == (
        "sys_libraries.id",
        "CASCADE",
        "fk_graph_extraction_jobs_library",
    )
    assert _foreign_key(cols.document_id) == (
        "documents.id",
        "RESTRICT",
        "fk_graph_extraction_jobs_document",
    )
    assert _foreign_key(cols.document_revision_id) == (
        "document_revisions.id",
        "RESTRICT",
        "fk_graph_extraction_jobs_revision",
    )
    assert _foreign_key(cols.ontology_version_id) == (
        "ontology_versions.id",
        "RESTRICT",
        "fk_graph_extraction_jobs_ontology",
    )
    assert _foreign_key(cols.rerun_of_job_id) == (
        "graph_extraction_jobs.id",
        "SET NULL",
        "fk_graph_extraction_jobs_rerun",
    )
    assert _foreign_key(cols.requested_by) == (
        "sys_users.id",
        "SET NULL",
        "fk_graph_extraction_jobs_requested_by",
    )
    assert cols.current_stage.nullable is True
    assert cols.retry_generation.nullable is False
    assert isinstance(cols.model_config_snapshot.type, JSONB)
    assert isinstance(cols.policy_config_snapshot.type, JSONB)
    assert isinstance(cols.ontology_snapshot.type, JSONB)
    assert isinstance(cols.counts.type, JSONB)
    assert isinstance(cols.statistics.type, JSONB)

    assert {
        "uq_graph_extraction_jobs_idempotency_key",
    } <= _constraint_names(table, UniqueConstraint)
    assert {
        "ck_graph_extraction_jobs_trigger_type",
        "ck_graph_extraction_jobs_execution_mode",
        "ck_graph_extraction_jobs_status",
        "ck_graph_extraction_jobs_current_stage",
        "ck_graph_extraction_jobs_retry_generation",
        "ck_graph_extraction_jobs_rerun_scope",
    } <= _constraint_names(table, CheckConstraint)
    assert {
        "ix_graph_extraction_jobs_status_created",
        "ix_graph_extraction_jobs_library_status_created",
        "ix_graph_extraction_jobs_revision_created",
        "ix_graph_extraction_jobs_rerun_of",
        "ix_graph_extraction_jobs_sensitive_purge",
    } <= _index_names(table)

    status_sql = _check_sql(table, "ck_graph_extraction_jobs_status")
    for value in (
        "queued",
        "processing",
        "partially_succeeded",
        "succeeded",
        "failed",
        "cancelled",
        "superseded",
    ):
        assert value in status_sql


def test_graph_extraction_unit_contract():
    from app.models.graph_extraction_unit import GraphExtractionUnit

    table = GraphExtractionUnit.__table__
    cols = table.c
    assert set(
        (
            "id",
            "job_id",
            "library_id",
            "document_revision_id",
            "ordinal",
            "center_chunk_id",
            "center_evidence_id",
            "unit_fingerprint",
            "status",
            "model_attempt_count",
            "retryable",
            "worker_id",
            "claim_token",
            "claimed_at",
            "lease_expires_at",
            "error_code",
            "error_message",
            "created_at",
            "updated_at",
            "started_at",
            "finished_at",
        )
    ) <= set(cols.keys())
    assert _foreign_key(cols.job_id) == (
        "graph_extraction_jobs.id",
        "CASCADE",
        "fk_graph_extraction_units_job",
    )
    assert _foreign_key(cols.center_chunk_id) == (
        "chunks.id",
        "RESTRICT",
        "fk_graph_extraction_units_center_chunk",
    )
    assert _foreign_key(cols.center_evidence_id) == (
        "evidence_units.id",
        "RESTRICT",
        "fk_graph_extraction_units_center_evidence",
    )
    assert {
        "uq_graph_extraction_units_job_chunk",
        "uq_graph_extraction_units_job_ordinal",
        "uq_graph_extraction_units_job_fingerprint",
    } <= _constraint_names(table, UniqueConstraint)
    assert {
        "ck_graph_extraction_units_status",
        "ck_graph_extraction_units_attempt_count",
        "ck_graph_extraction_units_claim_fields",
    } <= _constraint_names(table, CheckConstraint)
    assert {
        "ix_graph_extraction_units_claimable",
        "ix_graph_extraction_units_job_status_ordinal",
    } <= _index_names(table)
    claim_sql = _check_sql(table, "ck_graph_extraction_units_claim_fields")
    for token in ("worker_id", "claim_token", "claimed_at", "lease_expires_at"):
        assert token in claim_sql


def test_extraction_context_snapshot_contract():
    from app.models.extraction_context_snapshot import ExtractionContextSnapshot

    table = ExtractionContextSnapshot.__table__
    cols = table.c
    sensitive = (
        "context_json",
        "context_text",
        "document_metadata",
        "chunk_title_path",
        "block_title_path",
        "effective_title_path",
    )
    for name in sensitive:
        assert cols[name].nullable is True
    for name in (
        "previous_chunk_ids",
        "next_chunk_ids",
        "block_ids",
        "context_mapping",
    ):
        assert isinstance(cols[name].type, JSONB)
        assert cols[name].nullable is False
    assert _foreign_key(cols.extraction_unit_id) == (
        "graph_extraction_units.id",
        "CASCADE",
        "fk_extraction_context_snapshots_unit",
    )
    assert _foreign_key(cols.center_evidence_id) == (
        "evidence_units.id",
        "RESTRICT",
        "fk_extraction_context_snapshots_evidence",
    )
    assert "uq_extraction_context_snapshots_unit" in _constraint_names(
        table, UniqueConstraint
    )
    assert "ck_extraction_context_snapshots_payload_or_purged" in _constraint_names(
        table, CheckConstraint
    )
    purge_sql = _check_sql(table, "ck_extraction_context_snapshots_payload_or_purged")
    assert "purged_at is null" in purge_sql
    assert "purged_at is not null" in purge_sql
    for name in sensitive:
        assert f"{name} is null" in purge_sql
    assert {
        "ix_extraction_context_snapshots_job",
        "ix_extraction_context_snapshots_purge",
    } <= _index_names(table)


def test_extraction_raw_output_attempt_contract():
    from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt

    table = ExtractionRawOutputAttempt.__table__
    cols = table.c
    assert _foreign_key(cols.extraction_unit_id) == (
        "graph_extraction_units.id",
        "CASCADE",
        "fk_extraction_raw_attempts_unit",
    )
    assert _foreign_key(cols.context_snapshot_id) == (
        "extraction_context_snapshots.id",
        "CASCADE",
        "fk_extraction_raw_attempts_context",
    )
    assert cols.claim_token.nullable is False
    assert cols.request_payload_hash.type.length == 64
    assert isinstance(cols.parsed_response.type, JSONB)
    for name in ("raw_response", "parsed_response", "parse_error", "purged_at"):
        assert cols[name].nullable is True
    assert "uq_extraction_raw_attempts_unit_no" in _constraint_names(
        table, UniqueConstraint
    )
    assert {
        "ck_extraction_raw_attempts_attempt_no",
        "ck_extraction_raw_attempts_request_status",
        "ck_extraction_raw_attempts_parse_status",
        "ck_extraction_raw_attempts_latency",
        "ck_extraction_raw_attempts_parse_by_request",
        "ck_extraction_raw_attempts_abandoned_fields",
        "ck_extraction_raw_attempts_abandoned_reason",
        "ck_extraction_raw_attempts_payload_or_purged",
    } <= _constraint_names(table, CheckConstraint)
    assert {
        "ix_extraction_raw_attempts_unit_no",
        "ix_extraction_raw_attempts_pending_claim",
        "ix_extraction_raw_attempts_purge",
    } <= _index_names(table)
    purge_sql = _check_sql(table, "ck_extraction_raw_attempts_payload_or_purged")
    assert "purged_at is null" in purge_sql
    for name in ("raw_response", "parsed_response", "parse_error"):
        assert f"{name} is null" in purge_sql


def test_rerun_scope_check_freezes_every_trigger_rule():
    from app.models.graph_extraction_job import GraphExtractionJob

    sql = " ".join(
        _check_sql(
            GraphExtractionJob.__table__,
            "ck_graph_extraction_jobs_rerun_scope",
        ).split()
    )
    assert "trigger_type = 'full_rerun' and rerun_of_job_id is not null" in sql
    assert "trigger_type = 'repair'" in sql
    assert (
        "trigger_type in ('manual','revision_published','eval') "
        "and rerun_of_job_id is null"
    ) in sql


def test_context_snapshot_is_the_immutable_timestamp_exception():
    from app.models.extraction_context_snapshot import ExtractionContextSnapshot

    cols = ExtractionContextSnapshot.__table__.c
    assert "created_at" in cols
    assert "updated_at" not in cols
    assert cols.purged_at.nullable is True
