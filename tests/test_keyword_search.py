"""keyword_search 服务：库隔离、过滤删档、JOIN documents、命中结构、trgm/ILIKE 分支、分词。"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import keyword_search as K


class _Result:
    def __init__(self, *, scalar=None, rows=None):
        self._s = scalar
        self._rows = rows or []

    def scalar(self):
        return self._s

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, *, trgm=True, rows=None):
        self.trgm = trgm
        self.rows = rows or []
        self.calls = []          # [(sql_text, params)]

    async def execute(self, sql, params=None):
        s = str(sql)
        self.calls.append((s, params))
        if "pg_extension" in s:
            return _Result(scalar=1 if self.trgm else None)
        return _Result(rows=self.rows)


def _row(**kw):
    kw.setdefault("chunk_id", uuid.uuid4())
    kw.setdefault("document_id", uuid.uuid4())
    kw.setdefault("library_id", uuid.uuid4())
    kw.setdefault("text", "正文")
    kw.setdefault("title", "标题")
    kw.setdefault("external_id", None)
    kw.setdefault("document_revision", 1)
    kw.setdefault("score", 0.7)
    return SimpleNamespace(**kw)


def _lib():
    return SimpleNamespace(id=uuid.uuid4())


@pytest.fixture(autouse=True)
def _reset_trgm_cache():
    K._trgm_available = None      # 每个用例独立判定 trgm
    yield
    K._trgm_available = None


def _recall(db, lib, q, limit=10):
    return asyncio.run(K.recall(db, lib, q, limit=limit))


# ── 库隔离 + 过滤删档 + JOIN ─────────────────────────────────────────────────
def test_query_filters_deleted_joins_documents_and_scopes_library():
    lib = _lib()
    db = _FakeDB(trgm=True, rows=[_row()])
    _recall(db, lib, "JGJ250-2011 八大员")
    main = [c for c in db.calls if "pg_extension" not in c[0]][0]
    sql, params = main
    assert "join documents" in sql.lower()
    assert "d.deleted_at is null" in sql.lower()       # 必过滤删档
    assert "c.library_id = :lib" in sql.lower()        # 必限定库
    assert params["lib"] == str(lib.id)                # 限定到该库


def test_trgm_path_uses_word_similarity():
    db = _FakeDB(trgm=True, rows=[_row()])
    _recall(db, _lib(), "保障农民工工资条例")
    main = [c for c in db.calls if "pg_extension" not in c[0]][0]
    assert "word_similarity" in main[0].lower()


def test_fallback_path_uses_ilike_when_no_trgm():
    db = _FakeDB(trgm=False, rows=[_row()])
    _recall(db, _lib(), "JGJ250-2011 桂建通")
    main = [c for c in db.calls if "pg_extension" not in c[0]][0]
    sql = main[0].lower()
    assert "ilike" in sql and "word_similarity" not in sql
    assert "d.deleted_at is null" in sql and "c.library_id = :lib" in sql   # 回退路径同样隔离


# ── 命中结构（可与 dense 融合） ──────────────────────────────────────────────
def test_hit_shape_matches_dense():
    cid, did, lid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = _FakeDB(trgm=True, rows=[_row(chunk_id=cid, document_id=did, library_id=lid,
                                       text="八大员…", title="职业标准", external_id="JGJ250-2011",
                                       document_revision=3, score=0.8)])
    hits = _recall(db, _lib(), "八大员")
    assert len(hits) == 1
    h = hits[0]
    assert h["id"] == str(cid) and h["score"] == 0.8
    assert h["payload"]["chunk_id"] == str(cid)
    assert h["payload"]["document_id"] == str(did)
    assert h["payload"]["title"] == "职业标准" and h["payload"]["external_id"] == "JGJ250-2011"
    # P1 修复：必须带 document_revision（取自 documents.current_revision），否则可见性会误过滤
    assert h["payload"]["document_revision"] == 3


def test_query_selects_current_revision():
    db = _FakeDB(trgm=True, rows=[_row()])
    _recall(db, _lib(), "保障农民工工资条例")
    main = [c for c in db.calls if "pg_extension" not in c[0]][0]
    assert "current_revision as document_revision" in main[0].lower()


def test_empty_query_returns_empty_without_db():
    db = _FakeDB(rows=[_row()])
    assert _recall(db, _lib(), "   ") == []
    assert db.calls == []          # 空 query 不查库


# ── 分词（文件名/文号 token） ───────────────────────────────────────────────
def test_tokens_extract_filename_and_docno_tokens():
    toks = K._tokens("JGJ250-2011 桂建通 第38条")
    assert "JGJ250" in toks and "2011" in toks     # 标准号按 '-' 拆成可匹配 token
    assert "桂建通" in toks
    assert "第38条" in toks                          # CJK+数字连写保留为整体 token
    assert all(len(t) >= 2 for t in toks)
