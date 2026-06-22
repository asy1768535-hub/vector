"""真实 PostgreSQL 集成测试：证明并发唯一冲突只回滚冲突文档，不连累同批已成功文档。

默认 SKIP。需要一个**可丢弃的测试库**（不要指向生产库！），用环境变量提供 DSN：

    # PowerShell
    $env:VECTOR_KB_PG_TEST_DSN = "postgresql+asyncpg://postgres:<pw>@127.0.0.1:5434/vector_kb_test"
    .venv/Scripts/python.exe -m pytest tests/test_ingest_pg_integration.py -q

测试会在该库上 create_all 全部表（含部分唯一索引），跑完不自动清表。

场景（复刻批量导入里后条冲突的真实事务边界）：
  - S1 用 external_id=DUP 插入并 flush（不提交，持锁）。
  - S2 先成功摄入一条不同身份的文档 A（flush）；再摄入 external_id=DUP：
    _find_active 此刻看不到 S1 未提交行 → 进入 INSERT → 在唯一索引上阻塞。
  - 提交 S1 释放锁 → S2 的 flush 抛 IntegrityError → savepoint 只回滚这一条 →
    重查拿到 S1 的胜出记录返回。
  - 提交 S2：文档 A 必须仍然在库（没有被 session 级 rollback 连累）。
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  注册全部表，保证 create_all 建出 FK 依赖表
from app.db import Base
from app.models.document import Document
from app.models.library import Library
from app.services.ingest import ingest_text

_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="设置 VECTOR_KB_PG_TEST_DSN 指向可丢弃测试库后运行")


async def _scenario(dsn: str) -> None:
    engine = create_async_engine(dsn)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    slug = "itest_" + uuid.uuid4().hex[:8]
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        lib = Library(
            slug=slug, name="IT", qdrant_collection=f"{slug}_col",
            embedding_model="bge-m3", embedding_dim=8, chunk_size=1000, chunk_overlap=120,
        )
        async with Session() as s:
            s.add(lib)
            await s.commit()
            lib_id = lib.id

        s1 = Session()
        s2 = Session()
        try:
            lib1 = await s1.get(Library, lib_id)
            lib2 = await s2.get(Library, lib_id)

            # S1：占住 external_id=DUP（flush 持锁，不提交）
            await ingest_text(db=s1, library=lib1, text="s1-body", title="s1",
                              external_id="DUP", metadata=None, splitter="text",
                              created_by=None, chunks=["s1-body"])

            # S2：先成功摄入一条不同身份的文档 A
            docA, _, _, existA = await ingest_text(
                db=s2, library=lib2, text="A-body", title="A",
                external_id="A1", metadata=None, splitter="text",
                created_by=None, chunks=["A-body"])
            assert existA is False
            docA_id = docA.id

            # S2：再摄入 external_id=DUP —— 会在唯一索引上阻塞，等 S1 提交后冲突
            async def s2_conflict():
                return await ingest_text(
                    db=s2, library=lib2, text="s2-body", title="s2",
                    external_id="DUP", metadata=None, splitter="text",
                    created_by=None, chunks=["s2-body"])

            task = asyncio.create_task(s2_conflict())
            await asyncio.sleep(0.3)      # 让 S2 抵达阻塞的 flush
            await s1.commit()             # 释放锁 → S2 flush 抛 IntegrityError → savepoint 路径
            winner, _, _, existDup = await task

            assert existDup is True                 # 解析为已存在的胜出记录
            assert winner.external_id == "DUP"

            await s2.commit()             # 文档 A 必须仍可提交（没被连累回滚）
        finally:
            await s1.close()
            await s2.close()

        # 校验：文档 A 仍在库；DUP 恰好一条活动行（S1 的）
        async with Session() as s:
            a = await s.get(Document, docA_id)
            assert a is not None and a.deleted_at is None
            dup_count = (await s.execute(
                select(func.count()).select_from(Document).where(
                    Document.library_id == lib_id,
                    Document.external_id == "DUP",
                    Document.deleted_at.is_(None),
                )
            )).scalar_one()
            assert dup_count == 1
    finally:
        await engine.dispose()


def test_pg_concurrent_conflict_preserves_sibling_in_batch():
    asyncio.run(_scenario(_DSN))
