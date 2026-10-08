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


def test_context_separator_is_part_of_the_exact_budget():
    records = [_rec("c1", "abc", title="a"), _rec("c2", "def", title="b")]
    context, used = C.build_context(records, 18)
    assert len(context) <= 18
    assert context == "[1] a\nabc\n\n[2] b\nd"
    assert [record.content for record in used] == ["abc", "d"]


@pytest.mark.parametrize("record_kind", ["dict", "dify", "graph"])
def test_used_records_are_copies_of_exact_model_text(record_kind):
    original = _rec("c1", "  first\n second tail  ", title="  title  ")
    if record_kind == "dict":
        original = original.model_dump()
    if record_kind == "graph":
        original.metadata["chat_graph_evidence"] = {"content": "graph evidence", "chunk_id": "c1"}
    content_before = C._rec_field(original, "content")
    metadata_before = C._rec_field(original, "metadata").copy()
    context, used = C.build_context([original], 18)
    assert context == "[1] title\nfirst\n s"
    assert len(context) <= 18
    assert used[0] is not original
    assert C._rec_field(used[0], "content") == context.split("\n", 1)[1]
    assert C._rec_field(used[0], "metadata") == metadata_before
    assert C._rec_field(original, "content") == content_before
    assert C._rec_field(original, "metadata") == metadata_before


@pytest.mark.parametrize("budget", [-10, 0, 1, 4, 5, 6])
def test_no_header_only_sources_in_tiny_budgets(budget):
    context, used = C.build_context([_rec("c1", "正文", title="t")], budget)
    assert context == ""
    assert used == []


def test_empty_text_records_do_not_take_source_numbers():
    records = [_rec("blank", " \n ", title="empty"), _rec("full", " \n available \n ", title="full")]
    context, used = C.build_context(records, 100)
    assert context == "[1] full\navailable"
    assert len(used) == 1
    assert used[0].metadata["chunk_id"] == "full"
    assert used[0].content == "available"


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


def test_success_removes_model_thinking_block():
    with _patch(content="<think>这里是模型推理，不应返回。</think>根据 [1] 可知答案。"):
        res = _gen([_rec("c1", "正文")])
    assert res.answer == "根据 [1] 可知答案。"


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


def test_stream_removes_thinking_block_when_tags_cross_deltas():
    lines = [_delta("<thi"), _delta("nk>推理"), _delta("</th"), _delta("ink>答案"), "data: [DONE]"]
    assert _collect_stream(lines=lines) == ["答案"]


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


@pytest.mark.parametrize("answer", ["结论[0]", "结论[2]", "结论[999]"])
def test_checked_answer_rejects_citations_outside_actual_context(answer):
    with _patch(content=answer):
        with pytest.raises(C.ChatError, match="chat_invalid_citation"):
            _gen([_rec("c1", "正文"), _rec("c2", "未入模")], max_context_chars=7, validate_citations=True)


@pytest.mark.parametrize("answer", ["结论[1]", "`[99]` 和结论[1]", "```text\n[99]\n```\n结论[1]",
                                   "[99](https://example.test) 和结论[1]", "https://example.test/[99] 结论[1]"])
def test_checked_answer_preserves_valid_citations_code_and_links(answer):
    with _patch(content=answer):
        assert _gen([_rec("c1", "正文")], validate_citations=True).answer == answer


def test_stream_checked_citation_never_emits_an_invalid_badge_split_across_deltas():
    emitted = []
    async def scenario():
        async for delta in C.stream_answer("q", [_rec("c1", "正文")], base_url="http://llm/v1", model="m",
                                           validate_citations=True):
            emitted.append(delta)
    with _patch_stream(lines=[_delta("已知事实["), _delta("99"), _delta("]"), "data: [DONE]"]):
        with pytest.raises(C.ChatError, match="chat_invalid_citation"):
            asyncio.run(scenario())
    assert "[99]" not in "".join(emitted)


def test_stream_checked_markdown_is_unchanged_when_every_character_is_a_delta():
    answer = "`[99]` [99](https://example.test) https://example.test/[99] 结论[1]"
    lines = ["data: " + json.dumps({"choices": [{"delta": {"content": char}}]}) for char in answer]
    async def scenario():
        return "".join([delta async for delta in C.stream_answer("q", [_rec("c1", "正文")],
            base_url="http://llm/v1", model="m", validate_citations=True)])
    with _patch_stream(lines=[*lines, "data: [DONE]"]):
        assert asyncio.run(scenario()) == answer


@pytest.mark.parametrize("answer", [
    "未闭合`内容[99]",
    "未闭合``内容`[99]",
    "正文 ```未闭合[99]",
    "未闭合[[99]",
    "`[99]`后面有[[2]",
    "``[99]```",
])
@pytest.mark.parametrize("streaming", [False, True])
def test_unclosed_markdown_cannot_hide_invalid_citations(answer, streaming):
    emitted = []
    async def scenario():
        async for delta in C.stream_answer("q", [_rec("c1", "正文")], base_url="http://llm/v1",
                                           model="m", validate_citations=True):
            emitted.append(delta)
    if streaming:
        lines = ["data: " + json.dumps({"choices": [{"delta": {"content": char}}]}) for char in answer]
        with _patch_stream(lines=[*lines, "data: [DONE]"]):
            with pytest.raises(C.ChatError, match="chat_invalid_citation"):
                asyncio.run(scenario())
        assert "[2]" not in "".join(emitted)
        assert "[99]" not in "".join(emitted).split("`[99]`")[-1]
    else:
        with _patch(content=answer):
            with pytest.raises(C.ChatError, match="chat_invalid_citation"):
                _gen([_rec("c1", "正文")], validate_citations=True)


@pytest.mark.parametrize("answer", [
    "~~~text\n[99]\n~~~\n结论[1]",
    "~~~text\n[99]",
    "``[99]`仍在代码``结论[1]",
    "`跨\n行[99]`结论[1]",
    "```text\n```不是结束 [99]\n```\n结论[1]",
    "[代码 `[99]`] 结论[1]",
    "[外层 [99](https://example.test)] 结论[1]",
])
@pytest.mark.parametrize("streaming", [False, True])
def test_markdown_code_delimiters_preserved_in_both_modes(answer, streaming):
    if streaming:
        lines = ["data: " + json.dumps({"choices": [{"delta": {"content": char}}]}) for char in answer]
        async def scenario():
            return "".join([delta async for delta in C.stream_answer("q", [_rec("c1", "正文")],
                base_url="http://llm/v1", model="m", validate_citations=True)])
        with _patch_stream(lines=[*lines, "data: [DONE]"]):
            assert asyncio.run(scenario()) == answer
    else:
        with _patch(content=answer):
            assert _gen([_rec("c1", "正文")], validate_citations=True).answer == answer
