from __future__ import annotations

import asyncio
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr, ValidationError
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql

from app.config import Settings, settings, validate_classification_runtime_startup
from app.api import admin_libraries
from app.models.classification_job import DocumentClassificationJob
from app.models.classification_taxonomy import ClassificationLabel, ClassificationTaxonomy
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.models.organization import Organization
from app.schemas.admin import LibraryUpdate
from app.services import classification_jobs as jobs
from app.services.classification_provider import (
    ClassificationProviderError,
    OpenAICompatibleClassificationProvider,
)
from app.services.classification_runtime_contracts import (
    ClassificationRuntimeError,
    build_classification_job,
    classification_job_identity,
    classification_messages,
    classification_provider_name,
    parse_classifier_output,
)
from app.services.classification_runtime_policy import (
    normalize_classification_security_levels,
    validate_classification_scope,
)


NOW = datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc)
HASH = "a" * 64


class _Result:
    def __init__(self, rows=(), scalar=None, rowcount=0):
        self.rows = list(rows)
        self.scalar = scalar
        self.rowcount = rowcount

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)

    def scalar_one(self):
        return self.scalar

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _DB:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.added = []
        self.flush_count = 0
        self.commit = AsyncMock()

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.results, "unexpected query"
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1

    def begin_nested(self):
        return _Nested()


def _offline(command_name: str, revision: str) -> str:
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    if command_name == "upgrade":
        alembic_command.upgrade(config, revision, sql=True)
    else:
        alembic_command.downgrade(config, revision, sql=True)
    return " ".join(output.getvalue().lower().split())


def _settings(**values) -> Settings:
    defaults = {
        "organization_authorization_enabled": True,
        "classification_taxonomy_enabled": True,
        "classification_decision_enabled": True,
        "classification_runtime_enabled": True,
        "classification_external_model_enabled": True,
        "classification_api_key": SecretStr("test-key"),
    }
    return Settings(_env_file=None, **{**defaults, **values})


def _organization() -> Organization:
    return Organization(
        id=uuid.uuid4(),
        slug="example",
        name="Example",
        deployment_profile="hosted",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _library(organization: Organization) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization.id,
        slug="legal",
        name="Legal",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="legal",
        index_state="ready",
        external_llm_enabled=True,
        classification_auto_enabled=True,
        classification_external_model_enabled=True,
        classification_allowed_security_levels=["internal"],
        created_at=NOW,
    )


def _taxonomy(organization: Organization) -> ClassificationTaxonomy:
    return ClassificationTaxonomy(
        id=uuid.uuid4(),
        organization_id=organization.id,
        version_key="document-category",
        version_no=1,
        status="active",
        created_by_user_id=uuid.uuid4(),
        activated_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _label(taxonomy: ClassificationTaxonomy, key: str) -> ClassificationLabel:
    return ClassificationLabel(
        id=uuid.uuid4(),
        taxonomy_version_id=taxonomy.id,
        key=key,
        label=key.title(),
        status="active",
        sort_order=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _scope(library: Library) -> tuple[Document, DocumentRevision]:
    document_id, revision_id = uuid.uuid4(), uuid.uuid4()
    document = Document(
        id=document_id,
        library_id=library.id,
        title="Document",
        content_hash=HASH,
        current_revision=1,
        current_revision_id=revision_id,
        latest_revision_id=revision_id,
        status="ready",
        created_at=NOW,
        updated_at=NOW,
    )
    revision = DocumentRevision(
        id=revision_id,
        document_id=document_id,
        library_id=library.id,
        revision_no=1,
        title="Document",
        content_hash=HASH,
        normalized_text="A contract about payment obligations.",
        parser_name="test",
        parser_version="1",
        chunking_strategy="fixed",
        chunking_strategy_version="1",
        visibility_scope="internal",
        security_level="internal",
        status="ready",
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW,
    )
    return document, revision


def test_0036_orm_migration_and_offline_sql_are_reversible():
    assert DocumentClassificationJob.__table__.columns.claimed_by.type.length == 160
    assert DocumentClassificationJob.__table__.columns.result_run_id.nullable
    checks = {
        item.name
        for item in DocumentClassificationJob.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    assert "ck_document_classification_jobs_claim_state" in checks
    assert "ck_document_classification_jobs_result_state" in checks
    assert "ck_lib_classification_security_levels_array" in {
        item.name
        for item in Library.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0052"]
    assert script.get_revision("0036").down_revision == "0035"
    upgrade = _offline("upgrade", "0035:0036")
    downgrade = _offline("downgrade", "0036:0035")
    assert "create table document_classification_jobs" in upgrade
    assert "classification_auto_enabled boolean default false not null" in upgrade
    assert "classification_worker" in upgrade
    assert "drop table document_classification_jobs" in downgrade
    assert "drop column classification_auto_enabled" in downgrade
    assert "delete from service_heartbeats" in downgrade
    assert "update documents" not in upgrade + downgrade


def test_startup_is_default_off_and_dependency_provider_limits_fail_closed():
    assert settings.classification_runtime_enabled is False
    validate_classification_runtime_startup(Settings(_env_file=None))
    with pytest.raises(RuntimeError, match="requires decisions"):
        validate_classification_runtime_startup(
            Settings(_env_file=None, classification_runtime_enabled=True)
        )
    with pytest.raises(RuntimeError, match="auto trigger"):
        validate_classification_runtime_startup(
            _settings(
                classification_auto_trigger_enabled=True,
                classification_external_model_enabled=False,
            )
        )
    with pytest.raises(RuntimeError, match="source limit"):
        validate_classification_runtime_startup(
            _settings(classification_model_max_source_chars=999)
        )
    with pytest.raises(RuntimeError, match="less than half"):
        validate_classification_runtime_startup(
            _settings(
                classification_worker_lease_seconds=60,
                classification_worker_renew_seconds=30,
                classification_provider_timeout_seconds=20,
            )
        )
    with pytest.raises(RuntimeError, match="approved provider identity"):
        validate_classification_runtime_startup(
            _settings(classification_model="other-model")
        )
    with pytest.raises(RuntimeError, match="CLASSIFICATION_API_KEY"):
        validate_classification_runtime_startup(
            _settings(classification_api_key=SecretStr(""))
        )
    validate_classification_runtime_startup(
        _settings(classification_auto_trigger_enabled=True)
    )


def test_job_identity_is_deterministic_strict_and_retry_scoped():
    library_id, document_id, revision_id, taxonomy_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    label_ids = (uuid.uuid4(), uuid.uuid4())
    first = classification_job_identity(
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        revision_content_hash=HASH,
        taxonomy_version_id=taxonomy_id,
        enabled_label_ids=label_ids,
        retry_generation=0,
        config=_settings(),
    )
    assert first == classification_job_identity(
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        revision_content_hash=HASH,
        taxonomy_version_id=taxonomy_id,
        enabled_label_ids=label_ids,
        retry_generation=0,
        config=_settings(),
    )
    retry = classification_job_identity(
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        revision_content_hash=HASH,
        taxonomy_version_id=taxonomy_id,
        enabled_label_ids=label_ids,
        retry_generation=1,
        config=_settings(),
    )
    assert retry.input_fingerprint == first.input_fingerprint
    assert retry.idempotency_key != first.idempotency_key
    with pytest.raises(ClassificationRuntimeError) as bad_hash:
        classification_job_identity(
            library_id=library_id,
            document_id=document_id,
            document_revision_id=revision_id,
            revision_content_hash="bad",
            taxonomy_version_id=taxonomy_id,
            enabled_label_ids=label_ids,
            retry_generation=0,
            config=_settings(),
        )
    assert bad_hash.value.code == "classification_hash_invalid"


def test_provider_output_is_strict_and_maps_known_and_unknown_candidates():
    primary_id, secondary_id = uuid.uuid4(), uuid.uuid4()
    content = json.dumps(
        {
            "primary_candidates": [
                {"label_key": "contract", "confidence_micros": 950_000},
                {
                    "proposed_key": "new-risk",
                    "proposed_label": "New Risk",
                    "confidence_micros": 800_000,
                },
            ],
            "secondary_candidates": [
                {"label_key": "urgent", "confidence_micros": 910_000}
            ],
        }
    )
    proposals = parse_classifier_output(
        content,
        labels_by_key={"contract": primary_id, "urgent": secondary_id},
    )
    assert [(item.role, item.label_id) for item in proposals] == [
        ("primary", primary_id),
        ("primary", None),
        ("secondary", secondary_id),
    ]
    assert proposals[1].proposed_key == "new-risk"
    invalid_values = (
        {"primary_candidates": [], "secondary_candidates": []},
        {
            "primary_candidates": [
                {"label_key": "contract", "confidence_micros": 0.95}
            ],
            "secondary_candidates": [],
        },
        {
            "primary_candidates": [
                {"label_key": "contract", "confidence_micros": 950_000},
                {"label_key": "contract", "confidence_micros": 900_000},
            ],
            "secondary_candidates": [],
        },
        {
            "primary_candidates": [
                {
                    "label_key": "contract",
                    "confidence_micros": 950_000,
                    "raw_reason": "forbidden",
                }
            ],
            "secondary_candidates": [],
        },
    )
    for value in invalid_values:
        with pytest.raises(ClassificationRuntimeError) as invalid:
            parse_classifier_output(
                json.dumps(value),
                labels_by_key={"contract": primary_id},
            )
        assert invalid.value.code == "provider_invalid_output"
    with pytest.raises(ClassificationRuntimeError) as unknown:
        parse_classifier_output(
            json.dumps(
                {
                    "primary_candidates": [
                        {"label_key": "not-known", "confidence_micros": 950_000}
                    ],
                    "secondary_candidates": [],
                }
            ),
            labels_by_key={"contract": primary_id},
        )
    assert unknown.value.code == "provider_unknown_label_key"


def test_provider_identity_allows_deepseek_and_local_qwen_only():
    assert classification_provider_name(_settings()) == "deepseek"
    assert classification_provider_name(
        _settings(
            classification_base_url="http://10.0.10.2:8113/v1",
            classification_model="qwen3.5-9b",
        )
    ) == "openai-compatible"
    with pytest.raises(ClassificationRuntimeError) as denied:
        classification_provider_name(
            _settings(
                classification_base_url="http://unapproved.invalid/v1",
                classification_model="unknown",
            )
        )
    assert denied.value.code == "provider_config_invalid"

def test_messages_are_bounded_structured_and_contain_no_credentials():
    messages = classification_messages(
        "source text",
        enabled_labels=(("contract", "Contract", "Agreement"),),
    )
    assert [item["role"] for item in messages] == ["system", "user"]
    payload = json.loads(messages[1]["content"])
    assert payload["document_text"] == "source text"
    assert payload["selectable_labels"][0]["key"] == "contract"
    assert "95% is 950000, never 9500000" in messages[0]["content"]
    assert "unique across both arrays" in messages[0]["content"]
    assert "first primary candidate must use a selectable" in messages[0]["content"]
    assert "Do not propose new labels" in messages[0]["content"]
    assert "api_key" not in messages[1]["content"].lower()


def test_scope_policy_requires_every_library_and_security_gate():
    organization = _organization()
    library = _library(organization)
    document, revision = _scope(library)
    validate_classification_scope(
        library=library,
        document=document,
        revision=revision,
        auto_trigger=True,
        config=_settings(classification_auto_trigger_enabled=True),
    )
    library.classification_allowed_security_levels = []
    with pytest.raises(ClassificationRuntimeError) as denied:
        validate_classification_scope(
            library=library,
            document=document,
            revision=revision,
            auto_trigger=True,
            config=_settings(classification_auto_trigger_enabled=True),
        )
    assert denied.value.code == "security_allowlist_empty"
    library.classification_allowed_security_levels = ["internal"]
    document.current_revision_id = uuid.uuid4()
    with pytest.raises(ClassificationRuntimeError) as historical:
        validate_classification_scope(
            library=library,
            document=document,
            revision=revision,
            auto_trigger=True,
            config=_settings(classification_auto_trigger_enabled=True),
        )
    assert historical.value.code == "historical_revision"


def test_security_level_normalization_and_admin_schema_are_strict():
    assert normalize_classification_security_levels(
        [" internal ", "restricted", "internal"]
    ) == ["internal", "restricted"]
    body = LibraryUpdate(
        classification_auto_enabled=True,
        classification_external_model_enabled=True,
        classification_allowed_security_levels=[" restricted ", "internal"],
    )
    assert body.classification_allowed_security_levels == ["internal", "restricted"]
    with pytest.raises(ValidationError):
        LibraryUpdate(classification_auto_enabled=None)
    with pytest.raises(ValidationError):
        LibraryUpdate(classification_allowed_security_levels=[1])


def test_provider_success_and_errors_are_bounded_without_raw_payload_storage():
    async def success(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={
                "id": "request-1",
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    provider = OpenAICompatibleClassificationProvider(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-pro",
        api_key="secret",
        transport=httpx.MockTransport(success),
    )
    response = asyncio.run(
        provider.generate([{"role": "user", "content": "source"}])
    )
    assert response.content == '{"ok":true}'
    assert response.provider_request_id == "request-1"
    assert response.input_token_count == 3
    assert "secret" not in repr(response)

    async def failure(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="raw provider secret")

    failing = OpenAICompatibleClassificationProvider(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-pro",
        api_key="secret",
        transport=httpx.MockTransport(failure),
    )
    with pytest.raises(ClassificationProviderError) as exc_info:
        asyncio.run(failing.generate([{"role": "user", "content": "source"}]))
    assert exc_info.value.category == "http_error"
    assert "raw provider secret" not in str(exc_info.value)


def test_enqueue_is_idempotent_and_service_never_commits(monkeypatch):
    organization = _organization()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _scope(library)
    label = _label(taxonomy, "contract")
    scope = AsyncMock(return_value=(revision.normalized_text, taxonomy, (label,), {label.id: label}))
    monkeypatch.setattr(jobs, "_classification_scope", scope)
    db = _DB(_Result(rows=()))
    result = asyncio.run(
        jobs._enqueue_classification_job_result(
            db,
            library=library,
            document=document,
            revision=revision,
            trigger_type="revision_ready",
            auto_trigger=True,
            config=_settings(classification_auto_trigger_enabled=True),
        )
    )
    assert result.created is True and result.job.status == "queued"
    assert result.job.taxonomy_version_id == taxonomy.id
    assert db.flush_count == 1
    db.commit.assert_not_awaited()
    idempotent_db = _DB(_Result(rows=(result.job,)))
    again = asyncio.run(
        jobs._enqueue_classification_job_result(
            idempotent_db,
            library=library,
            document=document,
            revision=revision,
            trigger_type="revision_ready",
            auto_trigger=True,
            config=_settings(classification_auto_trigger_enabled=True),
        )
    )
    assert again.created is False and again.job is result.job
    assert not idempotent_db.added
    idempotent_db.commit.assert_not_awaited()


def test_retry_is_linked_immutable_and_rejects_changed_identity(monkeypatch):
    organization = _organization()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _scope(library)
    label = _label(taxonomy, "contract")
    config = _settings()
    source = build_classification_job(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_ids=(label.id,),
        trigger_type="revision_ready",
        config=config,
    )
    source.status = "failed"
    source.error_code = "provider_timeout"
    source.error_message = "classification generation did not complete"
    source.finished_at = NOW
    monkeypatch.setattr(
        jobs,
        "_classification_scope",
        AsyncMock(return_value=(revision.normalized_text, taxonomy, (label,), {label.id: label})),
    )
    retried = asyncio.run(
        jobs.retry_classification_job(
            _DB(_Result(rows=())),
            source_job=source,
            library=library,
            document=document,
            revision=revision,
            requested_by_user_id=uuid.uuid4(),
            config=config,
        )
    )
    assert source.status == "failed"
    assert retried.status == "queued" and retried.retry_generation == 1
    assert retried.rerun_of_job_id == source.id
    assert retried.input_fingerprint == source.input_fingerprint
    with pytest.raises(ClassificationRuntimeError) as stale:
        asyncio.run(
            jobs.retry_classification_job(
                _DB(),
                source_job=source,
                library=library,
                document=document,
                revision=revision,
                requested_by_user_id=None,
                config=_settings(classification_prompt_version="classification-prompt-v4"),
            )
        )
    assert stale.value.code == "classification_job_identity_stale"

    collision = build_classification_job(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_ids=(label.id,),
        trigger_type="retry",
        retry_generation=1,
        rerun_of_job_id=source.id,
        config=config,
    )
    collision.model_name = "wrong-model"
    with pytest.raises(ClassificationRuntimeError) as conflict:
        asyncio.run(
            jobs.retry_classification_job(
                _DB(_Result(rows=(collision,))),
                source_job=source,
                library=library,
                document=document,
                revision=revision,
                requested_by_user_id=None,
                config=config,
            )
        )
    assert conflict.value.code == "classification_idempotency_conflict"


def test_claim_renew_and_expiry_recovery_are_fenced():
    organization = _organization()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _scope(library)
    label = _label(taxonomy, "contract")
    job = build_classification_job(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_ids=(label.id,),
        trigger_type="manual",
        config=_settings(),
    )
    claim_db = _DB(_Result(rowcount=0), _Result(rows=(job,)))
    claimed = asyncio.run(
        jobs.claim_classification_job(
            claim_db,
            worker_id="worker-1",
            lease_seconds=180,
            max_attempts=3,
            now=NOW,
        )
    )
    assert claimed is not None and job.status == "processing"
    assert job.attempt_count == 1 and job.lease_expires_at == NOW + timedelta(seconds=180)
    claim_sql = str(
        claim_db.statements[1].compile(dialect=postgresql.dialect())
    ).lower()
    assert "skip locked" in claim_sql
    renew_db = _DB(_Result(rowcount=1))
    assert asyncio.run(
        jobs.renew_classification_lease(
            renew_db,
            job_id=job.id,
            claim_token=claimed.claim_token,
            lease_seconds=180,
            now=NOW + timedelta(seconds=20),
        )
    )
    job.lease_expires_at = NOW - timedelta(seconds=1)
    recovery_db = _DB(_Result(rows=(job,)))
    recovery = asyncio.run(
        jobs.recover_stale_classification_jobs(
            recovery_db,
            max_attempts=3,
            now=NOW,
        )
    )
    assert recovery.requeued == 1 and recovery.failed == 0
    assert job.status == "queued" and job.claim_token is None
    assert recovery_db.flush_count == 1


def test_library_policy_change_cancels_live_classification_jobs(monkeypatch):
    organization = _organization()
    library = _library(organization)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_Result(rows=(library,))),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    actor = SimpleNamespace(id=uuid.uuid4())
    cancel = AsyncMock(return_value=3)
    monkeypatch.setattr(
        admin_libraries.classification_jobs,
        "cancel_library_classification_jobs_for_policy_change",
        cancel,
    )
    monkeypatch.setattr(admin_libraries.audit_log, "record", AsyncMock())
    result = asyncio.run(
        admin_libraries.update_library(
            library.slug,
            LibraryUpdate(
                classification_allowed_security_levels=["restricted"],
            ),
            actor,
            db,
        )
    )
    assert result.classification_allowed_security_levels == ["restricted"]
    cancel.assert_awaited_once_with(
        db,
        library_id=library.id,
        error_code="library_classification_policy_changed",
    )
    db.commit.assert_awaited_once()


def test_terminal_attempt_failure_remains_discoverable_for_governance():
    organization = _organization()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _scope(library)
    label = _label(taxonomy, "contract")
    job = build_classification_job(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_ids=(label.id,),
        trigger_type="manual",
        config=_settings(),
    )
    job.status = "processing"
    job.attempt_count = 3
    job.claim_token = uuid.uuid4()
    job.claimed_by = "worker"
    job.lease_expires_at = NOW - timedelta(seconds=1)
    job.last_heartbeat_at = NOW - timedelta(minutes=1)
    recovery = asyncio.run(
        jobs.recover_stale_classification_jobs(
            _DB(_Result(rows=(job,))),
            max_attempts=3,
            now=NOW,
        )
    )
    assert recovery.failed_job_ids == (job.id,)
    assert job.status == "failed" and job.result_run_id is None
    assert job.error_code == "lease_expired_attempt_limit"

    lookup_db = _DB(_Result(rows=(job.id,)))
    pending = asyncio.run(jobs.list_unrecorded_attempt_failure_job_ids(lookup_db))
    assert pending == (job.id,)
    compiled = lookup_db.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled).lower()
    assert "result_run_id is null" in sql
    bound_values = {
        item
        for value in compiled.params.values()
        if isinstance(value, (list, tuple))
        for item in value
    }
    assert "lease_expired_attempt_limit" in bound_values


def test_compensation_paginates_past_existing_jobs_and_counts_only_new(monkeypatch):
    class _PageSession:
        def __init__(self, factory):
            self.factory = factory

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            self.factory.statements.append(statement)
            return _Result(rows=self.factory.pages.pop(0))

    class _PageFactory:
        def __init__(self, pages):
            self.pages = list(pages)
            self.statements = []

        def __call__(self):
            return _PageSession(self)

    library_id = uuid.uuid4()
    first_page = tuple(
        (
            library_id,
            uuid.UUID(int=index + 1),
            uuid.UUID(int=index + 1_000),
            NOW,
        )
        for index in range(100)
    )
    last_scope = (
        library_id,
        uuid.UUID(int=101),
        uuid.UUID(int=1_101),
        NOW,
    )
    factory = _PageFactory((first_page, (last_scope,)))
    enqueue = AsyncMock(
        side_effect=[*(() for _ in range(100)), (uuid.uuid4(),)]
    )
    monkeypatch.setattr(jobs, "enqueue_ready_revision_classification", enqueue)
    created = asyncio.run(
        jobs.compensate_ready_revision_classifications(
            limit=1,
            session_factory=factory,
            config=_settings(classification_auto_trigger_enabled=True),
        )
    )
    assert created == 1 and enqueue.await_count == 101
    assert len(factory.statements) == 2
    second_sql = str(
        factory.statements[1].compile(dialect=postgresql.dialect())
    ).lower()
    assert "documents.updated_at >" in second_sql
