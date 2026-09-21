"""Chat 答案生成（用户端 v1）。

把检索到的 records 拼成带编号的【资料】，调 OpenAI 兼容 chat/completions 生成答案。
边界（重要）：
  - 只做问答生成，**不做检索**，不接 Agent、不接工具调用。
  - 强约束「只能根据给定资料回答；资料不足则回答"资料中未找到明确依据"；用 [编号] 标注依据」。
  - 上下文超过 max_context_chars 截断。
  - 任何 LLM 失败/超时/响应异常抛 ChatError，由调用方转成明确错误，不影响系统其它功能。
  - 绝不把 API Key 写进日志。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是知识库问答助手。只能依据下面提供的【资料】回答用户问题，禁止使用资料之外的知识，"
    "禁止编造。如果资料中没有足够依据，请直接回答\"资料中未找到明确依据\"。"
    "优先直接给出结论，复杂内容再分点说明。合理使用 Markdown 排版（标题、列表、表格、"
    "代码块、粗体等），让回答清晰易读。用资料的引用编号标注依据（例如 [1][2]）。"
    "使用简体中文，简洁、准确。"
    "前面可能有历史对话，仅用于理解当前问题的指代/省略；不得把历史对话当作事实依据，"
    "事实必须来自本轮【资料】。"
)


class ChatError(RuntimeError):
    """Chat 生成失败（网络/超时/HTTP/响应结构异常）。"""


CHAT_OUTPUT_MAX_CHARS = 131_072
CHAT_OUTPUT_LIMIT_EXCEEDED = "chat_output_limit_exceeded"
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


@dataclass
class ChatAnswer:
    answer: str
    used_records: list          # 实际进入上下文（截断后）的 records，用于回显 sources


def _tag_prefix_suffix(value: str, tag: str) -> str:
    lowered = value.lower()
    for size in range(min(len(value), len(tag) - 1), 0, -1):
        if lowered.endswith(tag[:size]):
            return value[-size:]
    return ""


class _ThinkingBlockFilter:
    """移除模型错误写入 content 的 <think> 块，并支持标签跨流式增量。"""

    def __init__(self) -> None:
        self._pending = ""
        self._in_thinking = False

    def feed(self, value: str) -> str:
        self._pending += value
        output: list[str] = []
        while self._pending:
            lowered = self._pending.lower()
            tag = _THINK_CLOSE if self._in_thinking else _THINK_OPEN
            position = lowered.find(tag)
            if position >= 0:
                if not self._in_thinking:
                    output.append(self._pending[:position])
                self._pending = self._pending[position + len(tag):]
                self._in_thinking = not self._in_thinking
                continue
            suffix = _tag_prefix_suffix(self._pending, tag)
            stable = self._pending[:-len(suffix)] if suffix else self._pending
            if not self._in_thinking:
                output.append(stable)
            self._pending = suffix
            break
        return "".join(output)

    def finish(self) -> str:
        if self._in_thinking:
            return ""
        value, self._pending = self._pending, ""
        return value


def _without_thinking_blocks(value: str) -> str:
    filter_ = _ThinkingBlockFilter()
    return (filter_.feed(value) + filter_.finish()).strip()


def _endpoint(base_url: str) -> str:
    """归一到 chat/completions：支持传 .../v1，也支持传完整端点。"""
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    return url


def _rec_field(record, name: str, default=""):
    """records 既可能是 DifyRecord（属性）也可能是 dict（键），统一取值。"""
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def build_context(records, max_context_chars: int) -> tuple[str, list]:
    """把 records 拼成带编号资料块，累计长度不超过 max_context_chars。

    返回 (context_str, used_records)：
      - 逐条按 [n] 标题 + 正文拼接；
      - 某条放不下时，若还能放下编号头则截断该条正文后停止，否则直接停止；
      - used_records 是真正进入上下文的子集（用于回显 sources，保证「答案依据 = 引用来源」）。
    """
    parts: list[str] = []
    used: list = []
    total = 0
    for i, r in enumerate(records, 1):
        title = (str(_rec_field(r, "title", "") or "")).strip()
        content = (str(_rec_field(r, "content", "") or "")).strip()
        head = f"[{i}] {title}".rstrip()
        block = f"{head}\n{content}"
        if total + len(block) <= max_context_chars:
            parts.append(block)
            used.append(r)
            total += len(block)
            continue
        # 放不下整块：尝试截断正文塞进剩余预算
        remaining = max_context_chars - total - len(head) - 1   # -1 给 head 与正文之间的换行
        if remaining <= 0:
            break
        parts.append(f"{head}\n{content[:remaining]}")
        used.append(r)
        break
    return "\n\n".join(parts), used


def _messages(query: str, context: str, history: list[dict] | None = None) -> list[dict]:
    user_content = (
        f"用户问题：{query}\n\n"
        f"【资料】\n{context}\n\n"
        "请只依据以上资料作答，并用 [编号] 标注依据；"
        "资料不足时回答「资料中未找到明确依据」。"
    )
    msgs: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    # 历史对话（仅理解追问，不作事实依据）；只接受 user/assistant 文本
    for h in history or []:
        role = h.get("role")
        content = h.get("content")
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": user_content})
    return msgs


def _headers(api_key: str) -> dict | None:
    return {"Authorization": f"Bearer {api_key}"} if api_key else None


async def generate_answer(
    query: str,
    records,
    *,
    base_url: str,
    model: str,
    api_key: str = "",
    timeout: float = 30.0,
    temperature: float = 0.2,
    max_context_chars: int = 12000,
    history: list[dict] | None = None,
) -> ChatAnswer:
    """调 chat 模型生成答案（非流式）。失败/超时/响应异常一律抛 ChatError（不打印 key）。"""
    context, used = build_context(records, max_context_chars)
    payload = {
        "model": model,
        "messages": _messages(query, context, history),
        "temperature": temperature,
        "stream": False,
    }
    if "gemma" in model.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = _headers(api_key)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(_endpoint(base_url), json=payload, headers=headers)
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"]
    except httpx.TimeoutException as exc:
        raise ChatError("chat 模型调用超时") from exc
    except httpx.HTTPError as exc:
        # 不带 key：只暴露异常类型，不打印 headers/url 细节
        raise ChatError(f"chat 模型调用失败：{type(exc).__name__}") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ChatError("chat 模型返回结构异常") from exc
    if not isinstance(answer, str):
        raise ChatError("chat 模型返回空答案")
    answer = _without_thinking_blocks(answer)
    if not answer:
        raise ChatError("chat 模型返回空答案")
    log.info("chat_answer: model=%s ctx=%dchars sources=%d -> %dchars",
             model, len(context), len(used), len(answer))
    return ChatAnswer(answer=answer.strip(), used_records=used)


async def stream_answer(
    query: str,
    records,
    *,
    base_url: str,
    model: str,
    api_key: str = "",
    timeout: float = 30.0,
    temperature: float = 0.2,
    max_context_chars: int = 12000,
    history: list[dict] | None = None,
):
    """流式生成答案：逐段 yield 文本增量（OpenAI SSE delta.content）。

    失败/超时/HTTP 错误抛 ChatError（在生成器内抛出，由调用方捕获后下发 error 事件）。
    与 generate_answer 同样的 prompt/约束；只是 stream=True。绝不打印 key。
    """
    context, _used = build_context(records, max_context_chars)
    payload = {
        "model": model,
        "messages": _messages(query, context, history),
        "temperature": temperature,
        "stream": True,
    }
    if "gemma" in model.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = _headers(api_key)
    output_length = 0
    thinking_filter = _ThinkingBlockFilter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            async with client.stream(
                "POST", _endpoint(base_url), json=payload, headers=headers,
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise ChatError(f"chat 模型 HTTP {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = (obj["choices"][0].get("delta") or {}).get("content")
                    except (KeyError, IndexError, TypeError, ValueError):
                        continue        # 心跳/非内容块，跳过
                    if delta:
                        output_length += len(delta)
                        if output_length > CHAT_OUTPUT_MAX_CHARS:
                            raise ChatError(CHAT_OUTPUT_LIMIT_EXCEEDED)
                        visible = thinking_filter.feed(delta)
                        if visible:
                            yield visible
        visible = thinking_filter.finish()
        if visible:
            yield visible
    except httpx.TimeoutException as exc:
        raise ChatError("chat 模型调用超时") from exc
    except httpx.HTTPError as exc:
        raise ChatError(f"chat 模型调用失败：{type(exc).__name__}") from exc
