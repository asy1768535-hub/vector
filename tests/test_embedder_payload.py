"""worker payload 构造的安全性测试（#3：用户 metadata 不得覆盖系统保留字段）。"""
from __future__ import annotations

import uuid

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.library import Library
from app.workers.embedder import _build_payload


def _make(doc_metadata):
    lib = Library(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
        slug="lib", name="L", qdrant_collection="lib_col",
        embedding_model="bge-m3", embedding_dim=1024,
        chunk_size=1000, chunk_overlap=120,
    )
    doc = Document(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000bb"),
        library_id=lib.id, external_id="ext-1", title="真实标题",
        doc_metadata=doc_metadata, content_hash="h", status="pending",
    )
    chunk = Chunk(
        id=uuid.UUID("00000000-0000-0000-0000-0000000000cc"),
        document_id=doc.id, library_id=lib.id, seq=3, text="真实正文",
    )
    return lib, doc, chunk


def test_system_fields_win_over_user_metadata():
    """用户在 metadata 里塞同名保留字段，也不能覆盖系统值。"""
    lib, doc, chunk = _make({
        "document_id": "伪造的文档id",
        "chunk_id": "伪造的分片id",
        "text": "伪造正文",
        "title": "伪造标题",
        "library_id": "伪造库id",
        "external_id": "伪造外键",
        "seq": 999,
    })
    p = _build_payload(lib, doc, chunk)
    assert p["document_id"] == str(doc.id)
    assert p["chunk_id"] == str(chunk.id)
    assert p["library_id"] == str(lib.id)
    assert p["text"] == "真实正文"
    assert p["title"] == "真实标题"
    assert p["external_id"] == "ext-1"
    assert p["seq"] == 3


def test_legit_metadata_preserved():
    """非保留字段的业务 metadata 正常带入 payload。"""
    lib, doc, chunk = _make({"author": "张三", "year": 2024})
    p = _build_payload(lib, doc, chunk)
    assert p["author"] == "张三"
    assert p["year"] == 2024
    assert p["document_id"] == str(doc.id)


def test_none_metadata_ok():
    lib, doc, chunk = _make(None)
    p = _build_payload(lib, doc, chunk)
    assert p["document_id"] == str(doc.id)
    assert p["text"] == "真实正文"
