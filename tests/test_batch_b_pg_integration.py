"""批次 B 真实 PostgreSQL 集成测试（#7）。默认 SKIP（需 VECTOR_KB_PG_TEST_DSN）。

覆盖：cleanup outbox 幂等键去重；cleanup worker 失败 → 指数退避重排（pending + available_at 未来 + attempt++）。
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.db import Base
from app.models.cleanup_outbox import CleanupOutbox, EVENT_DELETE_DOCUMENT_ALL
from app.models.library import Library
from app.services import cleanup as cleanup_svc
from app.workers import cleanup as cleanup_worker

_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="设置 VECTOR_KB_PG_TEST_DSN 指向可丢弃测试库后运行")


async def _engine():
    eng = create_async_engine(_DSN)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return eng


def _lib(slug):
    return Library(slug=slug, name="IT", qdrant_collection=f"c_{slug}",
                   embedding_model="m", embedding_dim=8, chunk_size=1000, chunk_overlap=120,
                   lifecycle_mode="managed", index_state="ready")


def test_enqueue_idempotent_one_row():
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        slug = "itb_" + uuid.uuid4().hex[:8]
        try:
            async with Session() as s:
                lib = _lib(slug); s.add(lib); await s.commit(); lib_id = lib.id
            doc_id = uuid.uuid4()
            async with Session() as s:
                lib = await s.get(Library, lib_id)
                await cleanup_svc.enqueue_delete_document(s, lib, doc_id)
                await cleanup_svc.enqueue_delete_document(s, lib, doc_id)   # 同幂等键再入
                await s.commit()
            async with Session() as s:
                cnt = len((await s.execute(select(CleanupOutbox).where(
                    CleanupOutbox.library_id == lib_id))).scalars().all())
            assert cnt == 1
        finally:
            await eng.dispose()
    asyncio.run(run())


def test_reset_stale_marks_max_attempts_failed():
    """超时 processing 行：已达 max_attempts → failed（否则 reset 成 pending 后 _claim 永不领取 → 卡死）；
    未达上限 → pending。"""
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        slug = "itb_" + uuid.uuid4().hex[:8]
        try:
            async with Session() as s:
                lib = _lib(slug); s.add(lib); await s.commit(); lib_id = lib.id
            old = datetime(2000, 1, 1, tzinfo=timezone.utc)   # 远古 claimed_at → 必然 stale
            at_max, below = uuid.uuid4(), uuid.uuid4()
            async with Session() as s:
                from app.config import settings as cfg
                s.add(CleanupOutbox(id=at_max, event_type=EVENT_DELETE_DOCUMENT_ALL, library_id=lib_id,
                                    document_id=uuid.uuid4(), collection_name=f"c_{slug}",
                                    idempotency_key=f"k-max-{at_max}", status="processing",
                                    attempt_count=cfg.cleanup_worker_max_attempts, claimed_at=old))
                s.add(CleanupOutbox(id=below, event_type=EVENT_DELETE_DOCUMENT_ALL, library_id=lib_id,
                                    document_id=uuid.uuid4(), collection_name=f"c_{slug}",
                                    idempotency_key=f"k-below-{below}", status="processing",
                                    attempt_count=1, claimed_at=old))
                await s.commit()
            async with Session() as s:
                await cleanup_worker._reset_stale(s)
            async with Session() as s:
                r_max = await s.get(CleanupOutbox, at_max)
                r_below = await s.get(CleanupOutbox, below)
            assert r_max.status == "failed"      # 达上限 → 死信，不再卡在 pending
            assert r_below.status == "pending"   # 未达上限 → 重试
        finally:
            await eng.dispose()
    asyncio.run(run())


def test_cleanup_worker_retries_with_backoff_on_failure():
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        slug = "itb_" + uuid.uuid4().hex[:8]
        try:
            async with Session() as s:
                lib = _lib(slug); s.add(lib); await s.commit(); lib_id = lib.id
            # 模拟已 claim 一次的 processing 行（attempt_count=1）
            rid = uuid.uuid4()
            async with Session() as s:
                s.add(CleanupOutbox(
                    id=rid, event_type=EVENT_DELETE_DOCUMENT_ALL, library_id=lib_id,
                    document_id=uuid.uuid4(), collection_name=f"c_{slug}",
                    idempotency_key=f"k-{rid}", status="processing", attempt_count=1))
                await s.commit()
                row = await s.get(CleanupOutbox, rid)

            # Qdrant 删除失败 → _process 应退避重排为 pending、available_at 未来、保留 last_error
            with patch("app.services.qdrant.delete_points_by_document_id",
                       new_callable=AsyncMock, side_effect=RuntimeError("qdrant down")):
                async with Session() as s:
                    r = await s.get(CleanupOutbox, rid)
                    await cleanup_worker._process(s, r)

            async with Session() as s:
                r = await s.get(CleanupOutbox, rid)
            assert r.status == "pending"
            assert r.available_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
            assert "qdrant down" in (r.last_error or "")
        finally:
            await eng.dispose()
    asyncio.run(run())
