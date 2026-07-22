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
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  注册全部表
from app.db import Base
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.organization import (
    DEFAULT_ORGANIZATION_ID,
    DEFAULT_ORGANIZATION_SLUG,
    Organization,
)
from app.models.rebuild_operation import RebuildOperation
from app.services import rebuild as rebuild_svc

_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="设置 VECTOR_KB_PG_TEST_DSN 指向可丢弃测试库后运行")


async def _engine():
    eng = create_async_engine(_DSN)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
        await c.execute(
            insert(Organization)
            .values(
                id=DEFAULT_ORGANIZATION_ID,
                slug=DEFAULT_ORGANIZATION_SLUG,
                name="Default Organization",
                deployment_profile="private",
                status="active",
            )
            .on_conflict_do_nothing(index_elements=[Organization.id])
        )
    return eng


async def _seed_lib(Session, slug):
    async with Session() as s:
        lib = Library(slug=slug, name="IT", qdrant_collection=f"c_{slug}",
                      embedding_model="m", embedding_dim=8, chunk_size=1000, chunk_overlap=120,
                      lifecycle_mode="managed", index_state="ready")
        s.add(lib)
        await s.flush()
        doc = Document(library_id=lib.id, content_hash="h", current_revision=1, status="pending")
        s.add(doc)
        await s.commit()
        return lib.id, doc.id


def test_alembic_upgrade_0001_to_0010_builds_schema(monkeypatch):
    """真正跑 Alembic 0001→0010（而非 create_all），验证迁移链可用且与 ORM 结构一致。

    **只用显式测试 DSN（VECTOR_KB_PG_TEST_DSN）解析连接参数**，绝不用 settings.db_*（避免误碰
    正式实例）。env.py 用 settings.db_dsn_sync，故把 settings.db_* 全部临时指向**测试实例**上的
    临时库（monkeypatch 自动还原）。
    """
    import asyncpg
    from urllib.parse import urlparse
    from alembic import command
    from alembic.config import Config
    from app.config import settings

    u = urlparse(_DSN.replace("+asyncpg", ""))   # 测试实例连接参数（仅来自显式测试 DSN）
    host, port, user, pwd = u.hostname, u.port or 5432, u.username, u.password

    async def _admin(sql):
        conn = await asyncpg.connect(host=host, port=port, user=user, password=pwd, database="postgres")
        await conn.execute(sql)
        await conn.close()

    async def _check(name):
        eng = create_async_engine(f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{name}")
        try:
            async with eng.connect() as c:
                ver = (await c.execute(text("SELECT version_num FROM alembic_version"))).scalar()
                idx = (await c.execute(text(
                    "SELECT count(*) FROM pg_indexes WHERE tablename='embedding_jobs' "
                    "AND indexname IN ('uq_jobs_doc_rev_active','uq_jobs_op_doc')"))).scalar()
                dflt = (await c.execute(text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name='embedding_jobs' AND column_name='document_revision'"))).scalar()
                # 批次 B：outbox 表 + claim 索引存在
                outbox = (await c.execute(text(
                    "SELECT to_regclass('public.qdrant_cleanup_outbox')"))).scalar()
                ob_idx = (await c.execute(text(
                    "SELECT count(*) FROM pg_indexes WHERE tablename='qdrant_cleanup_outbox' "
                    "AND indexname='ix_cleanup_claim'"))).scalar()
            return ver, idx, dflt, outbox, ob_idx
        finally:
            await eng.dispose()

    name = "vkt_alembic_" + uuid.uuid4().hex[:8]
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS {name}'))
    asyncio.run(_admin(f'CREATE DATABASE {name}'))
    # 把 settings 全部指向测试实例的临时库（env.py 据此构造 sync DSN）；monkeypatch 退出自动还原
    monkeypatch.setattr(settings, "db_host", host)
    monkeypatch.setattr(settings, "db_port", int(port))
    monkeypatch.setattr(settings, "db_user", user)
    monkeypatch.setattr(settings, "db_password", pwd)
    monkeypatch.setattr(settings, "db_name", name)
    try:
        command.upgrade(Config("alembic.ini"), "0010")
        ver, idx, dflt, outbox, ob_idx = asyncio.run(_check(name))
        assert ver == "0010"
        assert idx == 2                  # 两个活动唯一索引经迁移建出
        assert dflt is None              # document_revision 无 DB 默认（与 ORM 一致）
        assert outbox is not None        # 批次 B：outbox 表存在
        assert ob_idx == 1               # claim 部分索引存在
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS {name}'))


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
            a = await s1.__aenter__()
            b = await s2.__aenter__()
            c = await s3.__aenter__()
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


def test_retry_jobs_guard_leaves_processing_untouched():
    """模拟「retry-1 已把 op 转 running、worker 正在 processing；retry-2 进来」：
    _retry_jobs 锁内复核 status!=failed → no-op，**processing 任务的 status/worker_id/
    claimed_at/attempt_count 完全不变**（旧 bug 会无条件重置回 pending，该断言会失败）。"""
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        slug = "it_" + uuid.uuid4().hex[:8]
        try:
            lib_id, doc_id = await _seed_lib(Session, slug)
            from datetime import datetime, timezone
            claimed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            async with Session() as s:
                op = RebuildOperation(library_id=lib_id, collection_name="c",
                                      status="running", expected_job_count=1)
                s.add(op)
                await s.flush()
                lib = await s.get(Library, lib_id)
                lib.index_state = "rebuilding"
                lib.active_rebuild_operation_id = op.id
                jid = uuid.uuid4()
                s.add(EmbeddingJob(id=jid, library_id=lib_id, document_id=doc_id, status="processing",
                                   document_revision=2, rebuild_operation_id=op.id,
                                   worker_id="w-1", claimed_at=claimed, attempt_count=1))
                await s.commit()
                op_id = op.id

            # retry-2 进来：op 仍 running → _retry_jobs 必须 no-op
            async with Session() as s:
                advanced = await rebuild_svc._retry_jobs(s, lib_id, op_id)
            assert advanced is False

            async with Session() as s:
                j = (await s.execute(select(EmbeddingJob).where(EmbeddingJob.id == jid))).scalar_one()
            assert j.status == "processing"          # 未被重置
            assert j.worker_id == "w-1"
            assert j.attempt_count == 1
            assert j.claimed_at is not None and j.claimed_at.replace(tzinfo=timezone.utc) == claimed
        finally:
            await eng.dispose()
    asyncio.run(run())


def test_prepare_rejects_external_under_stale_cache():
    """P0 复现：S1 缓存 managed；另一事务改 external 后，run_rebuild(S1) 仍因
    _prepare 的 populate_existing fresh 读而抛 ExternalLibraryError（不会创建 operation）。"""
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        slug = "it_" + uuid.uuid4().hex[:8]
        try:
            lib_id, _ = await _seed_lib(Session, slug)
            s1 = Session()
            try:
                await s1.get(Library, lib_id)            # S1 缓存 managed
                async with Session() as s2:              # 另一事务改 external
                    l2 = await s2.get(Library, lib_id)
                    l2.lifecycle_mode = "external"
                    await s2.commit()
                with pytest.raises(rebuild_svc.ExternalLibraryError):
                    await rebuild_svc.run_rebuild(s1, lib_id)
            finally:
                await s1.close()
            # 未创建任何 operation
            async with Session() as s:
                cnt = (await s.execute(select(func.count()).select_from(RebuildOperation).where(
                    RebuildOperation.library_id == lib_id))).scalar_one()
            assert cnt == 0
        finally:
            await eng.dispose()
    asyncio.run(run())


def test_concurrent_first_rebuild_one_conflicts_cleanly():
    """P1 复现：两个并发首次 run_rebuild → 一个成功，另一个抛 ConcurrentRebuildError
    （明确业务冲突，而非 uq_rebuild_op_active_per_lib 的 IntegrityError）。Qdrant 被 mock。"""
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        slug = "it_" + uuid.uuid4().hex[:8]
        try:
            lib_id, _ = await _seed_lib(Session, slug)

            async def rebuild():
                async with Session() as s:
                    try:
                        return await rebuild_svc.run_rebuild(s, lib_id)
                    except rebuild_svc.ConcurrentRebuildError:
                        return "CONFLICT"

            from unittest.mock import patch, AsyncMock
            with patch("app.services.qdrant.delete_collection", new_callable=AsyncMock), \
                 patch("app.services.qdrant.ensure_collection", new_callable=AsyncMock):
                r1, r2 = await asyncio.gather(rebuild(), rebuild())
            outcomes = sorted([r1, r2])
            assert outcomes.count("CONFLICT") == 1          # 恰一个干净冲突
            assert any(o != "CONFLICT" for o in outcomes)   # 另一个成功
            # 且只有一个 operation 被建
            async with Session() as s:
                cnt = (await s.execute(select(func.count()).select_from(RebuildOperation).where(
                    RebuildOperation.library_id == lib_id))).scalar_one()
            assert cnt == 1
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
                s.add(op)
                await s.flush()
                lib = await s.get(Library, lib_id)
                lib.index_state = "rebuilding"
                lib.active_rebuild_operation_id = op.id
                s.add(EmbeddingJob(library_id=lib_id, document_id=doc_id, status="done",
                                   document_revision=2, rebuild_operation_id=op.id))
                await s.commit()
                op_id = op.id

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
