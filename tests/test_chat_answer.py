"""chat_answer 单测：上下文拼接/截断、prompt 内容、key 不入 body、失败兜底。"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from app.schemas.dify import DifyRecord
from app.services import chat_answer as C


class _Resp:
    def __init__(self, data):
        self._d = data
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def _patch(*, content="根据资料 [1] 可知……", exc=None, body=None, cap=None):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            if cap is not None:
                cap["json"], cap["headers"], cap["url"] = json, headers, url
            if exc is not None:
                raise exc
            return _Resp(body if body is not None else {"choices": [{"message": {"content": content}}]})

    return patch.object(C.httpx, "AsyncClient", _Client)


def _rec(cid, content, *, title="t", score=0.9):
    return DifyRecord(content=content, score=score, title=title,
                      metadata={"document_id": "doc-" + cid, "chunk_id": cid})


def _gen(records, **kw):
    kw.setdefault("base_url", "http://llm/v1")
    kw.setdefault("model", "m")
    return asyncio.run(C.generate_answer("农民工工资条例说了什么", records, **kw))


# ── build_context ───────────────────────────────────────────────────────────
def test_build_context_numbers_all_records():
    recs = [_rec("c1", "甲内容", title="文件甲"), _rec("c2", "乙内容", title="文件乙")]
    ctx, used = C.build_context(recs, max_context_chars=10000)
    assert "[1] 文件甲" in ctx and "甲内容" in ctx
    assert "[2] 文件乙" in ctx and "乙内容" in ctx
    assert len(used) == 2


def test_build_context_truncates_over_budget():
    recs = [_rec("c1", "甲" * 500, title="t1"),
            _rec("c2", "乙" * 500, title="t2"),
            _rec("c3", "丙" * 500, title="t3")]
    ctx, used = C.build_context(recs, max_context_chars=600)
    assert "甲" in ctx              # 第 1 条进上下文
    assert "丙" not in ctx          # 第 3 条被预算挡在外面
    assert len(used) <= 2
    assert len(ctx) <= 600 + 4      # 不超过预算（+少量 join 余量）


# ── prompt 内容 ──────────────────────────────────────────────────────────────
def test_prompt_contains_numbers_and_chunk_content():
    cap = {}
    with _patch(cap=cap):
        _gen([_rec("c1", "保障农民工工资支付条例的关键条款", title="条例")])
    user_msg = cap["json"]["messages"][1]["content"]
    assert "[1]" in user_msg                                  # 来源编号
    assert "保障农民工工资支付条例的关键条款" in user_msg        # chunk 正文


def test_system_prompt_constrains_to_sources():
    cap = {}
    with _patch(cap=cap):
        _gen([_rec("c1", "x")])
    sys_msg = cap["json"]["messages"][0]["content"]
    assert "资料中未找到明确依据" in sys_msg
    assert "只能" in sys_msg or "依据" in sys_msg


# ── API Key 安全 ─────────────────────────────────────────────────────────────
def test_api_key_goes_to_header_not_body():
    cap = {}
    with _patch(cap=cap):
        _gen([_rec("c1", "x")], api_key="super-secret-key")
    assert cap["headers"]["Authorization"] == "Bearer super-secret-key"
    assert "super-secret-key" not in json.dumps(cap["json"])   # 绝不在请求体里


def test_no_auth_header_when_key_empty():
    cap = {}
    with _patch(cap=cap):
        _gen([_rec("c1", "x")], api_key="")
    assert cap["headers"] is None


# ── 成功 / 失败 ──────────────────────────────────────────────────────────────
def test_success_returns_answer_and_used_records():
    with _patch(content="  根据 [1] 可知答案。  "):
        res = _gen([_rec("c1", "正文")])
    assert res.answer == "根据 [1] 可知答案。"     # strip
    assert len(res.used_records) == 1


def test_timeout_raises_chat_error():
    with _patch(exc=httpx.TimeoutException("timeout")):
        with pytest.raises(C.ChatError):
            _gen([_rec("c1", "x")])


def test_http_error_raises_chat_error():
    with _patch(exc=httpx.ConnectError("refused")):
        with pytest.raises(C.ChatError):
            _gen([_rec("c1", "x")])


def test_malformed_response_raises_chat_error():
    with _patch(body={"unexpected": 1}):
        with pytest.raises(C.ChatError):
            _gen([_rec("c1", "x")])


def test_empty_answer_raises_chat_error():
    with _patch(content="   "):
        with pytest.raises(C.ChatError):
            _gen([_rec("c1", "x")])


# ── stream_answer ────────────────────────────────────────────────────────────
def _patch_stream(lines=None, *, status=200, enter_exc=None, iter_exc=None):
    class _Resp:
        def __init__(self):
            self.status_code = status

        async def aiter_lines(self):
            if iter_exc is not None:
                raise iter_exc
            for ln in (lines or []):
                yield ln

        async def aread(self):
            return b""

    class _CM:
        async def __aenter__(self):
            if enter_exc is not None:
                raise enter_exc
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None, headers=None):
            return _CM()

    return patch.object(C.httpx, "AsyncClient", _Client)


def _collect_stream(**patchkw):
    async def _run():
        out = []
        async for d in C.stream_answer("q", [_rec("c1", "正文")], base_url="http://llm/v1", model="m"):
            out.append(d)
        return out
    with _patch_stream(**patchkw):
        return asyncio.run(_run())


def _delta(s):
    return 'data: {"choices":[{"delta":{"content":"' + s + '"}}]}'


def test_stream_yields_content_deltas():
    lines = [_delta("农"), _delta("民工"), "data: [DONE]"]
    assert _collect_stream(lines=lines) == ["农", "民工"]


def test_stream_skips_heartbeat_and_non_content():
    lines = [": ping", 'data: {"choices":[{"delta":{}}]}', _delta("好"), "data: [DONE]"]
    assert _collect_stream(lines=lines) == ["好"]


def test_stream_http_error_raises_chat_error():
    with pytest.raises(C.ChatError):
        _collect_stream(lines=[], status=500)


def test_stream_timeout_raises_chat_error():
    with pytest.raises(C.ChatError):
        _collect_stream(iter_exc=httpx.ReadTimeout("slow"))


def test_stream_rejects_output_over_hard_limit():
    with pytest.raises(C.ChatError, match=C.CHAT_OUTPUT_LIMIT_EXCEEDED):
        _collect_stream(
            lines=[_delta("x" * (C.CHAT_OUTPUT_MAX_CHARS + 1))],
        )
