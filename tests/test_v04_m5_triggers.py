from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import SecretStr

from app.config import settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.services.graph_extraction_triggers import (
    compensate_ready_graph_extractions,
    enqueue_ready_revision_graph_extraction,
    graph_extraction_upload_configuration,
)
from app.workers import embedder


LIB_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
DOC_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
REV_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Rows:
    def __init__(self, rows):
        self.rows = list(rows)

    def all(self):
        return list(self.rows)


class _Scalar:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class TriggerSession:
    def __init__(self, *, objects=None, rows=()):
        self.objects = objects or {}
        self.rows = list(rows)
        self.statements = []
        self.nested_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return _Transaction()

    def begin_nested(self):
        self.nested_count += 1
        return _Transaction()

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.rows)


class TriggerFactory:
    def __init__(self, session):
        self.session = session
        self.call_count = 0

    def __call__(self):
        self.call_count += 1
        return self.session


def _scope():
    library = SimpleNamespace(id=LIB_ID)
    document = SimpleNamespace(id=DOC_ID)
    revision = SimpleNamespace(id=REV_ID, created_by=None)
    return library, document, revision


def _enable_auto_trigger(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    monkeypatch.setattr(settings, "graph_extraction_auto_trigger_enabled", True)
    monkeypatch.setattr(settings, "graph_extraction_api_key", SecretStr("test-key"))


def test_auto_trigger_flags_fail_closed_without_opening_a_database_session(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", False)
    monkeypatch.setattr(settings, "graph_extraction_auto_trigger_enabled", False)
    factory = TriggerFactory(TriggerSession())

    result = asyncio.run(
        enqueue_ready_revision_graph_extraction(
            library_id=LIB_ID,
            document_id=DOC_ID,
            revision_id=REV_ID,
            session_factory=factory,
        )
    )

    assert result is None
    assert factory.call_count == 0


def test_ready_revision_trigger_uses_production_creator_without_provider_call(monkeypatch):
    _enable_auto_trigger(monkeypatch)
    library, document, revision = _scope()
    session = TriggerSession(
        objects={
            (Library, LIB_ID): library,
            (Document, DOC_ID): document,
            (DocumentRevision, REV_ID): revision,
        }
    )
    factory = TriggerFactory(session)
    expected_job = SimpleNamespace(id=uuid.uuid4())
    with (
        patch(
            "app.services.graph_extraction_triggers.create_graph_extraction_job",
            new=AsyncMock(return_value=expected_job),
        ) as create,
        patch(
            "app.services.graph_extraction_provider.OpenAICompatibleGraphExtractor.extract",
            new=AsyncMock(),
        ) as provider,
    ):
        result = asyncio.run(
            enqueue_ready_revision_graph_extraction(
                library_id=LIB_ID,
                document_id=DOC_ID,
                revision_id=REV_ID,
                session_factory=factory,
            )
        )

    assert result is expected_job
    assert create.await_args.kwargs["trigger_type"] == "revision_published"
    assert create.await_args.kwargs["execution_mode"] == "production"
    assert create.await_args.kwargs["build_mode"] == "standard"
    provider.assert_not_awaited()


def test_upload_requested_trigger_bypasses_only_the_global_auto_switch(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    monkeypatch.setattr(settings, "graph_extraction_auto_trigger_enabled", False)
    monkeypatch.setattr(settings, "graph_extraction_api_key", SecretStr("test-key"))
    library, document, revision = _scope()
    session = TriggerSession(
        objects={
            (Library, LIB_ID): library,
            (Document, DOC_ID): document,
            (DocumentRevision, REV_ID): revision,
        }
    )
    expected_job = SimpleNamespace(id=uuid.uuid4())
    with patch(
        "app.services.graph_extraction_triggers.create_graph_extraction_job",
        new=AsyncMock(return_value=expected_job),
    ) as create:
        result = asyncio.run(
            enqueue_ready_revision_graph_extraction(
                library_id=LIB_ID,
                document_id=DOC_ID,
                revision_id=REV_ID,
                force=True,
                session_factory=TriggerFactory(session),
            )
        )

    assert result is expected_job
    create.assert_awaited_once()


def test_upload_configuration_requires_runtime_library_security_and_ontology(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    monkeypatch.setattr(settings, "graph_extraction_api_key", SecretStr("test-key"))
    library = SimpleNamespace(
        id=LIB_ID,
        graph_extraction_enabled=True,
        external_llm_enabled=True,
        graph_extraction_allowed_security_levels=["internal"],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Scalar(uuid.uuid4()))

    result = asyncio.run(graph_extraction_upload_configuration(db, library))

    assert result == {
        "available": True,
        "exploration_available": False,
        "default_requested": True,
        "default_build_mode": "standard",
        "allowed_security_levels": ["internal"],
        "reasons": [],
        "schema_mode": "governed",
        "schema_confirmation_policy": "required",
        "requires_active_schema": True,
    }


def test_upload_configuration_explore_mode_does_not_require_active_schema(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    monkeypatch.setattr(settings, "graph_extraction_api_key", SecretStr("test-key"))
    library = SimpleNamespace(
        id=LIB_ID,
        graph_extraction_enabled=True,
        external_llm_enabled=True,
        graph_extraction_allowed_security_levels=["internal"],
        schema_mode="explore",
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Scalar(None))

    result = asyncio.run(graph_extraction_upload_configuration(db, library))

    assert result["available"] is False
    assert result["exploration_available"] is True
    assert result["default_requested"] is True
    assert result["schema_mode"] == "explore"
    assert result["requires_active_schema"] is False
    assert result["reasons"] == ["active_ontology_missing"]


def test_explore_upload_with_confirmed_active_schema_starts_new_discovery_run(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    monkeypatch.setattr(settings, "graph_extraction_auto_trigger_enabled", False)
    monkeypatch.setattr(settings, "graph_extraction_api_key", SecretStr("test-key"))
    library, document, revision = _scope()
    library.graph_extraction_enabled = True
    library.external_llm_enabled = True
    library.graph_extraction_allowed_security_levels = ["internal"]
    library.schema_mode = "explore"

    configured = MagicMock()
    configured.scalars.return_value.first.return_value = SimpleNamespace(
        version_key="confirmed-v1",
        status="active",
        confirmed=True,
    )
    config_db = AsyncMock()
    config_db.execute = AsyncMock(return_value=configured)
    config = asyncio.run(graph_extraction_upload_configuration(config_db, library))
    assert config["available"] is True
    assert config["exploration_available"] is True

    session = TriggerSession(
        objects={
            (Library, LIB_ID): library,
            (Document, DOC_ID): document,
            (DocumentRevision, REV_ID): revision,
        }
    )
    import_job = SimpleNamespace(batch_id=uuid.uuid4())
    import_result = MagicMock()
    import_result.scalars.return_value.first.return_value = import_job
    session.execute = AsyncMock(return_value=import_result)
    run = SimpleNamespace(id=uuid.uuid4())
    job = SimpleNamespace(id=uuid.uuid4(), document_revision_id=REV_ID)
    with patch(
        "app.services.graph_extraction_triggers.ensure_schema_discovery_run_for_batch",
        new=AsyncMock(return_value=(run, [job])),
    ) as ensure:
        result = asyncio.run(
            enqueue_ready_revision_graph_extraction(
                library_id=LIB_ID,
                document_id=DOC_ID,
                revision_id=REV_ID,
                force=True,
                session_factory=TriggerFactory(session),
            )
        )

    assert result is job
    assert ensure.await_args.kwargs["batch_id"] == import_job.batch_id


def test_upload_configuration_governed_mode_blocks_without_schema(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    monkeypatch.setattr(settings, "graph_extraction_api_key", SecretStr("test-key"))
    library = SimpleNamespace(
        id=LIB_ID,
        graph_extraction_enabled=True,
        external_llm_enabled=True,
        graph_extraction_allowed_security_levels=["internal"],
        schema_mode="governed",
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Scalar(None))

    result = asyncio.run(graph_extraction_upload_configuration(db, library))

    assert result["available"] is False
    assert result["exploration_available"] is False
    assert result["default_requested"] is True
    assert result["schema_mode"] == "governed"
    assert result["requires_active_schema"] is True
    assert result["reasons"] == ["active_ontology_missing"]


def test_upload_configuration_reports_all_blocking_reasons(monkeypatch):
    monkeypatch.setattr(settings, "graph_extraction_enabled", False)
    monkeypatch.setattr(settings, "graph_extraction_api_key", SecretStr(""))
    library = SimpleNamespace(
        id=LIB_ID,
        graph_extraction_enabled=False,
        external_llm_enabled=False,
        graph_extraction_allowed_security_levels=[],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Scalar(None))

    result = asyncio.run(graph_extraction_upload_configuration(db, library))

    assert result["available"] is False
    assert result["exploration_available"] is False
    assert result["default_requested"] is False
    assert result["schema_mode"] == "disabled"
    assert result["requires_active_schema"] is False
    assert result["allowed_security_levels"] == []
    assert result["reasons"] == [
        "runtime_disabled",
        "provider_unconfigured",
        "library_disabled",
        "external_model_disabled",
        "security_levels_missing",
        "active_ontology_missing",
    ]


def test_compensation_is_bounded_to_current_ready_revision_candidates(monkeypatch):
    _enable_auto_trigger(monkeypatch)
    library, document, revision = _scope()
    session = TriggerSession(rows=[(library, document, revision)])
    factory = TriggerFactory(session)
    with patch(
        "app.services.graph_extraction_triggers.create_graph_extraction_job",
        new=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
    ) as create:
        ensured = asyncio.run(
            compensate_ready_graph_extractions(
                limit=100,
                session_factory=factory,
            )
        )

    assert ensured == 1
    assert session.nested_count == 1
    statement = session.statements[0]
    sql = str(statement).lower()
    assert "current_revision_id" in sql
    assert "document_revisions.status" in sql
    assert statement._limit_clause.value == 100
    create.assert_awaited_once()


class _ScalarResult:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


def test_publication_commit_survives_auto_trigger_failure(monkeypatch):
    _enable_auto_trigger(monkeypatch)
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    class ExpiringLibrary:
        expired = False
        revision_retention_enabled = False
        qdrant_collection = "trigger"

        @property
        def id(self):
            if self.expired:
                raise RuntimeError("expired library accessed after publication commit")
            return LIB_ID

    class ExpiringJob:
        expired = False

        def __init__(self):
            self._id = uuid.uuid4()

        @property
        def id(self):
            if self.expired:
                raise RuntimeError("expired job accessed after publication commit")
            return self._id

        @property
        def document_id(self):
            if self.expired:
                raise RuntimeError("expired job accessed after publication commit")
            return DOC_ID

        @property
        def document_revision_id(self):
            if self.expired:
                raise RuntimeError("expired job accessed after publication commit")
            return REV_ID

    library = ExpiringLibrary()
    document = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        content_hash="hash",
        current_revision_id=None,
        latest_revision_id=REV_ID,
        status="pending",
    )
    revision = DocumentRevision(
        id=REV_ID,
        document_id=DOC_ID,
        library_id=LIB_ID,
        revision_no=1,
        content_hash="hash",
        parser_name="plain",
        parser_version="v1",
        chunking_strategy="fixed",
        chunking_strategy_version="v1",
        status="processing",
        parser_config={"graph_extraction_requested": True},
    )
    embedding_job = ExpiringJob()
    order = []

    async def execute(statement, *_args, **_kwargs):
        sql = str(statement).lower()
        if "from documents" in sql:
            return _ScalarResult(document)
        if "from document_revisions" in sql:
            return _ScalarResult(revision)
        return MagicMock(rowcount=1)

    async def commit():
        order.append("publication_commit")
        embedding_job.expired = True

    async def rollback():
        library.expired = True

    async def trigger(**_kwargs):
        order.append("trigger_attempt")
        raise RuntimeError("enqueue failed")

    async def artifact_trigger(**_kwargs):
        order.append("artifact_trigger_attempt")
        return ()

    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    db.rollback = AsyncMock(side_effect=rollback)
    db.commit = AsyncMock(side_effect=commit)
    with (
        patch(
            "app.services.graph_extraction_triggers.enqueue_ready_revision_graph_extraction",
            new=AsyncMock(side_effect=trigger),
        ) as graph_trigger,
        patch(
            "app.services.knowledge_artifact_jobs.enqueue_ready_revision_artifacts",
            new=AsyncMock(side_effect=artifact_trigger),
        ),
    ):
        published = asyncio.run(
            embedder._publish_revision_after_qdrant(
                db,
                library=library,
                job=embedding_job,
            )
        )

    assert published is True
    assert order == [
        "publication_commit",
        "trigger_attempt",
        "artifact_trigger_attempt",
    ]
    db.commit.assert_awaited_once()
    assert graph_trigger.await_args.kwargs["force"] is True
