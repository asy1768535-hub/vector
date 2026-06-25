"""check_local_ai_services 单元测试：全程 mock（不连真实服务/模型/网络）。

覆盖：云地址守卫、embedding 维度/向量数校验、rerank 结构与排序合法性、
OCR 本地初始化、main 退出码。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings

# 以模块方式加载 scripts/ 下的检查脚本（非包，需手动注册到 sys.modules）
_SPEC = importlib.util.spec_from_file_location(
    "check_local_ai_services",
    Path(__file__).resolve().parent.parent / "scripts" / "check_local_ai_services.py",
)
cl = importlib.util.module_from_spec(_SPEC)
sys.modules["check_local_ai_services"] = cl
_SPEC.loader.exec_module(cl)

LOCAL_EMB = "http://localhost:8111/v1/embeddings"
LOCAL_RR = "http://localhost:9000/rerank"
CLOUD_EMB = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"


def test_is_cloud_host():
    assert cl.is_cloud_host(CLOUD_EMB) is True
    assert cl.is_cloud_host("https://api.openai.com/v1/embeddings") is True
    assert cl.is_cloud_host(LOCAL_EMB) is False
    assert cl.is_cloud_host("http://10.0.10.2:8111/v1/embeddings") is False


# ---------- embedding ----------

def test_embedding_ok():
    with patch("app.services.embedding.embed_texts", new_callable=AsyncMock) as m:
        m.return_value = [[0.0] * 1024, [0.0] * 1024]
        r = asyncio.run(cl.check_embedding(base_url=LOCAL_EMB, model="bge-m3", dim=1024, api_key="", allow_cloud=False))
    assert r.ok and "返回向量数=2" in r.detail and "维度=1024" in r.detail


def test_embedding_dim_mismatch_fails():
    with patch("app.services.embedding.embed_texts", new_callable=AsyncMock) as m:
        m.return_value = [[0.0] * 512, [0.0] * 512]
        r = asyncio.run(cl.check_embedding(base_url=LOCAL_EMB, model="bge-m3", dim=1024, api_key="", allow_cloud=False))
    assert not r.ok


def test_embedding_connection_error_fails():
    with patch("app.services.embedding.embed_texts", new_callable=AsyncMock) as m:
        m.side_effect = RuntimeError("connection refused")
        r = asyncio.run(cl.check_embedding(base_url=LOCAL_EMB, model="bge-m3", dim=1024, api_key="", allow_cloud=False))
    assert not r.ok and "connection refused" in r.detail


def test_embedding_cloud_refused_no_request():
    with patch("app.services.embedding.embed_texts", new_callable=AsyncMock) as m:
        r = asyncio.run(cl.check_embedding(base_url=CLOUD_EMB, model="x", dim=1024, api_key="", allow_cloud=False))
    assert not r.ok and "云地址" in r.detail
    m.assert_not_awaited()


def test_embedding_cloud_allowed_makes_request():
    with patch("app.services.embedding.embed_texts", new_callable=AsyncMock) as m:
        m.return_value = [[0.0] * 1024, [0.0] * 1024]
        r = asyncio.run(cl.check_embedding(base_url=CLOUD_EMB, model="x", dim=1024, api_key="k", allow_cloud=True))
    assert r.ok
    m.assert_awaited()


# ---------- rerank ----------

def test_rerank_ok():
    with patch("app.services.rerank.rerank", new_callable=AsyncMock) as m:
        m.return_value = [(1, 0.9), (0, 0.5), (2, 0.1)]
        r = asyncio.run(cl.check_rerank(base_url=LOCAL_RR, model="bge-reranker-v2-m3", api_key="", allow_cloud=False))
    assert r.ok


def test_rerank_not_sorted_fails():
    with patch("app.services.rerank.rerank", new_callable=AsyncMock) as m:
        m.return_value = [(0, 0.1), (1, 0.9)]
        r = asyncio.run(cl.check_rerank(base_url=LOCAL_RR, model="m", api_key="", allow_cloud=False))
    assert not r.ok


def test_rerank_duplicate_index_fails():
    with patch("app.services.rerank.rerank", new_callable=AsyncMock) as m:
        m.return_value = [(1, 0.9), (1, 0.5)]
        r = asyncio.run(cl.check_rerank(base_url=LOCAL_RR, model="m", api_key="", allow_cloud=False))
    assert not r.ok


def test_rerank_index_out_of_range_fails():
    with patch("app.services.rerank.rerank", new_callable=AsyncMock) as m:
        m.return_value = [(9, 0.9)]
        r = asyncio.run(cl.check_rerank(base_url=LOCAL_RR, model="m", api_key="", allow_cloud=False))
    assert not r.ok


def test_rerank_not_configured_no_request():
    with patch("app.services.rerank.rerank", new_callable=AsyncMock) as m:
        r = asyncio.run(cl.check_rerank(base_url="", model="", api_key="", allow_cloud=False))
    assert not r.ok and "未配置" in r.detail
    m.assert_not_awaited()


def test_rerank_cloud_refused_no_request():
    cloud_rr = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    with patch("app.services.rerank.rerank", new_callable=AsyncMock) as m:
        r = asyncio.run(cl.check_rerank(base_url=cloud_rr, model="m", api_key="", allow_cloud=False))
    assert not r.ok and "云地址" in r.detail
    m.assert_not_awaited()


# ---------- ocr ----------

def test_ocr_ok():
    with patch("app.services.ocr.is_available", return_value=True), \
         patch("app.services.ocr._get_engine", return_value=MagicMock()) as ge:
        r = cl.check_ocr()
    assert r.ok and ge.called


def test_ocr_missing_fails_no_init():
    with patch("app.services.ocr.is_available", return_value=False), \
         patch("app.services.ocr._get_engine") as ge:
        r = cl.check_ocr()
    assert not r.ok and "未安装" in r.detail
    ge.assert_not_called()


def test_ocr_init_error_fails():
    with patch("app.services.ocr.is_available", return_value=True), \
         patch("app.services.ocr._get_engine", side_effect=RuntimeError("onnx load failed")):
        r = cl.check_ocr()
    assert not r.ok and "onnx load failed" in r.detail


# ---------- main 退出码 ----------

def _all_pass_patches():
    return (
        patch("app.services.embedding.embed_texts", new_callable=AsyncMock, return_value=[[0.0] * 1024, [0.0] * 1024]),
        patch("app.services.rerank.rerank", new_callable=AsyncMock, return_value=[(1, 0.9), (0, 0.5), (2, 0.1)]),
        patch("app.services.ocr.is_available", return_value=True),
        patch("app.services.ocr._get_engine", return_value=MagicMock()),
    )


ARGV = ["--embedding-url", LOCAL_EMB, "--rerank-url", LOCAL_RR, "--rerank-model", "bge-reranker-v2-m3"]


def test_main_returns_zero_when_all_pass():
    p1, p2, p3, p4 = _all_pass_patches()
    with p1, p2, p3, p4:
        assert cl.main(ARGV) == 0


def test_main_returns_nonzero_when_a_check_fails():
    with patch("app.services.embedding.embed_texts", new_callable=AsyncMock, side_effect=RuntimeError("down")), \
         patch("app.services.rerank.rerank", new_callable=AsyncMock, return_value=[(0, 0.9)]), \
         patch("app.services.ocr.is_available", return_value=True), \
         patch("app.services.ocr._get_engine", return_value=MagicMock()):
        assert cl.main(ARGV) == 1


# ---------- 回归：本地地址不得携带 settings Key；不受 .env dashscope 影响；输出不含 Key ----------

def test_local_url_never_sends_settings_api_key(monkeypatch):
    """settings 里有云 Key，但检查本地 URL 时，embedding/rerank 收到的 api_key 不得是该云 Key
    （用空串抑制鉴权头；None 会回退 settings 反而泄漏）。"""
    monkeypatch.setattr(settings, "embedding_api_key", "CLOUDKEY_EMB")
    monkeypatch.setattr(settings, "rerank_api_key", "CLOUDKEY_RR")
    p1, p2, p3, p4 = _all_pass_patches()
    with p1 as me, p2 as mr, p3, p4:
        rc = cl.main(ARGV)
    assert rc == 0
    assert not me.await_args.kwargs["api_key"]            # 空（不是云 Key）
    assert me.await_args.kwargs["api_key"] != "CLOUDKEY_EMB"
    assert not mr.await_args.kwargs["api_key"]
    assert mr.await_args.kwargs["api_key"] != "CLOUDKEY_RR"
    assert mr.await_args.kwargs["provider"] == "standard"  # 强制 standard


def test_local_rerank_uses_standard_payload_despite_dashscope_provider(monkeypatch):
    """settings.rerank_provider=dashscope，但检查本地 URL 仍用 standard 请求格式，且不发鉴权头。"""
    monkeypatch.setattr(settings, "rerank_provider", "dashscope")
    monkeypatch.setattr(settings, "rerank_api_key", "CLOUDKEY_RR")
    captured = {}

    class _FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"results": [{"index": 1, "relevance_score": 0.9},
                                {"index": 0, "relevance_score": 0.5},
                                {"index": 2, "relevance_score": 0.1}]}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["payload"] = json
            captured["headers"] = headers
            return _FakeResp()

    with patch("app.services.rerank.httpx.AsyncClient", _FakeClient):
        r = asyncio.run(cl.check_rerank(base_url=LOCAL_RR, model="bge-reranker-v2-m3",
                                        api_key="CLOUDKEY_RR", allow_cloud=False))
    assert r.ok
    p = captured["payload"]
    assert "query" in p and "documents" in p and "top_n" in p   # standard 结构
    assert "input" not in p and "parameters" not in p           # 不是 dashscope 结构
    assert captured["headers"] is None                          # 本地：无 Authorization 头


def test_cloud_rerank_uses_dashscope_payload_when_allowed(monkeypatch):
    """DashScope URL + --allow-cloud + RERANK_PROVIDER=dashscope → 发 input/parameters 格式（备用检查可用）。"""
    monkeypatch.setattr(settings, "rerank_provider", "dashscope")
    cloud_rr = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    captured = {}

    class _FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"output": {"results": [{"index": 0, "relevance_score": 0.9},
                                           {"index": 1, "relevance_score": 0.5},
                                           {"index": 2, "relevance_score": 0.1}]}}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["payload"] = json
            captured["headers"] = headers
            return _FakeResp()

    with patch("app.services.rerank.httpx.AsyncClient", _FakeClient):
        r = asyncio.run(cl.check_rerank(base_url=cloud_rr, model="gte-rerank",
                                        api_key="CLOUDKEY_RR", allow_cloud=True))
    assert r.ok
    p = captured["payload"]
    assert "input" in p and "parameters" in p           # dashscope 结构
    assert "query" not in p and "top_n" not in p         # 非 standard
    # 云地址带配置 Key（鉴权头存在）
    assert captured["headers"] and "Bearer" in captured["headers"].get("Authorization", "")


def test_embedding_exception_containing_key_is_redacted():
    """异常正文含当前 Key 时，detail 里替换为 ***，不出现原 Key。"""
    with patch("app.services.embedding.embed_texts", new_callable=AsyncMock,
               side_effect=RuntimeError("auth failed: Bearer LEAKKEY123 rejected")):
        r = asyncio.run(cl.check_embedding(base_url=LOCAL_EMB, model="bge-m3", dim=1024,
                                           api_key="LEAKKEY123", allow_cloud=False))
    assert not r.ok
    assert "LEAKKEY123" not in r.detail and "***" in r.detail


def test_main_stdout_redacts_key_in_exception(monkeypatch, capsys):
    """异常正文含 settings 当前 Key 时，最终 stdout 不出现原 Key。"""
    monkeypatch.setattr(settings, "embedding_api_key", "LEAKKEY_EMB")
    with patch("app.services.embedding.embed_texts", new_callable=AsyncMock,
               side_effect=RuntimeError("upstream rejected key=LEAKKEY_EMB")), \
         patch("app.services.rerank.rerank", new_callable=AsyncMock,
               return_value=[(1, 0.9), (0, 0.5), (2, 0.1)]), \
         patch("app.services.ocr.is_available", return_value=True), \
         patch("app.services.ocr._get_engine", return_value=MagicMock()):
        rc = cl.main(ARGV)
    out = capsys.readouterr().out
    assert rc == 1
    assert "LEAKKEY_EMB" not in out and "***" in out


def test_output_and_errors_never_contain_api_key(monkeypatch, capsys):
    """无论成功或失败，stdout 都不得出现 settings 里的 Key 值。"""
    monkeypatch.setattr(settings, "embedding_api_key", "SENTINEL_EMB_KEY")
    monkeypatch.setattr(settings, "rerank_api_key", "SENTINEL_RR_KEY")
    with patch("app.services.embedding.embed_texts", new_callable=AsyncMock,
               return_value=[[0.0] * 1024, [0.0] * 1024]), \
         patch("app.services.rerank.rerank", new_callable=AsyncMock,
               side_effect=RuntimeError("rerank service 500: boom")), \
         patch("app.services.ocr.is_available", return_value=True), \
         patch("app.services.ocr._get_engine", return_value=MagicMock()):
        rc = cl.main(ARGV)
    out = capsys.readouterr().out
    assert rc == 1                                  # rerank 失败 → 非零
    assert "SENTINEL_EMB_KEY" not in out
    assert "SENTINEL_RR_KEY" not in out
