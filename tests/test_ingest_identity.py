"""#4 文档身份规则 + 并发 IntegrityError 处理的单测。

模型用 PG 专属类型，无法用 SQLite 真跑，这里测两个确定性点：
  1. _find_active 按 external_id / content_hash 构造正确的 WHERE（编译 SQL 断言）。
  2. ingest_text 在 flush 撞唯一索引（IntegrityError）时回滚并返回胜出记录。
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.document import Document
from app.models.library import Library
from app.services.ingest import _find_active, ingest_text


def _lib():
    return Library(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
        slug="lib", name="L", qdrant_collection="lib_col",
        embedding_model="bge-m3", embedding_dim=1024,
        chunk_size=1000, chunk_overlap=120,
    )


class _CapturingDB:
    """记录传入 execute 的 statement，并返回固定结果。"""
    def __init__(self, result):
        self.captured = []
        self._result = result

    async def execute(self, stmt):
        self.captured.append(stmt)
        return self._result


def _none_result():
    r = MagicMock()
    r.scalars.return_value.first.return_value = None
    return r


def _where(sql: str) -> str:
    """取编译 SQL 的 WHERE 子句（content_hash 总在 SELECT 列里出现，须只看 WHERE）。"""
    low = " ".join(sql.lower().split())     # 归一化换行/多空格
    return low[low.index(" where "):]


def test_find_active_uses_external_id_not_content_hash():
    db = _CapturingDB(_none_result())
    asyncio.run(_find_active(db, _lib().id, "ext-1", "deadbeef"))
    where = _where(str(db.captured[0]))
    assert "external_id =" in where
    assert "content_hash" not in where      # 带 external_id 时绝不按内容去重


def test_find_active_uses_content_hash_when_no_external_id():
    db = _CapturingDB(_none_result())
    asyncio.run(_find_active(db, _lib().id, None, "deadbeef"))
    where = _where(str(db.captured[0]))
    assert "content_hash =" in where
    assert "external_id is null" in where   # 仅匹配无 external_id 的行


def _nested_cm():
    """模拟 db.begin_nested() 返回的 async 上下文管理器（savepoint）。

    __aexit__ 返回 False → 块内异常照常向外传播（由 ingest_text 的 except 捕获）。
    """
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_db(execute_side_effect, *, flush_raises):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute_side_effect)
    db.add = MagicMock()
    db.add_all = MagicMock()
    db.begin_nested = MagicMock(return_value=_nested_cm())
    db.flush = AsyncMock(side_effect=flush_raises)
    db.rollback = AsyncMock()
    db.expunge = MagicMock()
    db.__contains__ = MagicMock(return_value=False)  # doc in db → False，跳过 expunge
    return db


def test_integrityerror_savepoint_returns_winner_without_session_rollback():
    """撞唯一索引：用 savepoint 回滚本条并返回胜出记录，绝不调 session 级 rollback。"""
    lib = _lib()
    winner = Document(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000bb"),
        library_id=lib.id, external_id="ext-1", content_hash="h", status="ready",
    )
    none_res = _none_result()
    win_res = MagicMock()
    win_res.scalars.return_value.first.return_value = winner
    job_res = MagicMock()
    job_res.scalars.return_value.first.return_value = None
    cnt_res = MagicMock()
    cnt_res.scalar_one.return_value = 3

    db = _make_db(
        [none_res, win_res, job_res, cnt_res],
        flush_raises=IntegrityError("stmt", {}, Exception("dup")),
    )

    doc, job, count, was_existing = asyncio.run(ingest_text(
        db=db, library=lib, text="正文", title="t", external_id="ext-1",
        metadata=None, splitter="text", created_by=None, chunks=["正文"],
    ))

    assert was_existing is True
    assert doc is winner
    assert count == 3
    db.begin_nested.assert_called_once()          # 走了 savepoint
    db.rollback.assert_not_called()               # 关键：没有整 session 回滚（批量安全）


def test_integrityerror_non_identity_conflict_reraises():
    """flush 抛 IntegrityError 但重查仍无胜出记录（非身份冲突）→ 抛出，不吞。"""
    lib = _lib()
    db = _make_db(
        [_none_result(), _none_result()],          # 初查 None；重查仍 None
        flush_raises=IntegrityError("stmt", {}, Exception("other")),
    )
    with pytest.raises(IntegrityError):
        asyncio.run(ingest_text(
            db=db, library=lib, text="正文", title="t", external_id=None,
            metadata=None, splitter="text", created_by=None, chunks=["正文"],
        ))
    db.rollback.assert_not_called()
