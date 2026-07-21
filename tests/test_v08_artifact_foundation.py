from __future__ import annotations

import json
import math
import re
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB


MIGRATION = Path("alembic/versions/0024_v08_knowledge_artifact_foundation.py")

LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")
REVISION_ID = uuid.UUID("30000000-0000-0000-0000-000000000003")
USER_ID = uuid.UUID("40000000-0000-0000-0000-000000000004")
CONTENT_HASH = "a" * 64
MODEL_CONFIG_HASH = "b" * 64


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
    return " ".join(str(constraint.sqltext).lower().split())


def _index_names(table) -> set[str]:
    return {index.name for index in table.indexes}


def _index(table, name: str) -> Index:
    return next(index for index in table.indexes if index.name == name)


def _index_where_sql(table, name: str) -> str:
    where = _index(table, name).dialect_options["postgresql"].get("where")
    return str(where).lower()


def _foreign_key(column) -> tuple[str, str | None, str | None]:
    fk = next(iter(column.foreign_keys))
    return fk.target_fullname, fk.ondelete, fk.name


def _job_kwargs(**overrides):
    values = {
        "library_id": LIBRARY_ID,
        "document_id": DOCUMENT_ID,
        "document_revision_id": REVISION_ID,
        "revision_content_hash": CONTENT_HASH,
        "artifact_type": "summary",
        "contract_version": "summary-v1",
        "extractor_version": "summary-extractor-v1",
        "generation_mode": "deterministic",
        "trigger_type": "revision_ready",
        "requested_by_user_id": USER_ID,
    }
    values.update(overrides)
    return values


def _artifact_kwargs(job, base_payload, **overrides):
    values = {
        "job": job,
        "library_id": job.library_id,
        "document_id": job.document_id,
        "document_revision_id": job.document_revision_id,
        "artifact_type": job.artifact_type,
        "contract_version": job.contract_version,
        "extractor_version": job.extractor_version,
        "input_fingerprint": job.input_fingerprint,
        "payload": base_payload,
    }
    values.update(overrides)
    return values


def test_v08_models_are_exported():
    from app.models import KnowledgeArtifact, KnowledgeArtifactJob

    assert KnowledgeArtifactJob.__tablename__ == "knowledge_artifact_jobs"
    assert KnowledgeArtifact.__tablename__ == "knowledge_artifacts"


def test_knowledge_artifact_job_metadata_contract():
    from app.models.knowledge_artifact_job import KnowledgeArtifactJob

    table = KnowledgeArtifactJob.__table__
    cols = table.c
    required = {
        "id",
        "library_id",
        "document_id",
        "document_revision_id",
        "artifact_type",
        "contract_version",
        "extractor_version",
        "generation_mode",
        "model_provider",
        "model_name",
        "model_config_hash",
        "input_fingerprint",
        "idempotency_key",
        "retry_generation",
        "trigger_type",
        "status",
        "rerun_of_job_id",
        "requested_by_user_id",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    }
    assert required <= set(cols.keys())
    assert _foreign_key(cols.library_id) == (
        "sys_libraries.id",
        "CASCADE",
        "fk_knowledge_artifact_jobs_library",
    )
    assert _foreign_key(cols.document_id) == (
        "documents.id",
        "RESTRICT",
        "fk_knowledge_artifact_jobs_document",
    )
    assert _foreign_key(cols.document_revision_id) == (
        "document_revisions.id",
        "RESTRICT",
        "fk_knowledge_artifact_jobs_revision",
    )
    assert _foreign_key(cols.rerun_of_job_id) == (
        "knowledge_artifact_jobs.id",
        "SET NULL",
        "fk_knowledge_artifact_jobs_rerun",
    )
    assert _foreign_key(cols.requested_by_user_id) == (
        "sys_users.id",
        "SET NULL",
        "fk_knowledge_artifact_jobs_requested_by",
    )
    assert cols.input_fingerprint.type.length == 64
    assert cols.idempotency_key.type.length == 128
    assert cols.error_message.type.length == 255
    assert "uq_knowledge_artifact_jobs_idempotency_key" in _constraint_names(
        table, UniqueConstraint
    )
    assert {
        "ck_knowledge_artifact_jobs_type",
        "ck_knowledge_artifact_jobs_generation_mode",
        "ck_knowledge_artifact_jobs_model_identity",
        "ck_knowledge_artifact_jobs_trigger",
        "ck_knowledge_artifact_jobs_status",
        "ck_knowledge_artifact_jobs_retry_generation",
    } <= _constraint_names(table, CheckConstraint)
    model_sql = _check_sql(table, "ck_knowledge_artifact_jobs_model_identity")
    assert "generation_mode = 'deterministic'" in model_sql
    assert "model_provider is null" in model_sql
    assert "generation_mode = 'model'" in model_sql
    assert "model_config_hash is not null" in model_sql
    assert {
        "ix_knowledge_artifact_jobs_status_created",
        "ix_knowledge_artifact_jobs_library_status_created",
        "ix_knowledge_artifact_jobs_revision_type_created",
        "ix_knowledge_artifact_jobs_rerun",
    } <= _index_names(table)


def test_knowledge_artifact_metadata_contract():
    from app.models.knowledge_artifact import KnowledgeArtifact

    table = KnowledgeArtifact.__table__
    cols = table.c
    required = {
        "id",
        "job_id",
        "library_id",
        "document_id",
        "document_revision_id",
        "artifact_type",
        "contract_version",
        "extractor_version",
        "generation_mode",
        "model_provider",
        "model_name",
        "model_config_hash",
        "input_fingerprint",
        "payload",
        "payload_hash",
        "lifecycle_state",
        "created_at",
        "stale_at",
        "deleted_at",
    }
    assert required <= set(cols.keys())
    assert isinstance(cols.payload.type, JSONB)
    assert cols.payload.nullable is False
    assert cols.payload_hash.type.length == 64
    assert _foreign_key(cols.job_id) == (
        "knowledge_artifact_jobs.id",
        "CASCADE",
        "fk_knowledge_artifacts_job",
    )
    assert _foreign_key(cols.library_id) == (
        "sys_libraries.id",
        "CASCADE",
        "fk_knowledge_artifacts_library",
    )
    assert _foreign_key(cols.document_id) == (
        "documents.id",
        "RESTRICT",
        "fk_knowledge_artifacts_document",
    )
    assert _foreign_key(cols.document_revision_id) == (
        "document_revisions.id",
        "RESTRICT",
        "fk_knowledge_artifacts_revision",
    )
    assert "uq_knowledge_artifacts_job" in _constraint_names(table, UniqueConstraint)
    assert {
        "ck_knowledge_artifacts_type",
        "ck_knowledge_artifacts_generation_mode",
        "ck_knowledge_artifacts_model_identity",
        "ck_knowledge_artifacts_lifecycle",
    } <= _constraint_names(table, CheckConstraint)
    assert {
        "uq_knowledge_artifacts_current_revision_type",
        "ix_knowledge_artifacts_library_type_lifecycle",
        "ix_knowledge_artifacts_document_type_created",
    } <= _index_names(table)
    assert (
        "lifecycle_state = 'current'"
        in _index_where_sql(table, "uq_knowledge_artifacts_current_revision_type")
    )
    assert "updated_at" not in cols


def test_summary_and_outline_payloads_are_strict_and_json_compatible():
    from app.schemas.knowledge_artifact import (
        OutlineItemV1,
        OutlinePayloadV1,
        SummaryPayloadV1,
    )

    summary = SummaryPayloadV1(
        summary="The contract total is 120,000 yuan.",
        generation_mode="model",
        source_character_count=4200,
        truncated=False,
    )
    outline = OutlinePayloadV1(
        generation_mode="deterministic",
        items=[
            OutlineItemV1(level=1, title="Contract", path=["Contract"]),
            OutlineItemV1(
                level=2,
                title="Payment terms",
                path=["Contract", "Payment terms"],
            ),
        ],
    )

    assert summary.model_dump(mode="json")["schema_version"] == "summary-v1"
    assert outline.model_dump(mode="json")["schema_version"] == "outline-v1"
    json.dumps(summary.model_dump(mode="json"))
    json.dumps(outline.model_dump(mode="json"))

    with pytest.raises(ValidationError):
        SummaryPayloadV1(
            summary="valid",
            generation_mode="deterministic",
            source_character_count=5,
            truncated=False,
            ignored="not allowed",
        )
    with pytest.raises(ValidationError):
        SummaryPayloadV1(
            summary="   ",
            generation_mode="deterministic",
            source_character_count=3,
            truncated=False,
        )
    with pytest.raises(ValidationError):
        SummaryPayloadV1(
            summary="x" * 16_001,
            generation_mode="deterministic",
            source_character_count=16_001,
            truncated=False,
        )
    with pytest.raises(ValidationError):
        OutlinePayloadV1(generation_mode="deterministic", items=[])
    with pytest.raises(ValidationError):
        OutlineItemV1(level=7, title="Invalid", path=["Invalid"])
    with pytest.raises(ValidationError):
        OutlineItemV1(level=1, title="Invalid", path=[])


def test_payload_dispatch_and_hashing_fail_closed_with_stable_codes():
    from app.services.knowledge_artifacts import (
        ArtifactContractError,
        artifact_payload_hash_v1,
        canonical_artifact_json_v1,
        validate_artifact_payload,
    )

    left = {
        "schema_version": "summary-v1",
        "summary": "Stable",
        "generation_mode": "deterministic",
        "source_character_count": 6,
        "truncated": False,
    }
    right = dict(reversed(list(left.items())))
    assert artifact_payload_hash_v1("summary", "summary-v1", left) == (
        artifact_payload_hash_v1("summary", "summary-v1", right)
    )

    for artifact_type, version, payload, code in (
        ("table", "table-v1", left, "unsupported_artifact_type"),
        ("summary", "summary-v2", left, "unsupported_contract_version"),
        ("summary", "summary-v1", {**left, "extra": True}, "invalid_artifact_payload"),
    ):
        with pytest.raises(ArtifactContractError) as exc_info:
            validate_artifact_payload(artifact_type, version, payload)
        assert exc_info.value.code == code

    with pytest.raises(ArtifactContractError) as exc_info:
        canonical_artifact_json_v1({"score": math.nan})
    assert exc_info.value.code == "invalid_canonical_json"


def test_generation_identity_is_deterministic_and_binds_every_required_input():
    from app.services.knowledge_artifacts import (
        generation_input_fingerprint_v1,
        job_idempotency_key_v1,
    )

    base = {
        key: value
        for key, value in _job_kwargs().items()
        if key not in {"trigger_type", "requested_by_user_id"}
    }
    first = generation_input_fingerprint_v1(**base)
    assert first == generation_input_fingerprint_v1(**dict(reversed(list(base.items()))))
    assert job_idempotency_key_v1(first, 0) == job_idempotency_key_v1(first, 0)

    changes = (
        {"document_revision_id": uuid.uuid4()},
        {"artifact_type": "outline", "contract_version": "outline-v1"},
        {"contract_version": "summary-v2"},
        {"extractor_version": "summary-extractor-v2"},
        {
            "generation_mode": "model",
            "model_provider": "openai",
            "model_name": "gpt-5-mini",
            "model_config_hash": MODEL_CONFIG_HASH,
        },
    )
    for change in changes:
        changed = {**base, **change}
        assert generation_input_fingerprint_v1(**changed) != first

    assert job_idempotency_key_v1(first, 1) != job_idempotency_key_v1(first, 0)


def test_job_builder_enforces_contract_and_model_identity():
    from app.services.knowledge_artifacts import ArtifactContractError, build_artifact_job

    deterministic = build_artifact_job(**_job_kwargs())
    assert deterministic.model_provider is None
    assert deterministic.status == "queued"
    assert deterministic.retry_generation == 0
    assert deterministic.idempotency_key.startswith("knowledge-artifact-job-v1:")

    model_job = build_artifact_job(
        **_job_kwargs(
            generation_mode="model",
            model_provider="openai",
            model_name="gpt-5-mini",
            model_config_hash=MODEL_CONFIG_HASH,
        )
    )
    assert model_job.model_name == "gpt-5-mini"
    assert model_job.input_fingerprint != deterministic.input_fingerprint

    invalid_cases = (
        (_job_kwargs(artifact_type="table"), "unsupported_artifact_type"),
        (_job_kwargs(contract_version="summary-v2"), "unsupported_contract_version"),
        (_job_kwargs(generation_mode="model"), "invalid_model_identity"),
        (
            _job_kwargs(generation_mode="deterministic", model_provider="openai"),
            "invalid_model_identity",
        ),
        (_job_kwargs(retry_generation=-1), "invalid_retry_generation"),
    )
    for kwargs, code in invalid_cases:
        with pytest.raises(ArtifactContractError) as exc_info:
            build_artifact_job(**kwargs)
        assert exc_info.value.code == code


def test_artifact_builder_validates_job_scope_identity_and_payload_mode():
    from app.services.knowledge_artifacts import ArtifactContractError, build_artifact_job, build_artifact

    job = build_artifact_job(**_job_kwargs())
    payload = {
        "schema_version": "summary-v1",
        "summary": "Stable",
        "generation_mode": "deterministic",
        "source_character_count": 6,
        "truncated": False,
    }
    artifact = build_artifact(**_artifact_kwargs(job, payload))
    assert artifact.job_id == job.id
    assert artifact.payload == payload
    assert artifact.lifecycle_state == "current"
    assert len(artifact.payload_hash) == 64

    mismatches = (
        ({"library_id": uuid.uuid4()}, "artifact_scope_mismatch"),
        ({"document_id": uuid.uuid4()}, "artifact_scope_mismatch"),
        ({"document_revision_id": uuid.uuid4()}, "artifact_scope_mismatch"),
        ({"artifact_type": "outline"}, "artifact_type_mismatch"),
        ({"contract_version": "outline-v1"}, "artifact_contract_mismatch"),
        ({"extractor_version": "other"}, "artifact_extractor_mismatch"),
        ({"input_fingerprint": "f" * 64}, "artifact_input_mismatch"),
        (
            {"payload": {**payload, "generation_mode": "model"}},
            "artifact_generation_mode_mismatch",
        ),
    )
    for change, code in mismatches:
        with pytest.raises(ArtifactContractError) as exc_info:
            build_artifact(**_artifact_kwargs(job, payload, **change))
        assert exc_info.value.code == code


def test_v08_migration_is_additive_0024_and_has_exact_downgrade_order():
    text = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "0024"' in text
    assert 'down_revision: Union[str, None] = "0023"' in text
    assert re.search(r'op\.create_table\(\s*"knowledge_artifact_jobs"', text)
    assert re.search(r'op\.create_table\(\s*"knowledge_artifacts"', text)
    assert "uq_knowledge_artifacts_current_revision_type" in text
    assert "lifecycle_state = 'current'" in text
    assert "ck_knowledge_artifact_jobs_model_identity" in text
    assert "ck_knowledge_artifacts_model_identity" in text
    assert text.index('op.drop_table("knowledge_artifacts")') < text.index(
        'op.drop_table("knowledge_artifact_jobs")'
    )
    for forbidden in (
        'op.alter_column("chunks"',
        'op.drop_table("chunks")',
        'op.create_table("entities"',
        'op.create_table("knowledge_relations"',
        "normalized_text",
        "raw_provider_output",
        "signed_url",
        "credential",
    ):
        assert forbidden not in text
