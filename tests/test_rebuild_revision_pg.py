"""Rebuild semantics on a disposable local PostgreSQL database.

Set VECTOR_KB_PG_TEST_DSN to a loopback database whose name ends in `_test`.
Only synthetic rows are created; network providers remain replaced at their boundary.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.config import settings
from app.db import Base
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.organization import DEFAULT_ORGANIZATION_ID, Organization
from app.models.rebuild_operation import RebuildOperation
from app.services import rebuild
from app.workers import embedder

_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="Needs disposable local PostgreSQL")


@asynccontextmanager
async def _database():
    url = make_url(_DSN)
    assert url.host in {"localhost", "127.0.0.1", "::1"}
    assert url.database and url.database.endswith("_test")
    schema = "rebuild_test_" + uuid.uuid4().hex
    engine = create_async_engine(url, connect_args={"server_settings": {
        "application_name": "rebuild_goal_test",
        "search_path": schema,
    }})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Build this owner's tables and their FK dependencies. Unrelated model
        # DDL is outside the rebuild contract and is not a test prerequisite.
        tables = {
            model.__table__ for model in (
                Organization, Library, Document, DocumentRevision, Chunk,
                EmbeddingJob, RebuildOperation, embedder.DocumentImportJob,
            )
        }
        pending = list(tables)
        while pending:
            for foreign_key in pending.pop().foreign_keys:
                target = foreign_key.column.table
                if target not in tables:
                    tables.add(target)
                    pending.append(target)
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=list(tables)))
        async with factory() as db:
            if await db.get(Organization, DEFAULT_ORGANIZATION_ID) is None:
                db.add(Organization(id=DEFAULT_ORGANIZATION_ID, slug="default", name="Test"))
                await db.commit()
        yield engine, factory
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _seed(factory):
    library_id, document_id, current_id, latest_id = (uuid.uuid4() for _ in range(4))
    slug = "rebuild_test_" + uuid.uuid4().hex[:12]
    async with factory() as db:
        db.add(Library(
            id=library_id, slug=slug, name="Synthetic rebuild",
            qdrant_collection=slug, embedding_model="test", embedding_dim=3,
            chunk_size=1000, chunk_overlap=100, index_state="ready",
        ))
        await db.flush()
        db.add(Document(
            id=document_id, library_id=library_id, title="Pending title",
            content_hash=uuid.uuid4().hex, current_revision=7,
            current_revision_id=current_id, latest_revision_id=latest_id, status="pending",
            doc_metadata={"label": "pending"},
        ))
        db.add_all([
            DocumentRevision(
                id=revision_id, document_id=document_id, library_id=library_id,
                revision_no=revision_no, title=title, document_metadata={"label": label},
                content_hash=uuid.uuid4().hex, parser_name="synthetic", parser_version="1",
                chunking_strategy="text", chunking_strategy_version="1", status=status,
            )
            for revision_id, revision_no, title, label, status in (
                (current_id, 3, "Published title", "published", "ready"),
                (latest_id, 4, "Pending title", "pending", "pending"),
            )
        ])
        await db.flush()
        db.add_all([
            Chunk(library_id=library_id, document_id=document_id, document_revision_id=rid,
                  seq=index, text=body, token_count=2)
            for index, rid, body in (
                (0, current_id, "published body"), (1, latest_id, "unpublished body"),
                (2, None, "legacy stray body"),
            )
        ])
        await db.commit()
    return library_id, document_id, current_id, latest_id


def test_pg_prepare_persists_published_snapshot_and_claim_waits_for_activation(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    monkeypatch.setattr(embedder, "get_worker_target_config", lambda: (False, None, None))

    async def scenario():
        async with _database() as (_, factory):
            lib_id, doc_id, current_id, latest_id = await _seed(factory)
            async with factory() as db:
                prepared = await rebuild._prepare(db, lib_id)
                op_id, _, _, _, snapshot = prepared
                job = (await db.execute(select(EmbeddingJob).where(
                    EmbeddingJob.rebuild_operation_id == op_id,
                ))).scalar_one()
                assert (job.document_revision, job.document_revision_id,
                        job.document_revision_no) == (8, current_id, 3)
                assert snapshot == [(doc_id, 8, current_id, 3)]
                await db.rollback()
                assert await embedder._claim_jobs(db, "before_activation", 100) == []

            async with factory() as db:
                doc = await db.get(Document, doc_id)
                doc.latest_revision_id = uuid.uuid4()
                await db.commit()
            async with factory() as db:
                operation = await db.get(RebuildOperation, op_id)
                resumed = await rebuild._resume_state(db, lib_id, operation)
                assert resumed[-1] == snapshot
                await db.rollback()
                await rebuild._activate(db, lib_id, op_id, resumed[-1])
                await rebuild._activate(db, lib_id, op_id, resumed[-1])
                jobs = await embedder._claim_jobs(db, "after_activation", 100)
                assert [job.document_id for job in jobs] == [doc_id]
                assert jobs[0].document_revision_id != latest_id
    asyncio.run(scenario())


@pytest.mark.parametrize("revision_gate", [False, True])
def test_pg_rebuild_worker_consumes_current_without_publishing_pending(monkeypatch, revision_gate):
    monkeypatch.setattr(settings, "enable_revision_id_worker", revision_gate)
    monkeypatch.setattr(embedder, "get_worker_target_config", lambda: (False, None, None))
    embed = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
    upsert = AsyncMock()
    publish = AsyncMock(side_effect=AssertionError("Rebuild must not publish content"))
    monkeypatch.setattr(embedder.embedding, "embed_texts", embed)
    monkeypatch.setattr(embedder.qdrant, "upsert_points", upsert)
    monkeypatch.setattr(embedder, "_publish_revision_after_qdrant", publish)

    async def scenario():
        async with _database() as (_, factory):
            monkeypatch.setattr(embedder, "async_session_factory", factory)
            lib_id, doc_id, current_id, latest_id = await _seed(factory)
            async with factory() as db:
                op_id, _, _, _, snapshot = await rebuild._prepare(db, lib_id)
                await rebuild._activate(db, lib_id, op_id, snapshot)
                jobs = await embedder._claim_jobs(db, "synthetic_worker", 100)
                assert len(jobs) == 1
                job_id = jobs[0].id
                await embedder._process_job(db, jobs[0])
            embed.assert_awaited_once()
            assert embed.await_args.args[0] == ["published body"]
            upsert.assert_awaited_once()
            points = upsert.await_args.args[1]
            assert len(points) == 1
            payload = points[0]["payload"]
            assert (payload["document_revision"], payload["document_revision_no"]) == (8, 3)
            assert payload["document_revision_id"] == str(current_id)
            assert payload["title"] == "Published title"
            assert payload["label"] == "published"
            publish.assert_not_awaited()
            async with factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.current_revision_id == current_id
                assert doc.latest_revision_id == latest_id
                assert doc.current_revision == 8
                assert doc.status == "ready"
                assert (await db.get(DocumentRevision, current_id)).status == "ready"
                assert (await db.get(DocumentRevision, latest_id)).status == "pending"
                assert (await db.get(EmbeddingJob, job_id)).status == "done"
                assert (await db.get(RebuildOperation, op_id)).status == "done"
                assert (await db.get(Library, lib_id)).index_state == "ready"
    asyncio.run(scenario())


def test_pg_concurrent_orchestrators_only_delete_once_and_release_session_lock(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    deleted = []

    async def scenario():
        async with _database() as (engine, factory):
            lib_id, _, _, _ = await _seed(factory)
            entered, release = asyncio.Event(), asyncio.Event()

            async def delete_collection(collection):
                deleted.append(collection)
                # The orchestration connection and lock connection are both idle.
                async with engine.connect() as check:
                    count = (await check.execute(text("""
                        SELECT count(*) FROM pg_stat_activity
                        WHERE application_name = 'rebuild_goal_test'
                          AND state = 'idle in transaction'
                    """))).scalar_one()
                    assert count == 0
                entered.set()
                await release.wait()

            monkeypatch.setattr(embedder.qdrant, "delete_collection", delete_collection)
            monkeypatch.setattr(embedder.qdrant, "ensure_collection", AsyncMock())
            async with factory() as first, factory() as second:
                task = asyncio.create_task(rebuild.run_rebuild(first, lib_id))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    with pytest.raises(rebuild.ConcurrentRebuildError):
                        await asyncio.wait_for(rebuild.run_rebuild(second, lib_id), timeout=2)
                finally:
                    release.set()
                    await asyncio.wait_for(task, timeout=5)
            assert len(deleted) == 1
            # A failed-operation resume must be able to acquire the returned lock.
            async with factory() as db:
                lib = await db.get(Library, lib_id)
                operation = await db.get(RebuildOperation, lib.active_rebuild_operation_id)
                operation.status = "failed"
                lib.index_state = "failed"
                await db.commit()
                await asyncio.wait_for(rebuild.run_rebuild(db, lib_id), timeout=5)
            assert len(deleted) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("provider_fails", [False, True])
def test_pg_embedding_drift_does_not_write_or_fail_newer_document(monkeypatch, provider_fails):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    monkeypatch.setattr(embedder, "get_worker_target_config", lambda: (False, None, None))
    upsert = AsyncMock()
    monkeypatch.setattr(embedder.qdrant, "upsert_points", upsert)

    async def scenario():
        async with _database() as (_, factory):
            monkeypatch.setattr(embedder, "async_session_factory", factory)
            lib_id, doc_id, current_id, latest_id = await _seed(factory)

            async def embed_then_change(*_args, **_kwargs):
                async with factory() as other:
                    doc = await other.get(Document, doc_id)
                    doc.current_revision = 9
                    doc.status = "pending"
                    doc.last_error = "newer task state"
                    await other.commit()
                if provider_fails:
                    raise RuntimeError("synthetic embedding failure")
                return [[1.0, 0.0, 0.0]]

            monkeypatch.setattr(embedder.embedding, "embed_texts", embed_then_change)
            async with factory() as db:
                op_id, _, _, _, snapshot = await rebuild._prepare(db, lib_id)
                await rebuild._activate(db, lib_id, op_id, snapshot)
                jobs = await embedder._claim_jobs(db, "drift_worker", 100)
                assert len(jobs) == 1
                jobs[0].attempt_count = settings.embed_worker_max_attempts
                await db.commit()
                job_id = jobs[0].id
                await embedder._process_job(db, jobs[0])
            upsert.assert_not_awaited()
            async with factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.current_revision == 9
                assert doc.status == "pending"
                assert doc.last_error == "newer task state"
                assert doc.current_revision_id == current_id
                assert doc.latest_revision_id == latest_id
                assert (await db.get(DocumentRevision, current_id)).status == "ready"
                assert (await db.get(DocumentRevision, latest_id)).status == "pending"
                assert (await db.get(EmbeddingJob, job_id)).status == "superseded"
                assert (await db.get(RebuildOperation, op_id)).status == "failed"
    asyncio.run(scenario())


@pytest.mark.parametrize("target_drifts", [False, True])
def test_pg_network_failure_resumes_snapshot_or_rejects_obsolete_target(monkeypatch, target_drifts):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    delete = AsyncMock(side_effect=RuntimeError("synthetic collection failure"))
    ensure = AsyncMock()
    monkeypatch.setattr(embedder.qdrant, "delete_collection", delete)
    monkeypatch.setattr(embedder.qdrant, "ensure_collection", ensure)

    async def scenario():
        async with _database() as (_, factory):
            lib_id, doc_id, current_id, _ = await _seed(factory)
            async with factory() as db:
                with pytest.raises(RuntimeError, match="synthetic collection failure"):
                    await rebuild.run_rebuild(db, lib_id)
            async with factory() as db:
                lib = await db.get(Library, lib_id)
                op_id = lib.active_rebuild_operation_id
                operation = await db.get(RebuildOperation, op_id)
                assert operation.status == "failed"
                assert operation.expected_job_count == 0
                assert lib.index_state == "failed"
                doc = await db.get(Document, doc_id)
                doc.latest_revision_id = uuid.uuid4()
                if target_drifts:
                    doc.current_revision = 9
                await db.commit()
            delete.side_effect = None
            async with factory() as db:
                if target_drifts:
                    with pytest.raises(rebuild.RebuildTargetError):
                        await rebuild.run_rebuild(db, lib_id)
                    assert delete.await_count == 1
                    ensure.assert_not_awaited()
                else:
                    assert await rebuild.run_rebuild(db, lib_id) == str(op_id)
                    assert delete.await_count == 2
                    ensure.assert_awaited_once()
                    job = (await db.execute(select(EmbeddingJob).where(
                        EmbeddingJob.rebuild_operation_id == op_id,
                    ))).scalar_one()
                    assert (job.document_revision, job.document_revision_id,
                            job.document_revision_no) == (8, current_id, 3)
                    assert (await db.get(Document, doc_id)).current_revision == 8
    asyncio.run(scenario())


def test_pg_unpublished_only_library_rebuild_completes_without_altering_document(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    monkeypatch.setattr(embedder.qdrant, "delete_collection", AsyncMock())
    monkeypatch.setattr(embedder.qdrant, "ensure_collection", AsyncMock())

    async def scenario():
        async with _database() as (_, factory):
            lib_id, doc_id, current_id, _ = await _seed(factory)
            async with factory() as db:
                doc = await db.get(Document, doc_id)
                doc.current_revision_id = None
                doc.current_revision = 1
                doc.status = "failed"
                doc.last_error = "original unpublished failure"
                (await db.get(DocumentRevision, current_id)).status = "pending"
                await db.commit()
                op_id = uuid.UUID(await rebuild.run_rebuild(db, lib_id))
            async with factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.current_revision_id is None
                assert doc.current_revision == 1
                assert doc.status == "failed"
                assert doc.last_error == "original unpublished failure"
                assert (await db.get(RebuildOperation, op_id)).expected_job_count == 0
                assert (await db.get(RebuildOperation, op_id)).status == "done"
                lib = await db.get(Library, lib_id)
                assert lib.index_state == "ready"
                assert lib.active_rebuild_operation_id is None
    asyncio.run(scenario())


def test_pg_cancelled_orchestration_releases_lock_for_snapshot_resume(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)

    async def scenario():
        async with _database() as (_, factory):
            lib_id, doc_id, current_id, _ = await _seed(factory)
            entered = asyncio.Event()

            async def wait_for_cancellation(_collection):
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(embedder.qdrant, "delete_collection", wait_for_cancellation)
            monkeypatch.setattr(embedder.qdrant, "ensure_collection", AsyncMock())
            async with factory() as db:
                task = asyncio.create_task(rebuild.run_rebuild(db, lib_id))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=5)
                finally:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            delete = AsyncMock()
            monkeypatch.setattr(embedder.qdrant, "delete_collection", delete)
            async with factory() as db:
                await asyncio.wait_for(rebuild.run_rebuild(db, lib_id), timeout=5)
                doc = await db.get(Document, doc_id)
                assert doc.current_revision == 8
                assert doc.current_revision_id == current_id
                delete.assert_awaited_once()
    asyncio.run(scenario())


def test_pg_inflight_normal_worker_cannot_publish_after_rebuild_finishes(monkeypatch):
    monkeypatch.setattr(settings, "enable_revision_id_worker", True)
    monkeypatch.setattr(embedder, "get_worker_target_config", lambda: (False, None, None))
    upsert = AsyncMock()
    publish = AsyncMock(side_effect=AssertionError("Old worker must not publish"))
    monkeypatch.setattr(embedder.qdrant, "upsert_points", upsert)
    monkeypatch.setattr(embedder.qdrant, "delete_collection", AsyncMock())
    monkeypatch.setattr(embedder.qdrant, "ensure_collection", AsyncMock())
    monkeypatch.setattr(embedder, "_publish_revision_after_qdrant", publish)

    async def scenario():
        async with _database() as (_, factory):
            monkeypatch.setattr(embedder, "async_session_factory", factory)
            lib_id, doc_id, current_id, latest_id = await _seed(factory)
            async with factory() as db:
                old_job = EmbeddingJob(
                    library_id=lib_id, document_id=doc_id, document_revision=7,
                    document_revision_id=latest_id, document_revision_no=4,
                    status="processing", attempt_count=1,
                )
                db.add(old_job)
                await db.commit()
                old_job_id = old_job.id

            async def embed_and_finish_rebuild(texts, **_kwargs):
                if texts == ["unpublished body"]:
                    async with factory() as other:
                        await rebuild.run_rebuild(other, lib_id)
                        jobs = await embedder._claim_jobs(other, "new_rebuild_worker", 100)
                        assert len(jobs) == 1
                        await embedder._process_job(other, jobs[0])
                return [[1.0, 0.0, 0.0] for _ in texts]

            monkeypatch.setattr(embedder.embedding, "embed_texts", embed_and_finish_rebuild)
            async with factory() as db:
                await embedder._process_job(db, await db.get(EmbeddingJob, old_job_id))
            upsert.assert_awaited_once()
            assert upsert.await_args.args[1][0]["payload"]["document_revision_id"] == str(current_id)
            publish.assert_not_awaited()
            async with factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.current_revision_id == current_id
                assert doc.latest_revision_id == latest_id
                assert (await db.get(DocumentRevision, latest_id)).status == "pending"
                assert (await db.get(EmbeddingJob, old_job_id)).status == "superseded"
    asyncio.run(scenario())
