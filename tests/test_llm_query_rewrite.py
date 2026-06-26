"""LLM Query Rewrite 单测：解析 / 容错 / 失败兜底 —— 任何异常都必须返回 []，绝不抛。"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx

from app.services import llm_query_rewrite as L


class _FakeResp:
    def __init__(self, data):
        self._d = data
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def _patch_client(*, content=None, exc=None, body=None):
    """patch httpx.AsyncClient：content→正常回包；exc→post 抛异常；body→自定义 json 体。"""
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            if exc is not None:
                raise exc
            data = body if body is not None else {
                "choices": [{"message": {"content": content}}]
            }
            return _FakeResp(data)

    return patch.object(L.httpx, "AsyncClient", _FakeClient)


def _gen(query="原问题", *, max_queries=4):
    return asyncio.run(L.generate(
        query, base_url="http://llm/v1", model="m", api_key="", timeout=8, max_queries=max_queries,
    ))


# ── 解析成功 ────────────────────────────────────────────────────────────────
def test_parses_json_array():
    with _patch_client(content='["原问题", "扩展1", "扩展2"]'):
        assert _gen() == ["原问题", "扩展1", "扩展2"]


def test_strips_code_fence():
    with _patch_client(content='```json\n["a", "b"]\n```'):
        assert _gen() == ["a", "b"]


def test_extracts_array_from_surrounding_text():
    with _patch_client(content='好的，这是查询：["原问题", "同义"] 希望有用'):
        assert _gen() == ["原问题", "同义"]


def test_caps_to_max_queries():
    with _patch_client(content='["a","b","c","d","e","f"]'):
        assert _gen(max_queries=3) == ["a", "b", "c"]


def test_drops_non_string_and_blank_items():
    with _patch_client(content='["a", 123, "", "  ", "b"]'):
        assert _gen() == ["a", "b"]


# ── 失败兜底（一律返回 []，绝不抛）─────────────────────────────────────────────
def test_timeout_returns_empty():
    with _patch_client(exc=httpx.TimeoutException("timeout")):
        assert _gen() == []


def test_http_error_returns_empty():
    with _patch_client(exc=httpx.ConnectError("refused")):
        assert _gen() == []


def test_invalid_json_returns_empty():
    with _patch_client(content="this is not json at all"):
        assert _gen() == []


def test_non_array_json_returns_empty():
    with _patch_client(content='{"queries": ["a", "b"]}'):
        assert _gen() == []


def test_malformed_response_body_returns_empty():
    # 缺 choices 结构 → 取 content 时抛 KeyError → 兜底 []
    with _patch_client(body={"unexpected": 1}):
        assert _gen() == []


def test_no_base_url_or_model_returns_empty():
    # 配置缺失：直接返回 []，不发请求
    assert asyncio.run(L.generate("q", base_url="", model="", api_key="", timeout=8, max_queries=4)) == []


# ── is_configured / _endpoint ───────────────────────────────────────────────
def test_is_configured():
    with patch.object(L.settings, "query_rewrite_llm_base_url", "http://x/v1"), \
         patch.object(L.settings, "query_rewrite_llm_model", "m"):
        assert L.is_configured() is True
    with patch.object(L.settings, "query_rewrite_llm_base_url", ""), \
         patch.object(L.settings, "query_rewrite_llm_model", "m"):
        assert L.is_configured() is False


def test_endpoint_normalization():
    assert L._endpoint("http://x/v1") == "http://x/v1/chat/completions"
    assert L._endpoint("http://x/v1/") == "http://x/v1/chat/completions"
    assert L._endpoint("http://x/v1/chat/completions") == "http://x/v1/chat/completions"
