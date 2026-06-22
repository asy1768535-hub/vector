from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from fastapi import status

from app.main import app
from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.models.user import User
from app.models.library import Library
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.schemas.documents import QueryRequest

# Mock user and library
mock_user = User(
    id="00000000-0000-0000-0000-000000000001",
    email="test@example.com",
    is_superuser=True,
    is_active=True,
)
mock_library = Library(
    id="00000000-0000-0000-0000-000000000002",
    slug="testlib",
    name="Test Library",
    qdrant_collection="testlib_col",
    embedding_model="bge-m3",
    embedding_base_url="http://mock-embeddings",
    embedding_dim=1024,
    chunk_size=1000,
    chunk_overlap=120,
    lifecycle_mode="managed",
    index_state="ready",
)

async def override_user():
    return mock_user


def make_db_mock(*, existing=None, scalar_list=None, get_return=None):
    """构造贴近真实 AsyncSession 的 mock。

    关键点：`await db.execute(...)` 返回的是同步 Result，其 .scalars()/.first()/.all()
    都是同步方法——裸 AsyncMock 会把它们变成协程导致链式调用炸。这里显式建一个同步
    Result，让 external_id 查重等 `(await db.execute(...)).scalars().first()` 正常工作。
    """
    db = AsyncMock()  # commit/flush/get/rollback 是 async → AsyncMock 合适
    result = MagicMock()
    result.scalars.return_value.first.return_value = existing
    result.scalars.return_value.all.return_value = scalar_list or []
    result.scalar_one.return_value = 0
    result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=result)
    if get_return is not None:
        db.get = AsyncMock(return_value=get_return)
    return db


async def override_db():
    return make_db_mock()

@pytest.fixture
def client():
    # Default mock user to superuser
    mock_user.is_superuser = True
    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    
    with patch("app.deps.load_active_library", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = mock_library
        yield TestClient(app)
        
    app.dependency_overrides.clear()

def test_query_request_validation():
    # Test valid limit
    req = QueryRequest(query="test query", limit=10)
    assert req.query == "test query"
    assert req.limit == 10

    # Test limit validation constraints (min 1, max 20)
    with pytest.raises(Exception):
        QueryRequest(query="test", limit=0)
    with pytest.raises(Exception):
        QueryRequest(query="test", limit=21)
    with pytest.raises(Exception):
        QueryRequest(query="", limit=5)

@patch("app.services.embedding.embed_one", new_callable=AsyncMock)
@patch("app.services.qdrant.search", new_callable=AsyncMock)
def test_query_library_endpoint(mock_search, mock_embed, client, monkeypatch):
    # 显式关掉 rerank，使该用例不依赖 .env 的全局开关、也不联网（只验 dense 路径）
    monkeypatch.setattr(settings, "rerank_enabled", False)
    monkeypatch.setattr(settings, "rerank_base_url", "")
    # 关掉可见性回查（本用例验 dense/enrich 路径，不验 #6 过滤；过滤有独立单测+E2E）
    monkeypatch.setattr(settings, "retrieval_consistency_filter", False)
    mock_embed.return_value = [0.1] * 1024
    mock_search.return_value = [
        {
            "id": "123",
            "score": 0.85,
            "payload": {
                "text": "matched chunk text",
                "document_id": "doc123",
                "chunk_id": "chunk123",
                "title": "doc title",
                "extra": "value"
            }
        }
    ]

    response = client.post(
        "/libraries/testlib/query",
        json={"query": "test query", "limit": 5}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "results" in data
    assert len(data["results"]) == 1
    assert data["results"][0]["text"] == "matched chunk text"
    assert data["results"][0]["similarity"] == 0.85
    assert data["results"][0]["document_id"] == "doc123"
    # metadata 保留业务字段 + 排查用的 vector_score（无 rerank 时不含 rerank_score）
    assert data["results"][0]["metadata"] == {"extra": "value", "vector_score": 0.85}


@patch("app.services.rerank.rerank", new_callable=AsyncMock)
@patch("app.services.qdrant.search", new_callable=AsyncMock)
@patch("app.services.embedding.embed_one", new_callable=AsyncMock)
def test_query_rerank_backfills_and_keeps_both_scores(mock_embed, mock_search, mock_rerank, client, monkeypatch):
    """rerank 生效路径端到端：召回 3 条、reranker 只返回 1 条 →
    断言补满到 3 条、命中项带 rerank_score、补满项只带 vector_score。"""
    # 全局开启 rerank 并配好地址（lib.rerank_enabled=None → 回退全局）
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_base_url", "http://mock-rerank/rerank")
    monkeypatch.setattr(settings, "rerank_model", "bge-reranker-v2-m3")
    monkeypatch.setattr(settings, "retrieval_consistency_filter", False)  # 本用例验 rerank，不验 #6 过滤

    mock_embed.return_value = [0.1] * 1024
    mock_search.return_value = [
        {"id": "1", "score": 0.90, "payload": {"text": "A", "document_id": "d1", "chunk_id": "c1", "title": "tA"}},
        {"id": "2", "score": 0.80, "payload": {"text": "B", "document_id": "d2", "chunk_id": "c2", "title": "tB"}},
        {"id": "3", "score": 0.70, "payload": {"text": "C", "document_id": "d3", "chunk_id": "c3", "title": "tC"}},
    ]
    # reranker 只返回向量序最差的第 3 条（idx=2），高分上浮到首位
    mock_rerank.return_value = [(2, 0.99)]

    response = client.post("/libraries/testlib/query", json={"query": "q", "limit": 5})
    assert response.status_code == status.HTTP_200_OK
    results = response.json()["results"]

    # 补满：3 条召回全部返回，顺序 = 重排命中 [2] + 原向量序补满 [0,1]
    assert [r["text"] for r in results] == ["C", "A", "B"]

    # 命中项：similarity=rerank 分，metadata 同时含 vector_score 与 rerank_score
    assert results[0]["similarity"] == 0.99
    assert results[0]["metadata"]["vector_score"] == 0.70
    assert results[0]["metadata"]["rerank_score"] == 0.99

    # 补满项：similarity 回退向量分，metadata 只有 vector_score、无 rerank_score
    assert results[1]["similarity"] == 0.90
    assert results[1]["metadata"]["vector_score"] == 0.90
    assert "rerank_score" not in results[1]["metadata"]
    assert results[2]["metadata"]["vector_score"] == 0.80
    assert "rerank_score" not in results[2]["metadata"]

@patch("app.deps.has_permission")
def test_query_library_unauthorized(mock_has_perm, client):
    # Set user to non-superuser to trigger permission checks
    mock_user.is_superuser = False
    mock_has_perm.return_value = False

    response = client.post(
        "/libraries/testlib/query",
        json={"query": "test query", "limit": 5}
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN

@patch("app.services.ingest.ingest_text", new_callable=AsyncMock)
def test_import_txt_file(mock_ingest, client):
    mock_doc = AsyncMock()
    mock_doc.id = "00000000-0000-0000-0000-000000000003"
    mock_doc.status = "pending"
    mock_job = AsyncMock()
    mock_ingest.return_value = (mock_doc, mock_job, 1, False)

    file_content = b"This is plain text content."
    files = {"file": ("test.txt", file_content, "text/plain")}
    response = client.post("/libraries/testlib/import-file", files=files)
    
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["status"] == "success"
    assert data["imported_count"] == 1
    assert data["documents"][0]["title"] == "test.txt"
    assert data["documents"][0]["chunk_count"] == 1
    
    mock_ingest.assert_called_once()
    kwargs = mock_ingest.call_args[1]
    assert kwargs["text"] == "This is plain text content."
    assert kwargs["title"] == "test.txt"
    assert kwargs.get("chunks") is None          # 纯文本不预切，chunks 不传

@patch("app.services.ingest.ingest_text", new_callable=AsyncMock)
def test_import_xlsx_passes_table_aware_chunks(mock_ingest, client):
    """上传 .xlsx → 表格感知切分 → chunks= 传进 ingest_text（防上传分支被改断）。"""
    import io as _io

    import openpyxl

    mock_doc = AsyncMock()
    mock_doc.id = "00000000-0000-0000-0000-000000000005"
    mock_doc.status = "pending"
    mock_ingest.return_value = (mock_doc, AsyncMock(), 1, False)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "技术选型"
    ws.append(["类别", "方案"])
    ws.append(["数据库", "MySQL"])
    ws.append(["缓存", "Redis"])
    buf = _io.BytesIO()
    wb.save(buf)

    files = {"file": ("台账.xlsx", buf.getvalue(),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    response = client.post("/libraries/testlib/import-file", files=files)

    assert response.status_code == status.HTTP_201_CREATED
    mock_ingest.assert_called_once()
    kwargs = mock_ingest.call_args[1]
    chunks = kwargs.get("chunks")
    assert chunks is not None and len(chunks) >= 1        # 关键：chunks 真传进去了
    joined = "\n".join(chunks)
    assert "【章节】技术选型" in joined                     # 工作表名作上下文（caption==heading 不重复）
    assert "类别 | 方案" in joined                          # 首行作表头
    assert "数据库 | MySQL" in joined and "缓存 | Redis" in joined

@patch("app.services.ingest.ingest_text", new_callable=AsyncMock)
def test_import_json_file_list(mock_ingest, client):
    mock_doc = AsyncMock()
    mock_doc.id = "00000000-0000-0000-0000-000000000004"
    mock_doc.status = "pending"
    mock_job = AsyncMock()
    mock_ingest.return_value = (mock_doc, mock_job, 2, False)

    json_data = [
        {"title": "Doc 1", "text": "First doc text", "external_id": "ext1"},
        {"title": "Doc 2", "text": "Second doc text", "external_id": "ext2"}
    ]
    file_content = json.dumps(json_data).encode("utf-8")
    files = {"file": ("docs.json", file_content, "application/json")}
    response = client.post("/libraries/testlib/import-file", files=files)
    
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["status"] == "success"
    assert data["imported_count"] == 2
    assert data["documents"][0]["title"] == "Doc 1"
    assert data["documents"][1]["title"] == "Doc 2"
    assert mock_ingest.call_count == 2

@patch("app.services.ingest.ingest_text", new_callable=AsyncMock)
def test_import_csv_file(mock_ingest, client):
    mock_doc = AsyncMock()
    mock_doc.id = "00000000-0000-0000-0000-000000000005"
    mock_doc.status = "pending"
    mock_job = AsyncMock()
    mock_ingest.return_value = (mock_doc, mock_job, 3, False)

    csv_data = "title,text,external_id\nTitle A,Text A,extA\nTitle B,Text B,extB"
    file_content = csv_data.encode("utf-8")
    files = {"file": ("data.csv", file_content, "text/csv")}
    response = client.post("/libraries/testlib/import-file", files=files)
    
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["status"] == "success"
    assert data["imported_count"] == 2
    assert data["documents"][0]["title"] == "Title A"
    assert data["documents"][1]["title"] == "Title B"
    assert mock_ingest.call_count == 2


# ── 3A：API Key（无 cookie）上传闭环 ──────────────────────────────────────

def test_import_file_with_api_key_no_cookie():
    """API Key (Bearer) 能调用上传接口，不依赖浏览器 cookie；返回含 job_id。"""
    mock_user.is_superuser = True
    app.dependency_overrides[get_db] = override_db
    doc = MagicMock(); doc.id = uuid.uuid4(); doc.status = "pending"
    job = MagicMock(); job.id = uuid.uuid4()
    with patch("app.deps.load_active_library", new_callable=AsyncMock) as ml, \
         patch("app.auth.api_key.APIKeyStrategy.read_token", new_callable=AsyncMock) as rt, \
         patch("app.services.ingest.ingest_text", new_callable=AsyncMock) as mi:
        ml.return_value = mock_library
        rt.return_value = mock_user          # Bearer key → 解析出用户（不连 DB）
        mi.return_value = (doc, job, 1, False)
        client = TestClient(app)
        resp = client.post(
            "/libraries/testlib/import-file",
            files={"file": ("a.txt", b"hello world", "text/plain")},
            headers={"Authorization": "Bearer vk_fake_key"},   # 仅 Bearer，无 cookie
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()
        assert data["imported_count"] == 1
        assert data["documents"][0]["job_id"] == str(job.id)
        rt.assert_awaited()                  # 证明确实走了 API Key 后端
    app.dependency_overrides.clear()


def test_import_file_api_key_without_insert_forbidden():
    """API Key 用户没有 insert 权限 → 上传返回 403。"""
    mock_user.is_superuser = False
    app.dependency_overrides[get_db] = override_db
    try:
        with patch("app.deps.load_active_library", new_callable=AsyncMock) as ml, \
             patch("app.auth.api_key.APIKeyStrategy.read_token", new_callable=AsyncMock) as rt, \
             patch("app.deps.has_permission") as hp:
            ml.return_value = mock_library
            rt.return_value = mock_user
            hp.return_value = False
            client = TestClient(app)
            resp = client.post(
                "/libraries/testlib/import-file",
                files={"file": ("a.txt", b"hi", "text/plain")},
                headers={"Authorization": "Bearer vk_fake_key"},
            )
            assert resp.status_code == status.HTTP_403_FORBIDDEN
    finally:
        mock_user.is_superuser = True
        app.dependency_overrides.clear()


# ── 3A：文件上传 external_id 自动 upsert ──────────────────────────────────

def test_import_file_external_id_upsert():
    """带 external_id 重复上传 → 命中库内已存在文档走 reingest 覆盖，不新建。"""
    existing = Document(
        id=uuid.uuid4(), library_id=mock_library.id,
        external_id="ext-1", status="pending", content_hash="oldhash",
    )
    db = make_db_mock(existing=existing)

    async def _ov_db():
        return db

    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = _ov_db
    job = MagicMock(); job.id = uuid.uuid4()
    try:
        with patch("app.deps.load_active_library", new_callable=AsyncMock) as ml, \
             patch("app.services.ingest.reingest_document", new_callable=AsyncMock) as rr, \
             patch("app.services.ingest.ingest_text", new_callable=AsyncMock) as it:
            ml.return_value = mock_library
            rr.return_value = (job, 3, False)   # changed=False → 不触发 qdrant 清理
            client = TestClient(app)
            resp = client.post(
                "/libraries/testlib/import-file",
                files={"file": ("note.txt", b"new content", "text/plain")},
                data={"external_id": "ext-1"},
            )
            assert resp.status_code == status.HTTP_201_CREATED
            d = resp.json()["documents"][0]
            assert d["document_id"] == str(existing.id)
            assert d["chunk_count"] == 3
            assert d["external_id"] == "ext-1"
            assert d["job_id"] == str(job.id)
            rr.assert_awaited_once()
            assert rr.call_args.kwargs["force"] is False     # upsert 用 no-op 判定
            it.assert_not_called()                           # 命中 upsert，不应新建
    finally:
        app.dependency_overrides.clear()


# ── 3A/3B：库级任务状态查询（普通用户）─────────────────────────────────────

def _job(**kw) -> EmbeddingJob:
    kw.setdefault("id", uuid.uuid4())
    kw.setdefault("library_id", mock_library.id)
    kw.setdefault("document_id", uuid.uuid4())
    kw.setdefault("status", "done")
    kw.setdefault("attempt_count", 0)
    kw.setdefault("document_revision", 1)
    kw.setdefault("created_at", datetime.now(timezone.utc))
    return EmbeddingJob(**kw)


def _client_with_db(db):
    app.dependency_overrides[current_active_user] = override_user

    async def _ov_db():
        return db

    app.dependency_overrides[get_db] = _ov_db
    return TestClient(app)


def test_get_job_status():
    mock_user.is_superuser = True
    job = _job(status="done", attempt_count=1)
    db = make_db_mock(get_return=job)
    try:
        with patch("app.deps.load_active_library", new_callable=AsyncMock) as ml:
            ml.return_value = mock_library
            resp = _client_with_db(db).get(f"/libraries/testlib/jobs/{job.id}")
            assert resp.status_code == status.HTTP_200_OK
            body = resp.json()
            assert body["id"] == str(job.id)
            assert body["status"] == "done"
            assert body["attempt_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_get_job_status_wrong_library_404():
    """job 属于别的库 → 404（库级隔离）。"""
    mock_user.is_superuser = True
    job = _job(library_id=uuid.uuid4())     # 与 mock_library.id 不同
    db = make_db_mock(get_return=job)
    try:
        with patch("app.deps.load_active_library", new_callable=AsyncMock) as ml:
            ml.return_value = mock_library
            resp = _client_with_db(db).get(f"/libraries/testlib/jobs/{job.id}")
            assert resp.status_code == status.HTTP_404_NOT_FOUND
    finally:
        app.dependency_overrides.clear()


def test_list_document_jobs():
    mock_user.is_superuser = True
    doc = Document(id=uuid.uuid4(), library_id=mock_library.id, status="ready", content_hash="h")
    job = _job(document_id=doc.id, status="processing")
    db = make_db_mock(get_return=doc, scalar_list=[job])
    try:
        with patch("app.deps.load_active_library", new_callable=AsyncMock) as ml:
            ml.return_value = mock_library
            resp = _client_with_db(db).get(f"/libraries/testlib/documents/{doc.id}/jobs")
            assert resp.status_code == status.HTTP_200_OK
            rows = resp.json()
            assert len(rows) == 1
            assert rows[0]["status"] == "processing"
            assert rows[0]["document_id"] == str(doc.id)
    finally:
        app.dependency_overrides.clear()


# ── #2：库级统计补 done_jobs / total_jobs ─────────────────────────────────

def test_library_stats_includes_done_and_total():
    mock_user.is_superuser = True
    job_rows = [("pending", 1), ("processing", 2), ("done", 3), ("failed", 4)]
    db = AsyncMock()
    # doc_count / chunk_count 用 scalar_one；job 聚合用 .all()——同一 result mock 都能满足
    db.execute = AsyncMock(return_value=MagicMock(
        scalar_one=MagicMock(return_value=7),
        all=MagicMock(return_value=job_rows),
    ))
    try:
        with patch("app.deps.load_active_library", new_callable=AsyncMock) as ml:
            ml.return_value = mock_library
            resp = _client_with_db(db).get("/libraries/testlib/stats")
            assert resp.status_code == status.HTTP_200_OK
            s = resp.json()
            assert s["document_count"] == 7 and s["chunk_count"] == 7
            assert s["pending_jobs"] == 1 and s["processing_jobs"] == 2
            assert s["done_jobs"] == 3 and s["failed_jobs"] == 4
            assert s["total_jobs"] == 10
    finally:
        app.dependency_overrides.clear()


# ── #3：docx 表格感知开关接入上传 ─────────────────────────────────────────

def test_import_docx_table_aware_passes_segment_chunks():
    """库开 docx_table_aware → docx 走 segments + chunk_segments，chunks 传进 ingest_text。"""
    mock_user.is_superuser = True
    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    mock_library.docx_table_aware = True
    doc = MagicMock(); doc.id = uuid.uuid4(); doc.status = "pending"
    segs = [{"kind": "table", "heading": "技术选型", "caption": "技术选型",
             "header": "类别 | 方案", "rows": ["类别 | 方案", "数据库 | MySQL"]}]
    try:
        with patch("app.deps.load_active_library", new_callable=AsyncMock) as ml, \
             patch("app.services.docx_extract.extract_docx_segments", return_value=segs) as seg, \
             patch("app.services.splitter.chunk_segments",
                   return_value=["【章节】技术选型\n类别 | 方案\n数据库 | MySQL"]) as ck, \
             patch("app.services.ingest.ingest_text", new_callable=AsyncMock) as mi:
            ml.return_value = mock_library
            mi.return_value = (doc, MagicMock(id=uuid.uuid4()), 1, False)
            client = TestClient(app)
            resp = client.post(
                "/libraries/testlib/import-file",
                files={"file": ("台账.docx", b"PK-fake-docx-bytes",
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            )
            assert resp.status_code == status.HTTP_201_CREATED
            seg.assert_called_once()
            ck.assert_called_once()
            chunks = mi.call_args.kwargs.get("chunks")
            assert chunks is not None and "数据库 | MySQL" in "\n".join(chunks)
    finally:
        mock_library.docx_table_aware = None
        app.dependency_overrides.clear()


# ── #4：.xls 明确拒绝 ─────────────────────────────────────────────────────

def test_import_xls_rejected(client):
    resp = client.post(
        "/libraries/testlib/import-file",
        files={"file": ("旧台账.xls", b"\xd0\xcf\x11\xe0fake-xls", "application/vnd.ms-excel")},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "xlsx" in resp.json()["detail"]
