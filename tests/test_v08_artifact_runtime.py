from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import CheckConstraint


MIGRATION = Path("alembic/versions/0025_v08_knowledge_artifact_runtime.py")


def _check_sql(table, name: str) -> str:
    constraint = next(
        item
        for item in table.constraints
        if isinstance(item, CheckConstraint) and item.name == name
    )
    return " ".join(str(constraint.sqltext).lower().split())


def _library(**overrides):
    values = {
        "id": uuid.uuid4(),
        "deleted_at": None,
        "knowledge_artifact_auto_enabled": True,
        "summary_artifact_enabled": True,
        "outline_artifact_enabled": True,
        "external_llm_enabled": True,
        "knowledge_artifact_external_model_enabled": True,
        "knowledge_artifact_allowed_security_levels": ["internal"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _revision(library_id, document_id, **overrides):
    values = {
        "id": uuid.uuid4(),
        "library_id": library_id,
        "document_id": document_id,
        "status": "ready",
        "content_hash": "a" * 64,
        "security_level": "internal",
        "normalized_text": "A short source document.",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _document(library_id, revision_id, **overrides):
    values = {
        "id": uuid.uuid4(),
        "library_id": library_id,
        "current_revision_id": revision_id,
        "status": "ready",
        "deleted_at": None,
        "title": "Contract",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_runtime_model_and_migration_contract():
    from app.models.knowledge_artifact_job import KnowledgeArtifactJob
    from app.models.library import Library
    from app.models.service_heartbeat import ServiceHeartbeat

    job_columns = KnowledgeArtifactJob.__table__.c
    for name in (
        "attempt_count",
        "claim_token",
        "claimed_by",
        "lease_expires_at",
        "last_heartbeat_at",
    ):
        assert name in job_columns
    assert job_columns.attempt_count.nullable is False
    assert job_columns.claimed_by.type.length == 160
    assert {
        "ck_knowledge_artifact_jobs_attempt_count",
        "ck_knowledge_artifact_jobs_claim_state",
    } <= {
        item.name
        for item in KnowledgeArtifactJob.__table__.constraints
        if item.name is not None
    }
    claim_sql = _check_sql(
        KnowledgeArtifactJob.__table__, "ck_knowledge_artifact_jobs_claim_state"
    )
    assert "status = 'processing'" in claim_sql
    assert "claim_token is not null" in claim_sql
    assert "status <> 'processing'" in claim_sql
    assert "ix_knowledge_artifact_jobs_claimable" in {
        item.name for item in KnowledgeArtifactJob.__table__.indexes
    }

    library_columns = Library.__table__.c
    for name in (
        "knowledge_artifact_auto_enabled",
        "summary_artifact_enabled",
        "outline_artifact_enabled",
        "knowledge_artifact_external_model_enabled",
        "knowledge_artifact_allowed_security_levels",
    ):
        assert name in library_columns
        assert library_columns[name].nullable is False
    for name in (
        "knowledge_artifact_auto_enabled",
        "summary_artifact_enabled",
        "outline_artifact_enabled",
        "knowledge_artifact_external_model_enabled",
    ):
        assert str(library_columns[name].server_default.arg).lower() == "false"

    heartbeat_sql = _check_sql(ServiceHeartbeat.__table__, "ck_heartbeat_service_type")
    assert "knowledge_artifact_worker" in heartbeat_sql

    text = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0025"' in text
    assert 'down_revision: Union[str, None] = "0024"' in text
    for name in job_columns.keys():
        if name in {
            "attempt_count",
            "claim_token",
            "claimed_by",
            "lease_expires_at",
            "last_heartbeat_at",
        }:
            assert f'"{name}"' in text
    for name in (
        "knowledge_artifact_auto_enabled",
        "summary_artifact_enabled",
        "outline_artifact_enabled",
        "knowledge_artifact_external_model_enabled",
        "knowledge_artifact_allowed_security_levels",
    ):
        assert f'"{name}"' in text
        assert f'op.drop_column("sys_libraries", "{name}")' in text
    assert "knowledge_artifact_worker" in text
    assert "runtime_upgrade_requeued" in text
    assert "runtime_downgrade_requeued" in text


def test_runtime_config_defaults_and_fail_closed_validation():
    from app.config import Settings, validate_knowledge_artifact_startup

    config = Settings(_env_file=None)
    assert config.knowledge_artifact_runtime_enabled is False
    assert config.knowledge_artifact_auto_trigger_enabled is False
    assert config.knowledge_artifact_external_model_enabled is False
    assert config.knowledge_artifact_short_summary_max_chars == 4_000
    assert config.knowledge_artifact_model_max_source_chars == 24_000
    validate_knowledge_artifact_startup(config)

    invalid = (
        {"knowledge_artifact_auto_trigger_enabled": True},
        {"knowledge_artifact_worker_lease_seconds": 10, "knowledge_artifact_worker_renew_seconds": 5},
        {
            "knowledge_artifact_worker_lease_seconds": 10,
            "knowledge_artifact_provider_timeout_seconds": 10,
        },
        {
            "knowledge_artifact_short_summary_max_chars": 10_000,
            "knowledge_artifact_model_max_source_chars": 5_000,
        },
        {"knowledge_artifact_worker_max_attempts": 0},
        {
            "knowledge_artifact_runtime_enabled": True,
            "knowledge_artifact_external_model_enabled": True,
        },
        {
            "knowledge_artifact_runtime_enabled": True,
            "knowledge_artifact_external_model_enabled": True,
            "graph_extraction_api_key": SecretStr("key"),
            "graph_extraction_base_url": "",
        },
        {"knowledge_artifact_provider_timeout_seconds": float("nan")},
        {"knowledge_artifact_worker_poll_seconds": float("inf")},
    )
    for overrides in invalid:
        with pytest.raises(RuntimeError):
            validate_knowledge_artifact_startup(Settings(_env_file=None, **overrides))


def test_api_lifespan_calls_knowledge_artifact_startup_validator(monkeypatch):
    import inspect

    import app.main as main

    calls = []
    monkeypatch.setattr(
        main,
        "validate_knowledge_artifact_startup",
        lambda config: calls.append(config),
    )
    main.assert_knowledge_artifact_startup_security()
    assert calls == [main.settings]
    assert "assert_knowledge_artifact_startup_security" in inspect.getsource(main.lifespan)


def test_library_update_contract_normalizes_artifact_policy_and_rejects_null():
    from pydantic import ValidationError

    from app.schemas.admin import LibraryUpdate

    update = LibraryUpdate(
        knowledge_artifact_auto_enabled=True,
        summary_artifact_enabled=True,
        outline_artifact_enabled=True,
        knowledge_artifact_external_model_enabled=True,
        knowledge_artifact_allowed_security_levels=[
            " restricted ",
            "internal",
            "internal",
        ],
    )
    assert update.knowledge_artifact_allowed_security_levels == [
        "internal",
        "restricted",
    ]
    for field in (
        "knowledge_artifact_auto_enabled",
        "summary_artifact_enabled",
        "outline_artifact_enabled",
        "knowledge_artifact_external_model_enabled",
        "knowledge_artifact_allowed_security_levels",
    ):
        with pytest.raises(ValidationError):
            LibraryUpdate(**{field: None})


def test_deterministic_summary_is_normalized_bounded_and_repeatable():
    from app.services.knowledge_artifact_generation import deterministic_summary

    source = "  First line.\n\n Second\tline.  "
    left = deterministic_summary(source)
    right = deterministic_summary(source)
    assert left == right
    assert left.summary == "First line. Second line."
    assert left.source_character_count == len(source)
    assert left.generation_mode == "deterministic"
    assert left.truncated is False

    long = deterministic_summary("x" * 16_010)
    assert len(long.summary) == 16_000
    assert long.truncated is True


def test_summary_prompt_requires_a_concise_chinese_semantic_summary():
    from app.services.knowledge_artifact_generation import summary_messages

    messages = summary_messages("source")
    prompt = messages[0]["content"]
    assert "300" in prompt and "500" in prompt
    assert "不要逐段照抄原文" in prompt
    assert messages[1] == {"role": "user", "content": "source"}


def test_deterministic_outline_expands_paths_deduplicates_and_falls_back():
    from app.services.knowledge_artifact_generation import deterministic_outline

    payload = deterministic_outline(
        [["Contract"], ["Contract", "Payment"], ["Contract", "Payment"]],
        document_title="Ignored fallback",
    )
    assert [item.model_dump() for item in payload.items] == [
        {"level": 1, "title": "Contract", "path": ["Contract"]},
        {
            "level": 2,
            "title": "Payment",
            "path": ["Contract", "Payment"],
        },
    ]
    fallback = deterministic_outline([], document_title="Untitled source")
    assert fallback.items[0].title == "Untitled source"

    with pytest.raises(ValueError):
        deterministic_outline([], document_title="  ")


def test_generation_policy_enforces_library_type_and_model_egress_gates():
    from app.config import Settings
    from app.services.knowledge_artifact_policy import (
        KnowledgeArtifactRuntimeError,
        select_generation_spec,
    )

    config = Settings(
        _env_file=None,
        knowledge_artifact_runtime_enabled=True,
        knowledge_artifact_external_model_enabled=True,
        graph_extraction_api_key=SecretStr("key"),
    )
    library = _library()
    document_id = uuid.uuid4()
    revision = _revision(library.id, document_id)

    short = select_generation_spec(
        library=library,
        revision=revision,
        artifact_type="summary",
        source_character_count=100,
        config=config,
    )
    assert short.generation_mode == "model"
    assert short.model_provider == "openai-compatible"

    long = select_generation_spec(
        library=library,
        revision=revision,
        artifact_type="summary",
        source_character_count=5_000,
        config=config,
    )
    assert long.generation_mode == "model"
    assert long.model_provider == "openai-compatible"
    assert len(long.model_config_hash or "") == 64

    outline = select_generation_spec(
        library=library,
        revision=revision,
        artifact_type="outline",
        source_character_count=100,
        config=config,
    )
    assert outline.generation_mode == "model"
    assert outline.model_provider == "openai-compatible"

    with pytest.raises(KnowledgeArtifactRuntimeError) as exc_info:
        select_generation_spec(
            library=library,
            revision=revision,
            artifact_type="summary",
            source_character_count=5_000,
            config=Settings(
                _env_file=None,
                knowledge_artifact_runtime_enabled=True,
            ),
        )
    assert exc_info.value.code == "external_model_disabled"

    cases = (
        (
            _library(summary_artifact_enabled=False),
            revision,
            "summary",
            100,
            "library_artifact_type_disabled",
        ),
        (
            _library(external_llm_enabled=False),
            revision,
            "summary",
            5_000,
            "library_external_llm_disabled",
        ),
        (
            _library(knowledge_artifact_external_model_enabled=False),
            revision,
            "summary",
            5_000,
            "library_external_model_disabled",
        ),
        (
            library,
            _revision(library.id, document_id, security_level="restricted"),
            "summary",
            5_000,
            "security_level_denied",
        ),
    )
    for lib, rev, artifact_type, count, code in cases:
        with pytest.raises(KnowledgeArtifactRuntimeError) as exc_info:
            select_generation_spec(
                library=lib,
                revision=rev,
                artifact_type=artifact_type,
                source_character_count=count,
                config=config,
            )
        assert exc_info.value.code == code


def test_scope_validation_rejects_stale_deleted_and_cross_library_inputs():
    from app.config import Settings
    from app.services.knowledge_artifact_policy import (
        KnowledgeArtifactRuntimeError,
        validate_enqueue_scope,
    )

    config = Settings(_env_file=None, knowledge_artifact_runtime_enabled=True)
    library = _library()
    document_id = uuid.uuid4()
    revision = _revision(library.id, document_id)
    document = _document(library.id, revision.id, id=document_id)
    validate_enqueue_scope(
        library=library,
        document=document,
        revision=revision,
        auto_trigger=False,
        config=config,
    )

    cases = (
        (_library(deleted_at=object()), document, revision, "library_deleted"),
        (library, _document(library.id, revision.id, id=document_id, deleted_at=object()), revision, "document_deleted"),
        (library, _document(library.id, uuid.uuid4(), id=document_id), revision, "historical_revision"),
        (library, document, _revision(uuid.uuid4(), document_id), "scope_mismatch"),
        (
            library,
            document,
            _revision(library.id, document_id, id=revision.id, status="failed"),
            "revision_not_ready",
        ),
        (
            _library(knowledge_artifact_auto_enabled=False),
            document,
            revision,
            "library_artifact_auto_disabled",
        ),
    )
    for lib, doc, rev, code in cases:
        with pytest.raises(KnowledgeArtifactRuntimeError) as exc_info:
            validate_enqueue_scope(
                library=lib,
                document=doc,
                revision=rev,
                auto_trigger=code == "library_artifact_auto_disabled",
                config=config,
            )
        assert exc_info.value.code == code


def test_model_summary_provider_has_no_raw_response_field_and_sanitizes_errors():
    from app.services.knowledge_artifact_provider import (
        KnowledgeArtifactProviderError,
        OpenAICompatibleSummaryProvider,
    )

    secret = "DO-NOT-LEAK"

    def success(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {secret}"
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "id": "request-1",
                "choices": [
                    {
                        "message": {"content": '{"summary":"Model result"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    provider = OpenAICompatibleSummaryProvider(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-pro",
        api_key=secret,
        transport=httpx.MockTransport(success),
    )
    response = asyncio.run(provider.generate([{"role": "user", "content": "source"}]))
    assert response.content == '{"summary":"Model result"}'
    assert "Model result" not in repr(response)
    assert not hasattr(response, "raw_response")
    assert len(response.request_payload_hash) == 64

    def failure(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"private body {secret}")

    provider = OpenAICompatibleSummaryProvider(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-pro",
        api_key=secret,
        transport=httpx.MockTransport(failure),
    )
    with pytest.raises(KnowledgeArtifactProviderError) as exc_info:
        asyncio.run(provider.generate([{"role": "user", "content": "source"}]))
    assert exc_info.value.category == "http_error"
    assert secret not in str(exc_info.value)
    assert "private body" not in str(exc_info.value)


def test_model_outline_parser_and_prompt_require_real_hierarchy():
    from app.services.knowledge_artifact_generation import (
        KnowledgeArtifactGenerationError,
        outline_messages,
        parse_model_outline,
    )

    payload = parse_model_outline(
        '{"items":[{"level":1,"title":"Overview","path":["Overview"]},'
        '{"level":2,"title":"Scope","path":["Overview","Scope"]}]}'
    )
    assert payload.generation_mode == "model"
    assert [item.title for item in payload.items] == ["Overview", "Scope"]
    prompt = outline_messages("source")[0]["content"]
    assert "不要把文件名当作章节" in prompt

    with pytest.raises(KnowledgeArtifactGenerationError):
        parse_model_outline(
            '{"items":[{"level":2,"title":"Scope","path":["Wrong"]}]}'
        )

def test_model_summary_parser_rejects_unknown_invalid_and_oversized_output():
    from app.services.knowledge_artifact_generation import (
        KnowledgeArtifactGenerationError,
        parse_model_summary,
    )

    payload = parse_model_summary(
        '{"summary":"Model result"}',
        source_character_count=10_000,
        source_truncated=True,
    )
    assert payload.summary == "Model result"
    assert payload.generation_mode == "model"
    assert payload.truncated is True

    for content in (
        "not-json",
        "[]",
        '{"summary":"ok","extra":true}',
        json.dumps({"summary": "x" * 16_001}),
    ):
        with pytest.raises(KnowledgeArtifactGenerationError) as exc_info:
            parse_model_summary(
                content,
                source_character_count=10_000,
                source_truncated=False,
            )
        assert exc_info.value.code in {
            "invalid_provider_json",
            "invalid_provider_payload",
        }


def test_worker_entrypoint_uses_dedicated_heartbeat_and_runtime(monkeypatch):
    from app.services import heartbeat
    from app.workers import knowledge_artifacts as worker

    monkeypatch.setattr(worker.settings, "knowledge_artifact_runtime_enabled", True)
    monkeypatch.setattr(worker, "validate_knowledge_artifact_startup", lambda _config: None)
    monkeypatch.setattr(heartbeat, "make_instance_id", lambda: "artifact-worker-1")
    monkeypatch.setattr(heartbeat, "heartbeat_loop", AsyncMock())
    monkeypatch.setattr(heartbeat, "beat", AsyncMock(return_value=True))
    runtime = AsyncMock()
    with patch(
        "app.services.knowledge_artifact_worker.run_knowledge_artifact_worker",
        runtime,
    ):
        asyncio.run(worker.run(watch=True))

    runtime.assert_awaited_once()
    assert runtime.await_args.kwargs["metadata"]["mode"] == "active_worker"
    heartbeat.heartbeat_loop.assert_called_once()
    assert heartbeat.heartbeat_loop.call_args.args[:2] == (
        "knowledge_artifact_worker",
        "artifact-worker-1",
    )


def test_local_scripts_manage_artifact_worker_without_reading_or_printing_secret():
    start = Path("scripts/start_local.ps1").read_text(encoding="utf-8")
    stop = Path("scripts/stop_local.ps1").read_text(encoding="utf-8")
    status = Path("scripts/status_local.ps1").read_text(encoding="utf-8")
    assert "KNOWLEDGE_ARTIFACT_RUNTIME_ENABLED" in start
    assert "app.workers.knowledge_artifacts" in start
    for text in (start, stop, status):
        assert "knowledge_artifacts.pid" in text
        assert "KNOWLEDGE_ARTIFACT_API_KEY" not in text


class _ScalarRows:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows=(), *, rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalars(self):
        return _ScalarRows(self._rows)

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _JobDb:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)

    def begin_nested(self):
        return _Nested()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def test_enqueue_is_idempotent_for_the_same_revision_and_contract():
    from app.config import Settings
    from app.services.knowledge_artifact_jobs import enqueue_knowledge_artifact_job

    config = Settings(
        _env_file=None,
        knowledge_artifact_runtime_enabled=True,
        knowledge_artifact_external_model_enabled=True,
        graph_extraction_api_key=SecretStr("key"),
    )
    library = _library()
    document_id = uuid.uuid4()
    revision = _revision(library.id, document_id)
    document = _document(library.id, revision.id, id=document_id)
    db = _JobDb([_Result()])

    created = asyncio.run(
        enqueue_knowledge_artifact_job(
            db,
            library=library,
            document=document,
            revision=revision,
            artifact_type="summary",
            trigger_type="manual",
            config=config,
        )
    )
    db.results.append(_Result([created]))
    duplicate = asyncio.run(
        enqueue_knowledge_artifact_job(
            db,
            library=library,
            document=document,
            revision=revision,
            artifact_type="summary",
            trigger_type="manual",
            config=config,
        )
    )

    assert duplicate is created
    assert db.added == [created]
    assert created.idempotency_key.startswith("knowledge-artifact-job-v1:")


def _queued_job(*, attempt_count=0):
    from app.config import Settings
    from app.services.knowledge_artifact_policy import _model_config_hash
    from app.services.knowledge_artifacts import build_artifact_job

    config = Settings(_env_file=None)
    job = build_artifact_job(
        library_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_revision_id=uuid.uuid4(),
        revision_content_hash="a" * 64,
        artifact_type="summary",
        contract_version="summary-v1",
        extractor_version="summary-extractor-v1",
        generation_mode="model",
        model_provider="openai-compatible",
        model_name=config.graph_extraction_model,
        model_config_hash=_model_config_hash(config),
        trigger_type="manual",
    )
    job.attempt_count = attempt_count
    return job


def test_claim_uses_skip_locked_and_sets_a_bounded_live_lease():
    from app.services.knowledge_artifact_jobs import claim_knowledge_artifact_job

    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    job = _queued_job()
    db = _JobDb([_Result(rowcount=0), _Result([job])])
    claimed = asyncio.run(
        claim_knowledge_artifact_job(
            db,
            worker_id="worker-1",
            lease_seconds=180,
            max_attempts=3,
            now=now,
        )
    )

    assert claimed.job_id == job.id
    assert claimed.claim_token == job.claim_token
    assert job.status == "processing"
    assert job.attempt_count == 1
    assert job.claimed_by == "worker-1"
    assert job.lease_expires_at == now + timedelta(seconds=180)
    assert db.statements[1]._for_update_arg.skip_locked is True


def test_expired_leases_requeue_or_fail_at_the_attempt_limit():
    from app.services.knowledge_artifact_jobs import (
        recover_stale_knowledge_artifact_jobs,
    )

    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    retryable = _queued_job(attempt_count=1)
    exhausted = _queued_job(attempt_count=3)
    for job in (retryable, exhausted):
        job.status = "processing"
        job.claim_token = uuid.uuid4()
        job.claimed_by = "worker"
        job.lease_expires_at = now - timedelta(seconds=1)
        job.last_heartbeat_at = now - timedelta(seconds=10)
    db = _JobDb([_Result([retryable, exhausted])])

    result = asyncio.run(
        recover_stale_knowledge_artifact_jobs(
            db,
            max_attempts=3,
            now=now,
        )
    )

    assert result.requeued == 1 and result.failed == 1
    assert retryable.status == "queued"
    assert exhausted.status == "failed"
    for job in (retryable, exhausted):
        assert job.claim_token is None
        assert job.lease_expires_at is None


def _prepared(job, claim_token, source="short source"):
    from app.services.knowledge_artifact_publication import PreparedArtifactJob

    return PreparedArtifactJob(
        job_id=job.id,
        claim_token=claim_token,
        library_id=job.library_id,
        document_id=job.document_id,
        document_revision_id=job.document_revision_id,
        artifact_type=job.artifact_type,
        contract_version=job.contract_version,
        extractor_version=job.extractor_version,
        generation_mode=job.generation_mode,
        input_fingerprint=job.input_fingerprint,
        revision_content_hash="a" * 64,
        source_text=source,
        source_character_count=len(source),
        source_truncated=False,
        title_paths=(),
        document_title="Document",
        model_provider=job.model_provider,
        model_name=job.model_name,
        model_config_hash=job.model_config_hash,
    )


class _PublishDb(_JobDb):
    def __init__(self, *, job, document, revision, library, previous):
        super().__init__(
            [
                _Result([document]),
                _Result([library]),
                _Result([job]),
                _Result([revision]),
                _Result([previous]),
            ]
        )


def test_publish_is_claim_and_revision_fenced_and_stales_previous_current():
    from app.config import Settings
    from app.services.knowledge_artifact_generation import parse_model_summary
    from app.services.knowledge_artifact_publication import publish_knowledge_artifact

    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    job = _queued_job()
    job.status = "processing"
    job.claim_token = uuid.uuid4()
    job.claimed_by = "worker"
    job.lease_expires_at = now + timedelta(seconds=60)
    job.last_heartbeat_at = now
    library = _library(id=job.library_id)
    revision = _revision(
        job.library_id,
        job.document_id,
        id=job.document_revision_id,
        normalized_text="short source",
    )
    document = _document(
        job.library_id,
        job.document_revision_id,
        id=job.document_id,
    )
    previous = SimpleNamespace(lifecycle_state="current", stale_at=None)
    db = _PublishDb(
        job=job,
        document=document,
        revision=revision,
        library=library,
        previous=previous,
    )
    prepared = _prepared(job, job.claim_token)
    artifact = asyncio.run(
        publish_knowledge_artifact(
            db,
            prepared=prepared,
            payload=parse_model_summary(
                '{"summary":"Concise result"}',
                source_character_count=len("short source"),
                source_truncated=False,
            ),
            now=now,
            config=Settings(
                _env_file=None,
                knowledge_artifact_runtime_enabled=True,
                knowledge_artifact_external_model_enabled=True,
                graph_extraction_api_key=SecretStr("key"),
            ),
        )
    )

    assert previous.lifecycle_state == "stale"
    assert previous.stale_at == now
    assert artifact.lifecycle_state == "current"
    assert artifact in db.added
    assert job.status == "succeeded"
    assert job.claim_token is None
    statements = [str(statement).lower() for statement in db.statements]
    assert "from documents" in statements[0]
    assert "from sys_libraries" in statements[1]
    assert "from knowledge_artifact_jobs" in statements[2]


def test_publish_rechecks_auto_policy_for_revision_ready_jobs():
    from app.config import Settings
    from app.services.knowledge_artifact_generation import deterministic_summary
    from app.services.knowledge_artifact_policy import KnowledgeArtifactRuntimeError
    from app.services.knowledge_artifact_publication import publish_knowledge_artifact

    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    job = _queued_job()
    job.trigger_type = "revision_ready"
    job.status = "processing"
    job.claim_token = uuid.uuid4()
    job.claimed_by = "worker"
    job.lease_expires_at = now + timedelta(seconds=60)
    job.last_heartbeat_at = now
    library = _library(id=job.library_id, knowledge_artifact_auto_enabled=False)
    revision = _revision(
        job.library_id,
        job.document_id,
        id=job.document_revision_id,
        normalized_text="short source",
    )
    document = _document(
        job.library_id,
        job.document_revision_id,
        id=job.document_id,
    )
    db = _PublishDb(
        job=job,
        document=document,
        revision=revision,
        library=library,
        previous=SimpleNamespace(lifecycle_state="current", stale_at=None),
    )

    with pytest.raises(KnowledgeArtifactRuntimeError) as exc_info:
        asyncio.run(
            publish_knowledge_artifact(
                db,
                prepared=_prepared(job, job.claim_token),
                payload=deterministic_summary("short source"),
                now=now,
                config=Settings(
                    _env_file=None,
                    knowledge_artifact_runtime_enabled=True,
                ),
            )
        )

    assert exc_info.value.code == "library_artifact_auto_disabled"
    assert job.status == "processing"


class _TrackedTransaction:
    def __init__(self, owner):
        self.owner = owner

    async def __aenter__(self):
        self.owner.depth += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.owner.depth -= 1
        return False


class _TrackedSession:
    def __init__(self, owner):
        self.owner = owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return _TrackedTransaction(self.owner)


class _TrackedFactory:
    def __init__(self):
        self.depth = 0

    def __call__(self):
        return _TrackedSession(self)


def test_model_provider_runs_between_prepare_and_publish_transactions():
    from app.config import Settings
    from app.services.knowledge_artifact_jobs import ClaimedArtifactJob
    from app.services.knowledge_artifact_publication import PreparedArtifactJob
    from app.services.knowledge_artifact_worker import process_knowledge_artifact_job

    factory = _TrackedFactory()
    claimed = ClaimedArtifactJob(uuid.uuid4(), uuid.uuid4())
    prepared = PreparedArtifactJob(
        job_id=claimed.job_id,
        claim_token=claimed.claim_token,
        library_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_revision_id=uuid.uuid4(),
        artifact_type="summary",
        contract_version="summary-v1",
        extractor_version="summary-extractor-v1",
        generation_mode="model",
        input_fingerprint="a" * 64,
        revision_content_hash="b" * 64,
        source_text="source",
        source_character_count=20_000,
        source_truncated=True,
        title_paths=(),
        document_title="Document",
        model_provider="deepseek",
        model_name="deepseek-v4-pro",
        model_config_hash="c" * 64,
    )
    assert "source_text=" not in repr(prepared)
    assert "Document" not in repr(prepared)

    async def provider_call(_messages):
        assert factory.depth == 0
        return SimpleNamespace(content='{"summary":"result"}')

    async def publish(_db, **_kwargs):
        assert factory.depth == 1

    provider = SimpleNamespace(generate=provider_call)
    with (
        patch(
            "app.services.knowledge_artifact_worker.prepare_knowledge_artifact_job",
            new=AsyncMock(return_value=prepared),
        ),
        patch(
            "app.services.knowledge_artifact_worker.publish_knowledge_artifact",
            new=AsyncMock(side_effect=publish),
        ),
    ):
        result = asyncio.run(
            process_knowledge_artifact_job(
                factory,
                claimed=claimed,
                provider=provider,
                config=Settings(
                    _env_file=None,
                    knowledge_artifact_runtime_enabled=True,
                    knowledge_artifact_external_model_enabled=True,
                    knowledge_artifact_api_key=SecretStr("key"),
                ),
            )
        )

    assert result.outcome == "succeeded"
    assert factory.depth == 0


def test_revision_supersession_updates_artifacts_and_live_jobs_together():
    from app.services.knowledge_artifact_publication import supersede_revision_artifacts

    db = _JobDb([_Result(rowcount=2), _Result(rowcount=3)])
    result = asyncio.run(
        supersede_revision_artifacts(
            db,
            library_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_revision_id=uuid.uuid4(),
        )
    )
    assert result == (2, 3)
    statements = [str(statement).lower() for statement in db.statements]
    assert "update knowledge_artifacts" in statements[0]
    assert "update knowledge_artifact_jobs" in statements[1]


def test_library_policy_cancellation_is_scoped_and_clears_live_claims():
    from app.services.knowledge_artifact_jobs import (
        cancel_library_artifact_jobs_for_policy_change,
    )

    library_id = uuid.uuid4()
    db = _JobDb([_Result(rowcount=4)])
    cancelled = asyncio.run(
        cancel_library_artifact_jobs_for_policy_change(
            db,
            library_id=library_id,
            error_code="library_artifact_policy_changed",
            artifact_types=("summary",),
            include_model_jobs=True,
        )
    )
    assert cancelled == 4
    statement = db.statements[0]
    sql = str(statement).lower()
    assert "knowledge_artifact_jobs.library_id" in sql
    assert "knowledge_artifact_jobs.artifact_type" in sql
    assert "knowledge_artifact_jobs.generation_mode" in sql
    params = statement.compile().params
    assert params["status"] == "cancelled"
    assert params["claim_token"] is None
    assert params["lease_expires_at"] is None


def test_admin_library_policy_update_cancels_affected_jobs_in_same_flow():
    from app.api import admin_libraries
    from app.models.library import Library
    from app.schemas.admin import LibraryUpdate

    library = Library(
        id=uuid.uuid4(),
        slug="artifact_policy",
        name="Artifact Policy",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="lib_artifact_policy",
        index_state="ready",
        graph_extraction_enabled=False,
        external_llm_enabled=False,
        graph_extraction_allowed_security_levels=[],
        knowledge_artifact_auto_enabled=True,
        summary_artifact_enabled=True,
        outline_artifact_enabled=True,
        knowledge_artifact_external_model_enabled=True,
        knowledge_artifact_allowed_security_levels=["internal"],
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_Result([library])),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    body = LibraryUpdate(
        summary_artifact_enabled=False,
        knowledge_artifact_allowed_security_levels=["restricted"],
    )
    actor = SimpleNamespace(id=uuid.uuid4())
    with (
        patch(
            "app.api.admin_libraries.knowledge_artifact_jobs."
            "cancel_library_artifact_jobs_for_policy_change",
            new=AsyncMock(return_value=3),
        ) as cancel,
        patch(
            "app.api.admin_libraries.audit_log.record",
            new=AsyncMock(),
        ) as audit,
    ):
        result = asyncio.run(
            admin_libraries.update_library(
                "artifact_policy",
                body,
                actor,
                db,
            )
        )

    assert result.summary_artifact_enabled is False
    assert result.knowledge_artifact_allowed_security_levels == ["restricted"]
    cancel.assert_awaited_once_with(
        db,
        library_id=library.id,
        error_code="library_artifact_policy_changed",
        artifact_types=("summary",),
        include_model_jobs=True,
    )
    audit_payload = audit.await_args.args[3]
    assert audit_payload["cancelled_knowledge_artifact_jobs"] == 3
    db.commit.assert_awaited_once()


def test_model_call_is_cancelled_when_periodic_lease_renewal_loses_claim():
    from app.services.knowledge_artifact_jobs import ClaimedArtifactJob
    from app.services.knowledge_artifact_policy import (
        KnowledgeArtifactRuntimeError,
    )
    from app.services.knowledge_artifact_worker import (
        call_provider_with_lease_renewal,
    )

    factory = _TrackedFactory()
    claimed = ClaimedArtifactJob(uuid.uuid4(), uuid.uuid4())

    async def slow_provider(_messages):
        await asyncio.sleep(10)

    provider = SimpleNamespace(generate=slow_provider)
    with patch(
        "app.services.knowledge_artifact_worker.renew_knowledge_artifact_lease",
        new=AsyncMock(return_value=False),
    ) as renew:
        with pytest.raises(KnowledgeArtifactRuntimeError) as exc_info:
            asyncio.run(
                call_provider_with_lease_renewal(
                    factory,
                    claimed=claimed,
                    provider=provider,
                    messages=[{"role": "user", "content": "source"}],
                    renew_seconds=0.001,
                    lease_seconds=180,
                )
            )

    assert exc_info.value.code == "claim_lost"
    renew.assert_awaited_once()
    assert factory.depth == 0


def test_retry_creates_a_linked_generation_without_overwriting_source():
    from app.config import Settings
    from app.services.knowledge_artifact_jobs import retry_knowledge_artifact_job

    source = _queued_job(attempt_count=3)
    source.status = "failed"
    library = _library(id=source.library_id)
    revision = _revision(
        source.library_id,
        source.document_id,
        id=source.document_revision_id,
    )
    document = _document(
        source.library_id,
        source.document_revision_id,
        id=source.document_id,
    )
    db = _JobDb([_Result()])
    retried = asyncio.run(
        retry_knowledge_artifact_job(
            db,
            source_job=source,
            library=library,
            document=document,
            revision=revision,
            requested_by_user_id=uuid.uuid4(),
            config=Settings(
                _env_file=None,
                knowledge_artifact_runtime_enabled=True,
                knowledge_artifact_external_model_enabled=True,
                graph_extraction_api_key=SecretStr("key"),
            ),
        )
    )

    assert source.status == "failed"
    assert retried.status == "queued"
    assert retried.retry_generation == 1
    assert retried.rerun_of_job_id == source.id
    assert retried.input_fingerprint == source.input_fingerprint
    assert retried.idempotency_key != source.idempotency_key


def test_retry_rejects_a_job_whose_generation_identity_is_no_longer_current():
    from app.config import Settings
    from app.services.knowledge_artifact_jobs import retry_knowledge_artifact_job
    from app.services.knowledge_artifact_policy import KnowledgeArtifactRuntimeError

    source = _queued_job(attempt_count=3)
    source.status = "failed"
    library = _library(id=source.library_id)
    revision = _revision(
        source.library_id,
        source.document_id,
        id=source.document_revision_id,
    )
    document = _document(
        source.library_id,
        source.document_revision_id,
        id=source.document_id,
    )

    with pytest.raises(KnowledgeArtifactRuntimeError) as exc_info:
        asyncio.run(
            retry_knowledge_artifact_job(
                _JobDb([]),
                source_job=source,
                library=library,
                document=document,
                revision=revision,
                requested_by_user_id=uuid.uuid4(),
                config=Settings(
                    _env_file=None,
                    knowledge_artifact_runtime_enabled=True,
                knowledge_artifact_external_model_enabled=True,
                graph_extraction_api_key=SecretStr("key"),
                    knowledge_artifact_summary_extractor_version="summary-extractor-v2",
                ),
            )
        )

    assert exc_info.value.code == "job_identity_stale"


def test_auto_trigger_global_flags_fail_closed_without_opening_a_session():
    from app.config import Settings
    from app.services.knowledge_artifact_jobs import (
        enqueue_ready_revision_artifacts,
    )

    calls = []

    def session_factory():
        calls.append(True)
        raise AssertionError("disabled trigger must not open a database session")

    result = asyncio.run(
        enqueue_ready_revision_artifacts(
            library_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            revision_id=uuid.uuid4(),
            session_factory=session_factory,
            config=Settings(_env_file=None),
        )
    )
    assert result == ()
    assert calls == []


def test_auto_trigger_reports_only_jobs_created_by_this_delivery():
    from app.config import Settings
    from app.models.document import Document
    from app.models.document_revision import DocumentRevision
    from app.models.library import Library
    from app.services.knowledge_artifact_jobs import (
        ArtifactJobEnqueueResult,
        enqueue_ready_revision_artifacts,
    )

    library = _library()
    revision = _revision(library.id, uuid.uuid4(), created_by=uuid.uuid4())
    document = _document(library.id, revision.id, id=revision.document_id)
    existing = _queued_job()

    class ReadySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def begin(self):
            return _Nested()

        async def get(self, model, object_id):
            values = {
                (Library, library.id): library,
                (Document, document.id): document,
                (DocumentRevision, revision.id): revision,
            }
            return values.get((model, object_id))

    enqueue_result = ArtifactJobEnqueueResult(job=existing, created=False)
    with patch(
        "app.services.knowledge_artifact_jobs."
        "_enqueue_knowledge_artifact_job_result",
        new=AsyncMock(side_effect=[enqueue_result, enqueue_result]),
    ) as enqueue:
        result = asyncio.run(
            enqueue_ready_revision_artifacts(
                library_id=library.id,
                document_id=document.id,
                revision_id=revision.id,
                session_factory=ReadySession,
                config=Settings(
                    _env_file=None,
                    knowledge_artifact_runtime_enabled=True,
                    knowledge_artifact_auto_trigger_enabled=True,
                ),
            )
        )

    assert result == ()
    assert enqueue.await_count == 2


def test_compensation_scan_excludes_revisions_that_already_have_jobs():
    from app.config import Settings
    from app.services.knowledge_artifact_jobs import (
        compensate_ready_revision_artifacts,
    )

    class ScanSession:
        def __init__(self):
            self.statement = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            self.statement = statement
            return _Result([])

    session = ScanSession()
    result = asyncio.run(
        compensate_ready_revision_artifacts(
            session_factory=lambda: session,
            config=Settings(
                _env_file=None,
                knowledge_artifact_runtime_enabled=True,
                knowledge_artifact_auto_trigger_enabled=True,
            ),
        )
    )

    assert result == 0
    sql = str(session.statement).lower()
    assert sql.count("not (exists") == 2
    assert "knowledge_artifact_jobs.artifact_type" in sql


def test_current_projection_is_fenced_to_the_document_current_revision():
    from app.services.knowledge_artifact_publication import (
        list_current_document_artifacts,
    )

    library_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    document = _document(library_id, revision_id)
    summary = SimpleNamespace(artifact_type="summary")
    outline = SimpleNamespace(artifact_type="outline")
    db = _JobDb([_Result([outline, summary])])
    rows = asyncio.run(
        list_current_document_artifacts(
            db,
            library_id=library_id,
            document=document,
        )
    )
    assert rows == (outline, summary)
    sql = str(db.statements[0]).lower()
    assert "knowledge_artifacts.document_revision_id" in sql
    assert "knowledge_artifacts.lifecycle_state" in sql


def test_one_shot_worker_compensates_then_drains_the_new_job():
    from app.config import Settings
    from app.services.knowledge_artifact_jobs import (
        ClaimedArtifactJob,
        StaleJobRecoveryResult,
    )
    from app.services.knowledge_artifact_worker import (
        ArtifactProcessResult,
        run_knowledge_artifact_worker,
    )

    factory = _TrackedFactory()
    claimed = ClaimedArtifactJob(uuid.uuid4(), uuid.uuid4())
    metadata = {}
    with (
        patch(
            "app.services.knowledge_artifact_worker."
            "recover_stale_knowledge_artifact_jobs",
            new=AsyncMock(return_value=StaleJobRecoveryResult(0, 0)),
        ),
        patch(
            "app.services.knowledge_artifact_worker.claim_knowledge_artifact_job",
            new=AsyncMock(side_effect=[None, claimed, None]),
        ),
        patch(
            "app.services.knowledge_artifact_worker."
            "compensate_ready_revision_artifacts",
            new=AsyncMock(side_effect=[1, 0]),
        ) as compensate,
        patch(
            "app.services.knowledge_artifact_worker.process_knowledge_artifact_job",
            new=AsyncMock(return_value=ArtifactProcessResult("succeeded")),
        ) as process,
    ):
        asyncio.run(
            run_knowledge_artifact_worker(
                watch=False,
                metadata=metadata,
                session_factory=factory,
                config=Settings(
                    _env_file=None,
                    knowledge_artifact_runtime_enabled=True,
                    knowledge_artifact_auto_trigger_enabled=True,
                ),
            )
        )

    assert compensate.await_count == 2
    process.assert_awaited_once()
    assert metadata["compensated"] == 1
    assert metadata["claimed"] == 1
    assert metadata["succeeded"] == 1
