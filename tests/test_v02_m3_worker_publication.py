from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import Settings, settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.workers import embedder


LIB_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
DOC_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
REV_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
OLD_REV_ID = uuid.UUID("00000000-0000-0000-0000-0000000000dd")
CHUNK_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ee")


def _lib() -> Library:
    return Library(
        id=LIB_ID,
        slug="lib",
        name="Library",
        qdrant_collection="lib_col",
        embedding_model="bge-m3",
        embedding_dim=3,
        chunk_size=1000,
        chunk_overlap=120,
        index_state="ready",
    )


def _doc() -> Document:
    return Document(
        id=DOC_ID,
        library_id=LIB_ID,
        title="Real title",
        external_id="ext-1",
        doc_metadata={
            "department": "legal",
            "document_revision_id": "user-forged",
            "evidence_id": "user-forged",
        },
        content_hash="hash",
        current_revision=2,
        current_revision_id=OLD_REV_ID,
        latest_revision_id=REV_ID,
        status="pending",
        visibility_scope="internal",
        security_level="normal",
    )


def _revision(status: str = "pending") -> DocumentRevision:
    return DocumentRevision(
        id=REV_ID,
        document_id=DOC_ID,
        library_id=LIB_ID,
        revision_no=2,
        title="Real title",
        content_hash="hash",
        parser_name="legacy",
        parser_version="v0.2-m2",
        chunking_strategy="text",
        chunking_strategy_version="v0.2-m2",
        status=status,
        visibility_scope="internal",
        security_level="normal",
    )


def _job() -> EmbeddingJob:
    return EmbeddingJob(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision=2,
        document_revision_id=REV_ID,
        document_revision_no=2,
        status="processing",
    )


def _chunk() -> Chunk:
    return Chunk(
        id=CHUNK_ID,
        document_id=DOC_ID,
        library_id=LIB_ID,
        document_revision_id=REV_ID,
        block_id=uuid.UUID("00000000-0000-0000-0000-000000000011"),
        evidence_id=uuid.UUID("00000000-0000-0000-0000-000000000012"),
        seq=7,
        chunk_kind="text",
        text="chunk text",
        token_count=10,
        source_start=3,
        source_end=13,
        page_start=1,
        page_end=1,
        title_path=["A", "B"],
        position={"type": "line", "start_line": 1},
    )


def test_m3_feature_flags_default_off():
    s = Settings()
    assert s.enable_revision_id_worker is False
    assert s.enable_revision_id_visibility is False


def test_revision_point_id_is_deterministic_and_revision_scoped():
    same = embedder.deterministic_revision_point_id(REV_ID, CHUNK_ID)
    assert same == embedder.deterministic_revision_point_id(REV_ID, CHUNK_ID)
    assert same != embedder.deterministic_revision_point_id(OLD_REV_ID, CHUNK_ID)
    assert uuid.UUID(same)


def test_point_id_uses_revision_scope_only_when_job_has_revision_id():
    chunk = _chunk()
    job = _job()
    assert embedder._point_id_for_chunk(job, chunk) == embedder.deterministic_revision_point_id(REV_ID, CHUNK_ID)
    job.document_revision_id = None
    assert embedder._point_id_for_chunk(job, chunk) == str(CHUNK_ID)


def test_revision_payload_system_fields_win_over_user_metadata():
    payload = embedder._build_payload(_lib(), _doc(), _chunk(), job=_job(), revision=_revision())
    assert payload["department"] == "legal"
    assert payload["library_id"] == str(LIB_ID)
    assert payload["document_id"] == str(DOC_ID)
    assert payload["document_revision_id"] == str(REV_ID)
    assert payload["document_revision_no"] == 2
    assert payload["document_revision"] == 2
    assert payload["chunk_id"] == str(CHUNK_ID)
    assert payload["block_id"] == "00000000-0000-0000-0000-000000000011"
    assert payload["evidence_id"] == "00000000-0000-0000-0000-000000000012"
    assert payload["chunk_kind"] == "text"
    assert payload["page_start"] == 1
    assert payload["page_end"] == 1
    assert payload["title_path"] == ["A", "B"]
    assert payload["position"] == {"type": "line", "start_line": 1}
    assert payload["visibility_scope"] == "internal"
    assert payload["security_level"] == "normal"


def test_revision_worker_eligibility_requires_latest_pending_revision(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    lib = NS(id=LIB_ID, deleted_at=None, index_state="ready", active_rebuild_operation_id=None)
    doc = NS(id=DOC_ID, deleted_at=None, latest_revision_id=REV_ID, current_revision=2)
    job = NS(
        document_revision=2,
        document_revision_id=REV_ID,
        rebuild_operation_id=None,
    )

    assert embedder.eligibility(job, doc, lib, None, NS(id=REV_ID, status="pending")) is True
    assert embedder.eligibility(job, doc, lib, None, NS(id=REV_ID, status="processing")) is True
    assert embedder.eligibility(job, doc, lib, None, NS(id=REV_ID, status="ready")) is False

    stale_doc = NS(id=DOC_ID, deleted_at=None, latest_revision_id=OLD_REV_ID, current_revision=2)
    assert embedder.eligibility(job, stale_doc, lib, None, NS(id=REV_ID, status="pending")) is False

    legacy_job = NS(document_revision=2, document_revision_id=None, rebuild_operation_id=None)
    assert embedder.eligibility(legacy_job, doc, lib, None, NS(id=REV_ID, status="pending")) is False


def test_revision_worker_keeps_legacy_eligibility_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", False)
    lib = NS(id=LIB_ID, deleted_at=None, index_state="ready", active_rebuild_operation_id=None)
    doc = NS(id=DOC_ID, deleted_at=None, current_revision=2, latest_revision_id=OLD_REV_ID)
    job = NS(document_revision=2, document_revision_id=REV_ID, rebuild_operation_id=None)

    assert embedder.eligibility(job, doc, lib, None, None) is True


def test_chunks_for_revision_job_selects_only_revision_chunks(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    chunk = _chunk()
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [chunk]
    db.execute = AsyncMock(return_value=result)

    chunks = asyncio.run(embedder._chunks_for_job(db, _job(), _doc()))

    assert chunks == [chunk]
    sql_text = str(db.execute.await_args.args[0]).lower()
    assert "document_revision_id" in sql_text


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


def _exec_for_publish(doc, revision):
    calls = []

    async def execute(stmt, *args, **kwargs):
        calls.append(str(stmt))
        stmt_text = str(stmt).lower()
        if "from documents" in stmt_text:
            return _ScalarResult(doc)
        if "from document_revisions" in stmt_text:
            return _ScalarResult(revision)
        return MagicMock(rowcount=1)

    return execute, calls


def test_publish_revision_after_qdrant_sets_current_only_when_latest_matches(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    doc = _doc()
    revision = _revision("processing")
    execute, calls = _exec_for_publish(doc, revision)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    db.rollback = AsyncMock()
    events = []
    db.commit = AsyncMock(side_effect=lambda: events.append("commit"))

    async def supersede_classification(*_args, **_kwargs):
        events.append("supersede_classification")
        return 1

    async def enqueue_classification(**_kwargs):
        events.append("enqueue_classification")
        return (uuid.uuid4(),)

    with (
        patch(
            "app.services.classification_jobs.supersede_revision_classification_jobs",
            side_effect=supersede_classification,
        ) as supersede,
        patch(
            "app.services.classification_jobs.enqueue_ready_revision_classification",
            side_effect=enqueue_classification,
        ) as enqueue,
    ):
        ok = asyncio.run(
            embedder._publish_revision_after_qdrant(
                db,
                library=_lib(),
                job=_job(),
                now=datetime(2026, 7, 9, tzinfo=timezone.utc),
            )
        )

    assert ok is True
    assert db.commit.await_count == 1
    supersede.assert_awaited_once_with(
        db,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=OLD_REV_ID,
        now=datetime(2026, 7, 9, tzinfo=timezone.utc),
    )
    enqueue.assert_awaited_once_with(
        library_id=LIB_ID,
        document_id=DOC_ID,
        revision_id=REV_ID,
    )
    assert events.index("supersede_classification") < events.index("commit")
    assert events.index("commit") < events.index("enqueue_classification")
    joined_sql = "\n".join(calls).lower()
    assert "current_revision_id" in joined_sql
    assert "latest_revision_id" in joined_sql
    assert "status" in joined_sql


def test_publish_revision_after_qdrant_rejects_stale_latest(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    doc = _doc()
    doc.latest_revision_id = OLD_REV_ID
    revision = _revision("processing")
    execute, calls = _exec_for_publish(doc, revision)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    db.rollback = AsyncMock()
    db.commit = AsyncMock()

    ok = asyncio.run(embedder._publish_revision_after_qdrant(db, library=_lib(), job=_job()))

    assert ok is False
    assert db.commit.await_count == 1
    status_values = []
    for call in db.execute.await_args_list:
        params = call.args[0].compile().params
        if "status" in params:
            status_values.append(params["status"])
    assert "superseded" in status_values


def test_revision_worker_upserts_revision_scoped_points_before_publish(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    lib = _lib()
    doc = _doc()
    revision = _revision("pending")
    job = _job()
    chunk = _chunk()

    async def fake_get(model, _id):
        if model is embedder.Library:
            return lib
        if model is embedder.Document:
            return doc
        if model is embedder.DocumentRevision:
            return revision
        return None

    db = MagicMock()
    db.get = AsyncMock(side_effect=fake_get)
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    async def publish_and_expire_orm_fields(*_args, **_kwargs):
        from sqlalchemy import inspect

        inspect(lib)._expire_attributes(lib.__dict__, ["slug"])
        inspect(doc)._expire_attributes(doc.__dict__, ["id"])
        inspect(job)._expire_attributes(job.__dict__, ["document_revision_id"])
        return True

    with patch.object(embedder, "_chunks_for_job", new_callable=AsyncMock, return_value=[chunk]) as chunks_for_job, \
         patch.object(embedder.embedding, "embed_texts", new_callable=AsyncMock, return_value=[[0.1, 0.2, 0.3]]) as embed, \
         patch.object(embedder.qdrant, "upsert_points", new_callable=AsyncMock) as upsert, \
         patch.object(
             embedder,
             "_publish_revision_after_qdrant",
             new=AsyncMock(side_effect=publish_and_expire_orm_fields),
         ) as publish:
        asyncio.run(embedder._process_job(db, job))

    chunks_for_job.assert_awaited_once()
    embed.assert_awaited_once()
    upsert.assert_awaited_once()
    points = upsert.await_args.args[1]
    assert points[0]["id"] == embedder.deterministic_revision_point_id(REV_ID, CHUNK_ID)
    assert points[0]["payload"]["document_revision_id"] == str(REV_ID)
    publish.assert_awaited_once()


def _db_with_revision_rows(rows):
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


def test_revision_visibility_requires_current_revision_id_when_payload_has_it(monkeypatch):
    from app.services import visibility

    monkeypatch.setattr(settings, "retrieval_consistency_filter", True)
    monkeypatch.setattr(settings, "enable_revision_id_visibility", True)
    rows = [(DOC_ID, LIB_ID, 2, REV_ID, None, "ready")]
    payloads = [
        {"document_id": str(DOC_ID), "document_revision_id": str(REV_ID), "document_revision": 2},
        {"document_id": str(DOC_ID), "document_revision_id": str(OLD_REV_ID), "document_revision": 2},
    ]

    mask = asyncio.run(visibility.compute_visible_mask(_db_with_revision_rows(rows), _lib(), payloads))

    assert mask == [True, False]


def test_revision_visibility_requires_ready_current_revision(monkeypatch):
    from app.services import visibility

    monkeypatch.setattr(settings, "retrieval_consistency_filter", True)
    monkeypatch.setattr(settings, "enable_revision_id_visibility", True)
    rows = [(DOC_ID, LIB_ID, 2, REV_ID, None, "failed")]
    payloads = [{"document_id": str(DOC_ID), "document_revision_id": str(REV_ID), "document_revision": 2}]

    mask = asyncio.run(visibility.compute_visible_mask(_db_with_revision_rows(rows), _lib(), payloads))

    assert mask == [False]


def test_cleanup_dispatches_revision_id_delete():
    from app.models.cleanup_outbox import EVENT_DELETE_DOCUMENT_REVISION
    from app.services import cleanup as cleanup_svc

    row = NS(
        event_type=EVENT_DELETE_DOCUMENT_REVISION,
        collection_name="lib_col",
        document_id=DOC_ID,
        target_revision=None,
        payload={"document_revision_id": str(REV_ID)},
    )
    with patch("app.services.qdrant.delete_points_by_document_revision_id", new_callable=AsyncMock) as delete_rev:
        asyncio.run(cleanup_svc.execute_event(row))

    delete_rev.assert_awaited_once_with("lib_col", str(REV_ID))


def test_evidence_reingest_defers_integer_cleanup_when_revision_worker_enabled(monkeypatch):
    from app.services import ingest as ingest_service

    monkeypatch.setattr(settings, "enable_evidence_write_path", True)
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    doc = _doc()
    doc.current_revision = 1
    db = MagicMock()
    db.add = MagicMock()
    db.add_all = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()

    with patch("app.services.cleanup.enqueue_delete_before_revision", new_callable=AsyncMock) as old_cleanup:
        asyncio.run(
            ingest_service.reingest_document(
                db=db,
                library=_lib(),
                document=doc,
                new_text="new text",
                title="New",
                metadata=None,
                splitter="text",
                force=True,
                chunks=["new text"],
            )
        )

    old_cleanup.assert_not_awaited()
