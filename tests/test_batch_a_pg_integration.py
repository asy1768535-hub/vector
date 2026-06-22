"""批次 A 真实 PostgreSQL 集成测试（#6）。默认 SKIP。

需可丢弃测试库（勿指向生产库）：
    $env:VECTOR_KB_PG_TEST_DSN = "postgresql+asyncpg://postgres:<pw>@127.0.0.1:5434/vector_kb_test"
    .venv/Scripts/python.exe -m pytest tests/test_batch_a_pg_integration.py -q

覆盖：
  - 迁移/模型部分唯一索引存在且谓词正确（uq_jobs_doc_rev_active / uq_jobs_op_doc）
  - uq_jobs_doc_rev_active 真正拒绝重复活动 job
  - 锁互斥：FOR KEY SHARE 之间不阻塞；FOR UPDATE 被 FOR KEY SHARE 阻塞（lock_timeout 触发）
  - finalize 并发只完成一次（幂等）
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  注册全部表
from app.db import Base
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.rebuild_operation import RebuildOperation
from app.services import rebuild as rebuild_svc

_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="设置 VECTOR_KB_PG_TEST_DSN 指向可丢弃测试库后运行")


async def _engine():
    eng = create_async_engine(_DSN)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return eng


async def _seed_lib(Session, slug):
    async with Session() as s:
        lib = Library(slug=slug, name="IT", qdrant_collection=f"c_{slug}",
                      embedding_model="m", embedding_dim=8, chunk_size=1000, chunk_overlap=120,
                      lifecycle_mode="managed", index_state="ready")
        s.add(lib); await s.flush()
        doc = Document(library_id=lib.id, content_hash="h", current_revision=1, status="pending")
        s.add(doc); await s.commit()
        return lib.id, doc.id


def test_partial_unique_indexes_exist():
    async def run():
        eng = await _engine()
        try:
            async with eng.connect() as c:
                rows = (await c.execute(text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename='embedding_jobs' AND indexname IN "
                    "('uq_jobs_doc_rev_active','uq_jobs_op_doc')"
                ))).all()
            defs = {n: d for n, d in rows}
            assert "uq_jobs_doc_rev_active" in defs
            assert "uq_jobs_op_doc" in defs
            assert "pending" in defs["uq_jobs_doc_rev_active"] and "processing" in defs["uq_jobs_doc_rev_active"]
            assert "rebuild_operation_id IS NOT NULL" in defs["uq_jobs_op_doc"]
        finally:
            await eng.dispose()
    asyncio.run(run())


def test_uq_jobs_doc_rev_active_rejects_duplicate():
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        slug = "it_" + uuid.uuid4().hex[:8]
        try:
            lib_id, doc_id = await _seed_lib(Session, slug)
            async with Session() as s:
                s.add(EmbeddingJob(library_id=lib_id, document_id=doc_id, status="pending", document_revision=1))
                await s.commit()
            with pytest.raises(IntegrityError):
                async with Session() as s:
                    s.add(EmbeddingJob(library_id=lib_id, document_id=doc_id, status="pending", document_revision=1))
                    await s.commit()
        finally:
            await eng.dispose()
    asyncio.run(run())


def test_lock_mutex_key_share_vs_for_update():
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        slug = "it_" + uuid.uuid4().hex[:8]
        try:
            lib_id, _ = await _seed_lib(Session, slug)
            s1, s2, s3 = Session(), Session(), Session()
            a = await s1.__aenter__(); b = await s2.__aenter__(); c = await s3.__aenter__()
            try:
                # S1 持 FOR KEY SHARE
                await a.execute(select(Library.id).where(Library.id == lib_id).with_for_update(read=True, key_share=True))
                # S2 也取 FOR KEY SHARE：不应阻塞（短超时内成功）
                await b.execute(text("SET LOCAL lock_timeout='1500'"))
                await b.execute(select(Library.id).where(Library.id == lib_id).with_for_update(read=True, key_share=True))
                # S3 取 FOR UPDATE：应被 S1 的 KEY SHARE 阻塞 → lock_timeout 触发
                await c.execute(text("SET LOCAL lock_timeout='800'"))
                with pytest.raises(DBAPIError):
                    await c.execute(select(Library.id).where(Library.id == lib_id).with_for_update())
            finally:
                await s1.__aexit__(None, None, None)
                await s2.__aexit__(None, None, None)
                await s3.__aexit__(None, None, None)
        finally:
            await eng.dispose()
    asyncio.run(run())


def test_finalize_concurrent_completes_once():
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        slug = "it_" + uuid.uuid4().hex[:8]
        try:
            lib_id, doc_id = await _seed_lib(Session, slug)
            # 构造 running operation + 1 条 done job（expected=1）
            async with Session() as s:
                op = RebuildOperation(library_id=lib_id, collection_name="c", status="running", expected_job_count=1)
                s.add(op); await s.flush()
                lib = await s.get(Library, lib_id)
                lib.index_state = "rebuilding"; lib.active_rebuild_operation_id = op.id
                s.add(EmbeddingJob(library_id=lib_id, document_id=doc_id, status="done",
                                   document_revision=2, rebuild_operation_id=op.id))
                await s.commit(); op_id = op.id

            async def fin():
                async with Session() as s:
                    return await rebuild_svc.try_finalize(s, op_id)

            r1, r2 = await asyncio.gather(fin(), fin())     # 并发 finalize
            assert (r1, r2).count(True) == 1                # 恰好一次推进终态
            async with Session() as s:
                op = await s.get(RebuildOperation, op_id)
                lib = await s.get(Library, lib_id)
            assert op.status == "done"
            assert lib.index_state == "ready" and lib.active_rebuild_operation_id is None
        finally:
            await eng.dispose()
    asyncio.run(run())
