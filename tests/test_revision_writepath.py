"""#6 写路径 revision 生成：新建 rev=1、reingest +1 + supersede、_new_generation。"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

from app.models.document import Document
from app.models.library import Library
from app.services import ingest as ing


def _lib():
    return Library(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
        slug="lib", name="L", qdrant_collection="lib_col",
        embedding_model="m", embedding_dim=8, chunk_size=1000, chunk_overlap=120,
    )


def _mock_db(added):
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.add = MagicMock(side_effect=lambda o: added.append(o))
    db.add_all = MagicMock()
    db.flush = AsyncMock()
    return db


def test_new_generation_bumps_revision_and_supersedes():
    lib = _lib()
    doc = Document(id=uuid.uuid4(), library_id=lib.id, content_hash="h",
                   current_revision=3, status="ready")
    added = []
    db = _mock_db(added)
    job = asyncio.run(ing._new_generation(db, lib, doc))
    assert doc.current_revision == 4                 # +1
    assert job.document_revision == 4                # job 带新 revision
    assert job.status == "pending"
    assert job.rebuild_operation_id is None
    # supersede 旧 job 的 UPDATE 被执行（execute 至少一次）
    assert db.execute.await_count >= 1


def test_new_generation_carries_rebuild_operation_id():
    lib = _lib()
    doc = Document(id=uuid.uuid4(), library_id=lib.id, content_hash="h",
                   current_revision=1, status="ready")
    op_id = uuid.uuid4()
    job = asyncio.run(ing._new_generation(_mock_db([]), lib, doc, rebuild_operation_id=op_id))
    assert doc.current_revision == 2
    assert job.rebuild_operation_id == op_id


def test_ingest_text_new_doc_revision_1():
    """新建文档：current_revision=1，job.document_revision=1。"""
    lib = _lib()
    none_res = MagicMock()
    none_res.scalars.return_value.first.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=none_res)   # _find_active → None
    added = []
    db.add = MagicMock(side_effect=lambda o: added.append(o))
    db.add_all = MagicMock()
    db.flush = AsyncMock()
    # begin_nested 作为 async 上下文管理器
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=cm)

    doc, job, cnt, was = asyncio.run(ing.ingest_text(
        db=db, library=lib, text="正文内容", title="t", external_id=None,
        metadata=None, splitter="text", created_by=None, chunks=["正文内容"],
    ))
    assert was is False
    assert doc.current_revision == 1
    assert job.document_revision == 1
    assert cnt == 1
