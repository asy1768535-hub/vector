from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.rebuild_operation import RebuildOperation
from app.services import rebuild
from app.workers import embedder


def _state():
    lib_id, doc_id, rev_id, latest_id, op_id = [uuid.uuid4() for _ in range(5)]
    lib = Library(id=lib_id, slug="test", name="test", lifecycle_mode="managed",
                  qdrant_collection="test_col", embedding_dim=3, embedding_model="test",
                  vector_distance="cosine", index_state="rebuilding", active_rebuild_operation_id=op_id)
    doc = Document(id=doc_id, library_id=lib_id, content_hash="test", current_revision=8,
                   current_revision_id=rev_id, latest_revision_id=latest_id, status="pending")
    rev = DocumentRevision(id=rev_id, document_id=doc_id, library_id=lib_id, revision_no=3,
                           status="ready", title="Published title", document_metadata={"source": "published"})
    op = RebuildOperation(id=op_id, library_id=lib_id, collection_name="test_col", status="running")
    job = EmbeddingJob(id=uuid.uuid4(), library_id=lib_id, document_id=doc_id,
                       document_revision=8, document_revision_id=rev_id, document_revision_no=3,
                       rebuild_operation_id=op_id, status="processing", attempt_count=1)
    chunk = Chunk(id=uuid.uuid4(), document_id=doc_id, library_id=lib_id,
                  document_revision_id=rev_id, seq=1, text="Published content")
    return lib, doc, rev, op, job, chunk


def _result(value=None, rows=None):
    result = MagicMock(rowcount=1)
    result.scalar_one.return_value = value
    result.scalar_one_or_none.return_value = value
    result.scalars.return_value.first.return_value = value
    result.scalars.return_value.all.return_value = rows or []
    result.all.return_value = rows or []
    return result


def test_rebuild_accepts_published_current_even_when_latest_differs(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    lib, doc, rev, op, job, _ = _state()
    assert embedder.eligibility(job, doc, lib, op, rev) is True
    rev.status = "pending"
    assert embedder.eligibility(job, doc, lib, op, rev) is False


def test_rebuild_payload_separates_index_generation_from_content_revision():
    lib, doc, rev, _, job, chunk = _state()
    payload = embedder._build_payload(lib, doc, chunk, job=job, revision=rev)
    assert payload["document_revision"] == 8
    assert payload["document_revision_no"] == 3
    assert payload["document_revision_id"] == str(rev.id)
    assert payload["title"] == "Published title"
    assert payload["source"] == "published"


def test_prepare_persists_only_published_targets_before_qdrant(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    lib, doc, rev, _, _, _ = _state()
    lib.index_state = "ready"
    lib.active_rebuild_operation_id = None
    doc.status = "ready"
    doc.current_revision = 7
    unpublished = Document(id=uuid.uuid4(), library_id=lib.id, content_hash="new",
                           current_revision=1, status="failed", latest_revision_id=uuid.uuid4())
    added = []
    async def execute(stmt, *args):
        sql = str(stmt).lower()
        if "from sys_libraries" in sql:
            return _result(lib)
        if "from rebuild_operations" in sql:
            return _result()
        if "from documents" in sql:
            return _result(rows=[doc, unpublished])
        if "from document_revisions" in sql:
            return _result(rev, [rev])
        if "from chunks" in sql or "count(" in sql:
            return _result(0)
        return _result()
    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    db.add = MagicMock(side_effect=added.append)
    async def flush():
        for obj in added:
            if obj.id is None:
                obj.id = uuid.uuid4()
    db.flush = AsyncMock(side_effect=flush)
    db.commit = AsyncMock()
    asyncio.run(rebuild._prepare(db, lib.id))
    jobs = [obj for obj in added if isinstance(obj, EmbeddingJob)]
    assert len(jobs) == 1
    assert jobs[0].document_id == doc.id
    assert jobs[0].document_revision_id == rev.id
    assert jobs[0].document_revision_no == 3
    assert jobs[0].document_revision == 8
    assert unpublished.current_revision == 1
    assert unpublished.status == "failed"
    operation = next(obj for obj in added if isinstance(obj, RebuildOperation))
    assert operation.status == "preparing"
    assert operation.expected_job_count == 0


def test_resume_reads_persisted_job_snapshot_not_current_document():
    lib, doc, rev, op, job, _ = _state()
    op.status = "preparing"
    doc.latest_revision_id = uuid.uuid4()
    db = MagicMock()
    statements = []
    async def execute(stmt, *args):
        statements.append(str(stmt).lower())
        if "from sys_libraries" in str(stmt).lower():
            return _result(lib)
        if "from embedding_jobs" in str(stmt).lower():
            return _result(rows=[job])
        if "from documents" in str(stmt).lower():
            return _result(rows=[doc])
        if "from document_revisions" in str(stmt).lower():
            return _result(rows=[rev])
        return _result()
    db.execute = AsyncMock(side_effect=execute)
    db.commit = AsyncMock()
    _, _, _, targets = asyncio.run(rebuild._resume_state(db, lib.id, op))
    assert targets == [(job.document_id, 8, rev.id, 3)]
    assert any("from embedding_jobs" in stmt for stmt in statements)


def test_legacy_rebuild_cannot_read_unpublished_latest_when_gate_off(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", False)
    lib, doc, _, _, _, _ = _state()
    lib.active_rebuild_operation_id = None
    doc.current_revision_id = None
    doc.status = "ready"
    db = MagicMock()
    async def execute(stmt, *args):
        sql = str(stmt).lower()
        if "from sys_libraries" in sql:
            return _result(lib)
        if "from rebuild_operations" in sql:
            return _result()
        if "from documents" in sql:
            return _result(rows=[doc])
        if "from chunks" in sql:
            return _result(1, [(doc.id, 1, 0)])
        return _result()
    db.execute = AsyncMock(side_effect=execute)
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    with pytest.raises(rebuild.RebuildTargetError, match="legacy"):
        asyncio.run(rebuild._prepare(db, lib.id))
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("revision_gate", [False, True])
def test_published_legacy_is_supported_only_without_revision_gate(monkeypatch, revision_gate):
    monkeypatch.setattr(settings, "enable_revision_id_worker", revision_gate)
    lib, doc, _, _, _, _ = _state()
    lib.active_rebuild_operation_id = None
    doc.current_revision_id = None
    doc.latest_revision_id = None
    doc.status = "ready"
    added = []
    async def execute(stmt, *args):
        sql = str(stmt).lower()
        if "from sys_libraries" in sql:
            return _result(lib)
        if "from rebuild_operations" in sql:
            return _result()
        if "from documents" in sql:
            return _result(rows=[doc])
        if "from chunks" in sql:
            return _result(0 if "is not null" in sql else 1)
        return _result()
    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    db.add = MagicMock(side_effect=added.append)
    async def flush():
        for obj in added:
            if obj.id is None:
                obj.id = uuid.uuid4()
    db.flush = AsyncMock(side_effect=flush)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    if revision_gate:
        with pytest.raises(rebuild.RebuildTargetError, match="legacy"):
            asyncio.run(rebuild._prepare(db, lib.id))
        assert added == []
    else:
        asyncio.run(rebuild._prepare(db, lib.id))
        job = next(obj for obj in added if isinstance(obj, EmbeddingJob))
        assert job.document_revision_id is None
        assert job.document_revision_no is None
        assert job.document_revision == 9


@pytest.mark.parametrize("changed_field", ["current", "generation", "number", "operation", "collection", "deleted"])
def test_rebuild_eligibility_rejects_changed_target(monkeypatch, changed_field):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    lib, doc, rev, op, job, _ = _state()
    if changed_field == "current":
        doc.current_revision_id = uuid.uuid4()
    elif changed_field == "generation":
        doc.current_revision += 1
    elif changed_field == "number":
        job.document_revision_no = 99
    elif changed_field == "operation":
        op.id = uuid.uuid4()
    elif changed_field == "collection":
        op.collection_name = "another_collection"
    else:
        doc.deleted_at = object()
    assert embedder.eligibility(job, doc, lib, op, rev) is False


def test_old_operation_cannot_finalize_new_library_state():
    lib, _, _, op, _, _ = _state()
    lib.active_rebuild_operation_id = uuid.uuid4()
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(lib.id), _result(lib), _result(op)])
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    assert asyncio.run(rebuild.try_finalize(db, op.id)) is False
    assert lib.index_state == "rebuilding"
    assert op.status == "running"
    db.commit.assert_not_awaited()


def test_normal_worker_keeps_distinct_generation_and_content_number(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    lib, doc, revision, _, job, chunk = _state()
    lib.index_state = "ready"
    lib.active_rebuild_operation_id = None
    doc.current_revision_id = uuid.uuid4()
    doc.latest_revision_id = revision.id
    revision.status = "processing"
    job.rebuild_operation_id = None
    db = MagicMock()
    async def get(model, _id):
        return {Library: lib, Document: doc, DocumentRevision: revision}.get(model)
    async def execute(stmt, *args):
        sql = str(stmt).lower()
        if "from sys_libraries" in sql:
            return _result(lib)
        if "from documents" in sql:
            return _result(doc)
        if "from document_revisions" in sql:
            return _result(revision)
        return _result()
    db.get = AsyncMock(side_effect=get)
    db.execute = AsyncMock(side_effect=execute)
    db.rollback = AsyncMock()
    upsert, publish = AsyncMock(), AsyncMock(return_value=True)
    monkeypatch.setattr(embedder, "_chunks_for_job", AsyncMock(return_value=[chunk]))
    monkeypatch.setattr(embedder.embedding, "embed_texts", AsyncMock(return_value=[[1.0, 0.0, 0.0]]))
    monkeypatch.setattr(embedder.qdrant, "upsert_points", upsert)
    monkeypatch.setattr(embedder, "_publish_revision_after_qdrant", publish)
    asyncio.run(embedder._process_job(db, job))
    upsert.assert_awaited_once()
    payload = upsert.await_args.args[1][0]["payload"]
    assert payload["document_revision"] == 8
    assert payload["document_revision_no"] == 3
    publish.assert_awaited_once()


@pytest.mark.parametrize("already_ready", [False, True])
def test_normal_publication_never_cleans_retained_revision(monkeypatch, already_ready):
    from app.services import cleanup
    lib, doc, revision, _, job, _ = _state()
    lib.index_state = "ready"
    doc.latest_revision_id = revision.id
    job.rebuild_operation_id = None
    if already_ready:
        revision.status = "ready"
    else:
        revision.status = "processing"
        doc.current_revision = 9
    statements = []
    async def execute(stmt, *args):
        sql = str(stmt).lower()
        statements.append(sql)
        if "from sys_libraries" in sql:
            return _result(lib)
        if "from documents" in sql:
            return _result(doc)
        if "from document_revisions" in sql:
            return _result(revision)
        return _result()
    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    db.rollback, db.commit = AsyncMock(), AsyncMock()
    unpublished_cleanup, revision_cleanup = AsyncMock(), AsyncMock()
    monkeypatch.setattr(cleanup, "enqueue_delete_unpublished_revision_points", unpublished_cleanup)
    monkeypatch.setattr(cleanup, "enqueue_delete_document_revision", revision_cleanup)
    assert asyncio.run(embedder._publish_revision_after_qdrant(db, library=lib, job=job)) is False
    assert any("update embedding_jobs" in sql for sql in statements)
    assert not any("update document_revisions" in sql or "update documents" in sql for sql in statements)
    unpublished_cleanup.assert_not_awaited()
    revision_cleanup.assert_not_awaited()
