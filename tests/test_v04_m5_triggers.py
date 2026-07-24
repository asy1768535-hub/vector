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
    provider.assert_not_awaited()


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
        ),
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
