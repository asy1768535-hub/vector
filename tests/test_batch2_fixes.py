"""批次2 修复的测试：
#9 chunk_overlap<chunk_size 交叉校验、#10 批量导入错误契约、
#13 后缀白名单/415、#16 公开注册默认关闭。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.auth.backend import current_active_user
from app.db import get_db
from app.schemas.admin import LibraryCreate, LibraryUpdate
from app.services import splitter
from tests.test_query_import_api import (
    mock_library, override_user, make_db_mock,
)


# ── #9 overlap 交叉校验 ───────────────────────────────────────────────
def test_library_create_rejects_overlap_ge_size():
    with pytest.raises(ValidationError):
        LibraryCreate(slug="abc", name="L", chunk_size=200, chunk_overlap=300)


def test_library_create_accepts_valid_overlap():
    lib = LibraryCreate(slug="abc", name="L", chunk_size=1000, chunk_overlap=120)
    assert lib.chunk_overlap == 120


def test_library_update_rejects_overlap_ge_size():
    with pytest.raises(ValidationError):
        LibraryUpdate(chunk_size=500, chunk_overlap=500)  # 相等也非法


def test_library_create_partial_params_ok():
    # 只给其一不交叉校验（由 splitter 入口兜底）
    LibraryCreate(slug="abc", name="L", chunk_overlap=2000)


def test_splitter_guards_overlap_ge_size():
    with pytest.raises(ValueError, match="chunk_overlap"):
        splitter.split_text("一些文本内容", chunk_size=200, chunk_overlap=300)


# ── #13 后缀白名单 / 415 ──────────────────────────────────────────────
@pytest.fixture
def client():
    mock_library.is_superuser = True
    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = lambda: make_db_mock()
    with patch("app.deps.load_active_library", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = mock_library
        yield TestClient(app)
    app.dependency_overrides.clear()


def test_import_unknown_suffix_returns_415(client):
    files = {"file": ("malware.exe", b"MZ\x90\x00binary", "application/octet-stream")}
    resp = client.post("/libraries/testlib/import-file", files=files)
    assert resp.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE


def test_import_uppercase_suffix_routed_not_as_text(client):
    """.TXT（大写）应按 txt 处理而非落入未知分支被拒。"""
    with patch("app.services.ingest.ingest_text", new_callable=AsyncMock) as mi:
        doc = MagicMock(); doc.id = "00000000-0000-0000-0000-0000000000d1"; doc.status = "pending"
        mi.return_value = (doc, MagicMock(id="00000000-0000-0000-0000-0000000000d2"), 1, False)
        files = {"file": ("NOTES.TXT", "大写后缀正文".encode("utf-8"), "text/plain")}
        resp = client.post("/libraries/testlib/import-file", files=files)
    assert resp.status_code == status.HTTP_201_CREATED


# ── #10 批量导入错误契约 ─────────────────────────────────────────────
def _json_list_file(n: int):
    data = [{"text": f"doc-{i}", "title": f"t{i}"} for i in range(n)]
    return {"file": ("batch.json", json.dumps(data).encode("utf-8"), "application/json")}


def test_import_all_fail_returns_400(client):
    with patch("app.services.ingest.ingest_text", new_callable=AsyncMock) as mi:
        mi.side_effect = ValueError("boom")
        resp = client.post("/libraries/testlib/import-file", files=_json_list_file(2))
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_import_partial_returns_partial(client):
    doc = MagicMock(); doc.id = "00000000-0000-0000-0000-0000000000e1"; doc.status = "pending"
    job = MagicMock(); job.id = "00000000-0000-0000-0000-0000000000e2"
    with patch("app.services.ingest.ingest_text", new_callable=AsyncMock) as mi:
        # 第一条成功，第二条失败
        mi.side_effect = [(doc, job, 1, False), ValueError("bad second")]
        resp = client.post("/libraries/testlib/import-file", files=_json_list_file(2))
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    assert data["status"] == "partial"
    assert data["imported_count"] == 1
    assert data["failed_count"] == 1
    assert data["errors"][0]["index"] == 1
    assert "bad second" in data["errors"][0]["error"]


# ── #12 上传大小上限（分块读取 + 413） ─────────────────────────────────
def test_import_oversize_returns_413(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "max_import_file_bytes", 16)  # 16 字节上限
    big = b"x" * 1024  # 远超上限
    files = {"file": ("big.txt", big, "text/plain")}
    resp = client.post("/libraries/testlib/import-file", files=files)
    assert resp.status_code == status.HTTP_413_CONTENT_TOO_LARGE


def test_read_capped_aborts_without_reading_whole_file():
    """直接测 _read_capped：超限即抛，且读取的字节不超过 limit+一个分块。"""
    import asyncio
    from fastapi import HTTPException
    from app.api import documents as docs_mod

    class FakeUpload:
        def __init__(self, total, chunk):
            self.remaining = total
            self.chunk = chunk
            self.read_bytes = 0

        async def read(self, n=-1):
            if self.remaining <= 0:
                return b""
            take = min(n if n and n > 0 else self.remaining, self.remaining, self.chunk)
            self.remaining -= take
            self.read_bytes += take
            return b"x" * take

    up = FakeUpload(total=100 * 1024 * 1024, chunk=docs_mod._UPLOAD_READ_CHUNK)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(docs_mod._read_capped(up, limit=10))
    assert ei.value.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    # 关键：没把 100MiB 全读进来——最多读了 limit + 一个分块
    assert up.read_bytes <= 10 + docs_mod._UPLOAD_READ_CHUNK


# ── #16 公开注册默认关闭 ─────────────────────────────────────────────
def test_public_registration_disabled_by_default():
    """默认不挂 /auth/register → 404（而非 200/422）。"""
    from app.config import settings
    assert settings.allow_public_registration is False
    client = TestClient(app)
    resp = client.post("/auth/register", json={"email": "x@y.com", "password": "Password123"})
    assert resp.status_code == status.HTTP_404_NOT_FOUND
