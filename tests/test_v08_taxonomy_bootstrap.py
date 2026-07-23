from __future__ import annotations

import asyncio
import inspect
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import CheckConstraint

from app.config import (
    Settings,
    settings,
    validate_classification_runtime_startup,
    validate_classification_taxonomy_bootstrap_startup,
)
from app.models.classification_taxonomy import ClassificationLabel, ClassificationTaxonomy
from app.models.classification_taxonomy_bootstrap import (
    TaxonomyBootstrapRun,
    TaxonomyBootstrapSource,
)
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.services import classification_taxonomy_bootstrap as service
from app.services.classification_taxonomy_bootstrap_contracts import (
    ApplyBuiltinTemplateCommand,
    DraftLabelInput,
    ImportTaxonomyBootstrapCommand,
    LlmTaxonomyProposal,
    PrepareLlmTaxonomyBootstrapCommand,
    TaxonomyBootstrapError,
    normalize_taxonomy_draft,
    parse_llm_taxonomy_bootstrap_output,
    taxonomy_bootstrap_model_config_hash,
)
from app.services.classification_taxonomy_templates import (
    GENERAL_ENTERPRISE_V1,
    list_taxonomy_bootstrap_templates,
)


NOW = datetime(2026, 7, 22, 23, 0, tzinfo=timezone.utc)
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

    def scalar_one_or_none(self):
        if self.scalar is not None:
            return self.scalar
        return self.rows[0] if self.rows else None


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

    def add_all(self, values):
        self.added.extend(list(values))

    async def flush(self):
        self.flush_count += 1

    async def get(self, model, identity):
        return next(
            (
                value
                for value in self.added
                if isinstance(value, model) and value.id == identity
            ),
            None,
        )


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
        "classification_taxonomy_bootstrap_enabled": True,
        "classification_taxonomy_bootstrap_llm_enabled": True,
        "classification_external_model_enabled": True,
        "classification_api_key": SecretStr("test-key"),
    }
    return Settings(_env_file=None, **{**defaults, **values})


def _library(organization_id: uuid.UUID) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization_id,
        slug=f"lib-{uuid.uuid4().hex[:8]}",
        name="Library",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection=f"lib_{uuid.uuid4().hex[:8]}",
        index_state="ready",
        external_llm_enabled=True,
        classification_external_model_enabled=True,
        classification_allowed_security_levels=["internal"],
        created_at=NOW,
    )


def _sample_scope(organization_id: uuid.UUID):
    library = _library(organization_id)
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
        normalized_text="A contract, payroll report, and safety inspection.",
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
    return library, document, revision


def _processing_run(
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    config: Settings | None = None,
    expires_at: datetime | None = None,
) -> TaxonomyBootstrapRun:
    config = config or _settings()
    return TaxonomyBootstrapRun(
        id=uuid.uuid4(),
        organization_id=organization_id,
        request_id=uuid.uuid4(),
        source_type="llm_proposal",
        source_key="organization-revision-samples",
        source_version="1",
        source_hash=HASH,
        input_fingerprint="b" * 64,
        idempotency_key="c" * 64,
        status="processing",
        warning_items=[],
        model_provider="deepseek",
        model_name="deepseek-v4-pro",
        model_config_hash=taxonomy_bootstrap_model_config_hash(config),
        prompt_version="taxonomy-bootstrap-v1",
        attempt_token=uuid.uuid4(),
        expires_at=expires_at or NOW + timedelta(minutes=2),
        created_by_user_id=actor_id,
        started_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def test_0037_orm_migration_and_offline_sql_are_reversible():
    run_checks = {
        item.name
        for item in TaxonomyBootstrapRun.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    source_checks = {
        item.name
        for item in TaxonomyBootstrapSource.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    assert "ck_taxonomy_bootstrap_runs_state" in run_checks
    assert "ck_taxonomy_bootstrap_runs_model" in run_checks
    assert "ck_taxonomy_bootstrap_sources_values" in source_checks
    names = [
        item.name
        for table in (TaxonomyBootstrapRun.__table__, TaxonomyBootstrapSource.__table__)
        for item in (*table.constraints, *table.indexes)
        if item.name
    ]
    assert max(map(len, names)) <= 63
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0042"]
    assert script.get_revision("0037").down_revision == "0036"
    upgrade = _offline("upgrade", "0036:0037")
    downgrade = _offline("downgrade", "0037:0036")
    assert "create table classification_taxonomy_bootstrap_runs" in upgrade
    assert "create table classification_taxonomy_bootstrap_sources" in upgrade
    assert "where status = 'processing'" in upgrade
    assert "drop table classification_taxonomy_bootstrap_sources" in downgrade
    assert "drop table classification_taxonomy_bootstrap_runs" in downgrade
    assert "update classification_taxonomies" not in upgrade + downgrade


def test_bootstrap_startup_is_default_off_and_llm_dependencies_fail_closed():
    assert settings.classification_taxonomy_bootstrap_enabled is False
    assert settings.classification_taxonomy_bootstrap_llm_enabled is False
    validate_classification_taxonomy_bootstrap_startup(Settings(_env_file=None))
    with pytest.raises(RuntimeError, match="requires Organization"):
        validate_classification_taxonomy_bootstrap_startup(
            Settings(
                _env_file=None,
                classification_taxonomy_bootstrap_enabled=True,
            )
        )
    with pytest.raises(RuntimeError, match="requires bootstrap"):
        validate_classification_taxonomy_bootstrap_startup(
            Settings(
                _env_file=None,
                classification_taxonomy_bootstrap_llm_enabled=True,
            )
        )
    valid = _settings()
    validate_classification_runtime_startup(valid)
    validate_classification_taxonomy_bootstrap_startup(valid)
    with pytest.raises(RuntimeError, match="sample limit"):
        validate_classification_taxonomy_bootstrap_startup(
            _settings(classification_taxonomy_bootstrap_max_samples=21)
        )
    with pytest.raises(RuntimeError, match="must exceed provider timeout"):
        validate_classification_taxonomy_bootstrap_startup(
            _settings(classification_taxonomy_bootstrap_attempt_seconds=120)
        )


def test_disabled_bootstrap_does_not_reject_an_existing_classifier_timeout():
    config = _settings(
        classification_runtime_enabled=True,
        classification_decision_enabled=True,
        classification_taxonomy_bootstrap_enabled=False,
        classification_taxonomy_bootstrap_llm_enabled=False,
        classification_provider_timeout_seconds=200,
        classification_worker_lease_seconds=300,
    )
    validate_classification_runtime_startup(config)
    validate_classification_taxonomy_bootstrap_startup(config)


def test_normalized_draft_validates_hierarchy_duplicates_cycles_and_warnings():
    draft = normalize_taxonomy_draft(
        (
            DraftLabelInput("child", "Finance Reports", parent_key="parent", sort_order=2),
            DraftLabelInput("parent", "Finance Report", sort_order=1),
        )
    )
    assert [label.key for label in draft.labels] == ["parent", "child"]
    assert draft.warnings[0].code == "near_duplicate_label"
    assert len(draft.payload_hash) == 64
    with pytest.raises(TaxonomyBootstrapError) as duplicate:
        normalize_taxonomy_draft(
            (
                DraftLabelInput("first", "Same"),
                DraftLabelInput("second", "same"),
            )
        )
    assert duplicate.value.code == "bootstrap_label_duplicate"
    with pytest.raises(TaxonomyBootstrapError) as cycle:
        normalize_taxonomy_draft(
            (
                DraftLabelInput("first", "First", parent_key="second"),
                DraftLabelInput("second", "Second", parent_key="first"),
            )
        )
    assert cycle.value.code == "bootstrap_parent_cycle"
    with pytest.raises(TaxonomyBootstrapError) as disabled:
        normalize_taxonomy_draft(
            (
                DraftLabelInput("parent", "Parent", status="disabled"),
                DraftLabelInput("child", "Child", parent_key="parent"),
            )
        )
    assert disabled.value.code == "bootstrap_parent_disabled"


def test_template_registry_and_llm_output_are_frozen_and_strict():
    assert list_taxonomy_bootstrap_templates() == (GENERAL_ENTERPRISE_V1,)
    assert len(GENERAL_ENTERPRISE_V1.draft.labels) == 8
    assert (
        GENERAL_ENTERPRISE_V1.template_hash
        == "a8b032abbbbc4ff4c2049464843339d7b02b049e43548cf1e69449038595d67c"
    )
    proposal = parse_llm_taxonomy_bootstrap_output(
        json.dumps(
            {
                "description": "Suggested taxonomy",
                "labels": [
                    {"key": "legal", "label": "Legal", "sort_order": 10},
                    {"key": "finance", "label": "Finance", "sort_order": 20},
                    {"key": "safety", "label": "Safety", "sort_order": 30},
                ],
            }
        )
    )
    assert proposal.description == "Suggested taxonomy"
    assert [label.key for label in proposal.draft.labels] == ["legal", "finance", "safety"]
    invalid = {
        "labels": [
            {"key": "legal", "label": "Legal", "sort_order": 1.5},
            {"key": "finance", "label": "Finance", "sort_order": 20},
            {"key": "safety", "label": "Safety", "sort_order": 30},
        ]
    }
    with pytest.raises(TaxonomyBootstrapError) as exc_info:
        parse_llm_taxonomy_bootstrap_output(json.dumps(invalid))
    assert exc_info.value.code == "provider_invalid_output"


def test_persist_draft_assigns_parent_ids_and_never_commits():
    organization_id, actor_id = uuid.uuid4(), uuid.uuid4()
    draft = normalize_taxonomy_draft(
        (
            DraftLabelInput("parent", "Parent", sort_order=1),
            DraftLabelInput("child", "Child", parent_key="parent", sort_order=2),
        )
    )
    db = _DB()
    taxonomy, labels = asyncio.run(
        service._persist_draft(
            db,
            organization_id=organization_id,
            actor_user_id=actor_id,
            version_key="document-category",
            description="Draft",
            draft=draft,
        )
    )
    assert taxonomy.status == "draft" and taxonomy.version_no == 1
    by_key = {label.key: label for label in labels}
    assert by_key["child"].parent_label_id == by_key["parent"].id
    assert db.flush_count == 3
    db.commit.assert_not_awaited()


def test_builtin_template_creates_provenance_linked_draft_and_replays(monkeypatch):
    organization_id, actor_id, request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    taxonomy = ClassificationTaxonomy(
        id=uuid.uuid4(),
        organization_id=organization_id,
        version_key="document-category",
        version_no=1,
        status="draft",
        created_by_user_id=actor_id,
        created_at=NOW,
        updated_at=NOW,
    )
    labels = tuple(
        ClassificationLabel(
            id=uuid.uuid4(),
            taxonomy_version_id=taxonomy.id,
            key=item.key,
            label=item.label,
            sort_order=item.sort_order,
            status=item.status,
            created_at=NOW,
            updated_at=NOW,
        )
        for item in GENERAL_ENTERPRISE_V1.draft.labels
    )
    monkeypatch.setattr(service, "lock_classification_admin_scope", AsyncMock())
    monkeypatch.setattr(service, "_repair_expired_processing_run", AsyncMock(return_value=0))
    monkeypatch.setattr(service, "_load_request_run", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_require_initial_scope", AsyncMock())
    monkeypatch.setattr(service, "_persist_draft", AsyncMock(return_value=(taxonomy, labels)))
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    db = _DB()
    command = ApplyBuiltinTemplateCommand(
        organization_id,
        actor_id,
        request_id,
        "general-enterprise",
    )
    result = asyncio.run(service.apply_builtin_taxonomy_template(db, command, now=NOW))
    assert result.created is True and result.taxonomy is taxonomy
    assert result.run.output_taxonomy_id == taxonomy.id
    assert result.run.status == "succeeded"
    assert result.run.source_hash == GENERAL_ENTERPRISE_V1.template_hash
    db.commit.assert_not_awaited()

    monkeypatch.setattr(service, "_load_request_run", AsyncMock(return_value=result.run))
    monkeypatch.setattr(service, "_load_output", AsyncMock(return_value=(taxonomy, labels)))
    again = asyncio.run(service.apply_builtin_taxonomy_template(_DB(), command, now=NOW))
    assert again.created is False and again.run is result.run


def test_admin_import_creates_draft_replays_and_rejects_identity_collision(monkeypatch):
    organization_id, actor_id, request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(service, "lock_classification_admin_scope", AsyncMock())
    monkeypatch.setattr(service, "_repair_expired_processing_run", AsyncMock(return_value=0))
    load_request = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "_load_request_run", load_request)
    monkeypatch.setattr(service, "_require_initial_scope", AsyncMock())
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    command = ImportTaxonomyBootstrapCommand(
        organization_id,
        actor_id,
        request_id,
        "Administrator starter",
        "2026.07",
        (
            DraftLabelInput("parent", "Policies", sort_order=10),
            DraftLabelInput(
                "child",
                "Policy records",
                parent_key="parent",
                sort_order=20,
            ),
        ),
    )
    db = _DB()
    result = asyncio.run(service.import_taxonomy_bootstrap(db, command, now=NOW))

    assert result.created is True
    assert result.run.source_type == "admin_import"
    assert result.run.source_key == "Administrator starter"
    assert result.run.status == "succeeded"
    assert result.taxonomy is not None and result.taxonomy.status == "draft"
    by_key = {label.key: label for label in result.labels}
    assert by_key["child"].parent_label_id == by_key["parent"].id
    db.commit.assert_not_awaited()

    load_request.return_value = result.run
    monkeypatch.setattr(
        service,
        "_load_output",
        AsyncMock(return_value=(result.taxonomy, result.labels)),
    )
    replay = asyncio.run(service.import_taxonomy_bootstrap(_DB(), command, now=NOW))
    assert replay.created is False and replay.run is result.run

    changed = ImportTaxonomyBootstrapCommand(
        organization_id,
        actor_id,
        request_id,
        command.source_name,
        "2026.08",
        command.labels,
    )
    with pytest.raises(TaxonomyBootstrapError) as exc_info:
        asyncio.run(service.import_taxonomy_bootstrap(_DB(), changed, now=NOW))
    assert exc_info.value.code == "bootstrap_idempotency_conflict"


def test_expired_processing_repair_and_live_attempt_exclusivity_never_commit():
    organization_id, actor_id = uuid.uuid4(), uuid.uuid4()
    run = _processing_run(
        organization_id,
        actor_id,
        expires_at=NOW - timedelta(seconds=1),
    )
    repair_db = _DB(_Result(rows=(run,)))
    repaired = asyncio.run(
        service._repair_expired_processing_run(
            repair_db,
            organization_id=organization_id,
            now=NOW,
        )
    )
    assert repaired == 1
    assert run.status == "failed"
    assert run.error_code == "bootstrap_attempt_expired"
    assert run.attempt_token is None and run.expires_at is None
    repair_db.commit.assert_not_awaited()

    live_run_id = uuid.uuid4()
    scope_db = _DB(_Result(), _Result(scalar=live_run_id))
    with pytest.raises(TaxonomyBootstrapError) as exc_info:
        asyncio.run(
            service._require_initial_scope(
                scope_db,
                organization_id=organization_id,
            )
        )
    assert exc_info.value.code == "bootstrap_attempt_in_progress"
    scope_db.commit.assert_not_awaited()


def test_llm_prepare_persists_only_snapshots_and_bounded_messages(monkeypatch):
    organization_id, actor_id, request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    library, document, revision = _sample_scope(organization_id)
    config = _settings(classification_taxonomy_bootstrap_max_source_chars=1_000)
    monkeypatch.setattr(
        service,
        "_load_sample_scope",
        AsyncMock(return_value=((document,), (library,), (revision,))),
    )
    monkeypatch.setattr(service, "_repair_expired_processing_run", AsyncMock(return_value=0))
    monkeypatch.setattr(service, "_load_request_run", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_require_initial_scope", AsyncMock())
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    db = _DB()
    prepared = asyncio.run(
        service.prepare_llm_taxonomy_bootstrap(
            db,
            PrepareLlmTaxonomyBootstrapCommand(
                organization_id,
                actor_id,
                request_id,
                (revision.id,),
            ),
            now=NOW,
            config=config,
        )
    )
    assert prepared.reused is False and prepared.attempt_token is not None
    assert prepared.run.status == "processing"
    assert prepared.run.expires_at == NOW + timedelta(seconds=180)
    sources = [value for value in db.added if isinstance(value, TaxonomyBootstrapSource)]
    assert len(sources) == 1 and sources[0].revision_content_hash == HASH
    persisted = " ".join(repr(value) for value in db.added)
    assert revision.normalized_text not in persisted
    payload = json.loads(prepared.messages[1]["content"])
    assert len(payload["authorized_document_samples"][0]["text"]) <= 1_000
    db.commit.assert_not_awaited()


def test_llm_publish_revalidates_snapshots_and_links_draft(monkeypatch):
    organization_id, actor_id = uuid.uuid4(), uuid.uuid4()
    library, document, revision = _sample_scope(organization_id)
    config = _settings()
    run = TaxonomyBootstrapRun(
        id=uuid.uuid4(),
        organization_id=organization_id,
        request_id=uuid.uuid4(),
        source_type="llm_proposal",
        source_key="organization-revision-samples",
        source_version="1",
        source_hash=HASH,
        input_fingerprint="b" * 64,
        idempotency_key="c" * 64,
        status="processing",
        warning_items=[],
        model_provider="deepseek",
        model_name="deepseek-v4-pro",
        model_config_hash=taxonomy_bootstrap_model_config_hash(config),
        prompt_version="taxonomy-bootstrap-v1",
        attempt_token=uuid.uuid4(),
        expires_at=NOW + timedelta(minutes=2),
        created_by_user_id=actor_id,
        started_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    snapshot = service.BootstrapSampleSnapshot(
        library.id,
        document.id,
        revision.id,
        revision.content_hash,
        revision.security_level,
    )
    prepared = service.PreparedLlmTaxonomyBootstrap(
        run,
        run.attempt_token,
        actor_id,
        "document-category",
        None,
        (snapshot,),
        [],
        False,
    )
    proposal = LlmTaxonomyProposal(
        "Suggested taxonomy",
        normalize_taxonomy_draft(
            (
                DraftLabelInput("legal", "Legal", sort_order=10),
                DraftLabelInput("finance", "Finance", sort_order=20),
                DraftLabelInput("safety", "Safety", sort_order=30),
            ),
            minimum=3,
            maximum=50,
        ),
    )
    taxonomy = ClassificationTaxonomy(
        id=uuid.uuid4(),
        organization_id=organization_id,
        version_key="document-category",
        version_no=1,
        status="draft",
        created_by_user_id=actor_id,
        created_at=NOW,
        updated_at=NOW,
    )
    labels = tuple(
        ClassificationLabel(
            id=uuid.uuid4(),
            taxonomy_version_id=taxonomy.id,
            key=item.key,
            label=item.label,
            sort_order=item.sort_order,
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
        for item in proposal.draft.labels
    )
    monkeypatch.setattr(
        service,
        "_load_sample_scope",
        AsyncMock(return_value=((document,), (library,), (revision,))),
    )
    monkeypatch.setattr(service, "_require_initial_scope", AsyncMock())
    monkeypatch.setattr(service, "_lock_run_by_attempt", AsyncMock(return_value=run))
    monkeypatch.setattr(service, "_persist_draft", AsyncMock(return_value=(taxonomy, labels)))
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    db = _DB()
    result = asyncio.run(
        service.publish_llm_taxonomy_bootstrap(
            db,
            prepared=prepared,
            proposal=proposal,
            now=NOW + timedelta(seconds=30),
            config=config,
        )
    )
    assert result.run.status == "succeeded"
    assert result.run.output_taxonomy_id == taxonomy.id
    assert result.run.attempt_token is None and result.taxonomy.status == "draft"
    db.commit.assert_not_awaited()


def test_llm_publish_rejects_revision_and_library_policy_drift(monkeypatch):
    organization_id, actor_id = uuid.uuid4(), uuid.uuid4()
    library, document, revision = _sample_scope(organization_id)
    config = _settings()
    run = _processing_run(organization_id, actor_id, config=config)
    snapshot = service.BootstrapSampleSnapshot(
        library.id,
        document.id,
        revision.id,
        revision.content_hash,
        revision.security_level,
    )
    prepared = service.PreparedLlmTaxonomyBootstrap(
        run,
        run.attempt_token,
        actor_id,
        "document-category",
        None,
        (snapshot,),
        [],
        False,
    )
    proposal = LlmTaxonomyProposal(
        None,
        normalize_taxonomy_draft(
            (
                DraftLabelInput("legal", "Legal", sort_order=10),
                DraftLabelInput("finance", "Finance", sort_order=20),
                DraftLabelInput("safety", "Safety", sort_order=30),
            ),
            minimum=3,
            maximum=50,
        ),
    )
    persist = AsyncMock()
    monkeypatch.setattr(service, "_persist_draft", persist)
    revision.content_hash = "f" * 64
    monkeypatch.setattr(
        service,
        "_load_sample_scope",
        AsyncMock(return_value=((document,), (library,), (revision,))),
    )
    with pytest.raises(TaxonomyBootstrapError) as revision_exc:
        asyncio.run(
            service.publish_llm_taxonomy_bootstrap(
                _DB(),
                prepared=prepared,
                proposal=proposal,
                now=NOW + timedelta(seconds=30),
                config=config,
            )
        )
    assert revision_exc.value.code == "bootstrap_sample_revision_stale"
    persist.assert_not_awaited()

    monkeypatch.setattr(
        service,
        "_load_sample_scope",
        AsyncMock(side_effect=TaxonomyBootstrapError("bootstrap_library_model_disabled")),
    )
    with pytest.raises(TaxonomyBootstrapError) as policy_exc:
        asyncio.run(
            service.publish_llm_taxonomy_bootstrap(
                _DB(),
                prepared=prepared,
                proposal=proposal,
                now=NOW + timedelta(seconds=30),
                config=config,
            )
        )
    assert policy_exc.value.code == "bootstrap_library_model_disabled"
    persist.assert_not_awaited()


def test_llm_failure_is_sanitized_and_does_not_rewrite_terminal_run(monkeypatch):
    token = uuid.uuid4()
    run = TaxonomyBootstrapRun(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        source_type="llm_proposal",
        source_key="organization-revision-samples",
        source_version="1",
        source_hash=HASH,
        input_fingerprint="b" * 64,
        idempotency_key="c" * 64,
        status="processing",
        warning_items=[],
        model_provider="deepseek",
        model_name="deepseek-v4-pro",
        model_config_hash="d" * 64,
        prompt_version="taxonomy-bootstrap-v1",
        attempt_token=token,
        expires_at=NOW + timedelta(minutes=1),
        created_by_user_id=uuid.uuid4(),
        started_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    monkeypatch.setattr(service, "_lock_run_by_attempt", AsyncMock(return_value=run))
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    db = _DB()
    recorded = asyncio.run(
        service.record_llm_taxonomy_bootstrap_failure(
            db,
            run_id=run.id,
            attempt_token=token,
            error_code="provider_timeout",
            now=NOW,
        )
    )
    assert recorded is True and run.status == "failed"
    assert run.error_code == "provider_timeout" and run.attempt_token is None
    assert not hasattr(run, "raw_response")


def test_sample_scope_locks_documents_before_organization_and_libraries(monkeypatch):
    organization_id, actor_id = uuid.uuid4(), uuid.uuid4()
    library, document, revision = _sample_scope(organization_id)
    events = []

    class _OrderedDB(_DB):
        async def execute(self, statement):
            events.append(str(statement))
            return await super().execute(statement)

    async def lock_admin(*_args, **_kwargs):
        events.append("lock_admin")
        return SimpleNamespace(id=organization_id)

    monkeypatch.setattr(service, "lock_classification_admin_scope", lock_admin)
    db = _OrderedDB(
        _Result(rows=(revision,)),
        _Result(rows=(document,)),
        _Result(rows=(library,)),
        _Result(rows=(revision,)),
    )
    asyncio.run(
        service._load_sample_scope(
            db,
            organization_id=organization_id,
            actor_user_id=actor_id,
            sample_revision_ids=(revision.id,),
            config=_settings(),
        )
    )
    assert "documents" in events[1].lower()
    assert events.index("lock_admin") == 2
    assert "sys_libraries" in events[3].lower()
    assert "document_revisions" in events[4].lower()


def test_bootstrap_service_has_no_commit_or_raw_content_persistence_fields():
    assert ".commit(" not in inspect.getsource(service)
    persisted_columns = {
        column.name
        for table in (
            TaxonomyBootstrapRun.__table__,
            TaxonomyBootstrapSource.__table__,
        )
        for column in table.columns
    }
    assert persisted_columns.isdisjoint(
        {
            "raw_import",
            "sample_text",
            "sample_title",
            "prompt",
            "response",
            "credential",
            "endpoint",
            "url",
            "exception",
        }
    )
