"""LLM Query Rewrite（可选）：调 OpenAI 兼容 chat 接口，把用户问题改写成多个**检索 query**。

边界（重要）：
  - 只产出用于向量检索的 query，**绝不回答问题**，不接 Agent、不接工具调用。
  - LLM 输出必须是 JSON 数组字符串，如 ["原问题", "扩展问题1", "扩展问题2"]。
  - 任何失败（网络/超时/HTTP 错误/非法 JSON/结构不符）都返回空列表 []，
    由调用方（retrieval）退回规则 rewrite —— **绝不阻断检索**。

不依赖任何 Agent/LLM SDK，仅用 httpx 直连，便于离线与单测。
"""
from __future__ import annotations

import json
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# 提示词：强约束「只输出 JSON 数组、第一个元素是原问题、不要作答」。
_SYSTEM_PROMPT = (
    "你是一个检索查询改写器。把用户的问题改写成多个用于向量检索的查询，"
    "覆盖同义词、简称/全称、口语与书面表达、可能的专有名词或条款号写法，提升召回。"
    "严格要求："
    "1) 只输出一个 JSON 数组，元素都是字符串，不要任何解释、前后缀或多余文字；"
    "2) 数组第一个元素必须是用户的原始问题原文；"
    "3) 绝不回答问题本身，只产出检索 query；"
    "4) 最多输出 {max_n} 条。"
    '示例：["原问题", "扩展问题1", "扩展问题2"]'
)


def is_configured() -> bool:
    """是否配齐了 LLM 改写所需的最小配置（base_url + model）。"""
    return bool(settings.query_rewrite_llm_base_url and settings.query_rewrite_llm_model)


def _endpoint(base_url: str) -> str:
    """把 base_url 归一到 chat/completions 端点：既支持传 .../v1，也支持传完整端点。"""
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    return url


def _load_array(s: str) -> list:
    """把文本解析成 JSON 数组。

    1) 优先整体解析：是合法 JSON 但**不是数组**（如 {"queries": [...]}）→ 视为非法格式拒绝；
    2) 整体解析失败时，才从散文中截取首个 [ 到末个 ] 再解析（容忍模型加了前后缀说明）。
    任何情况下解析不出「数组」都抛异常，由 generate() 统一兜底成 []。
    """
    try:
        whole = json.loads(s)
    except ValueError:
        whole = None
    if isinstance(whole, list):
        return whole
    if whole is not None:                         # 合法 JSON 但非数组 → 不接受
        raise ValueError("LLM 输出 JSON 不是数组")
    lo, hi = s.find("["), s.rfind("]")            # 散文里夹带数组片段
    if lo != -1 and hi != -1 and hi > lo:
        data = json.loads(s[lo:hi + 1])
        if isinstance(data, list):
            return data
    raise ValueError("LLM 输出中未找到 JSON 数组")


def _parse_content(content: str, *, max_queries: int) -> list[str]:
    """从 LLM 回复文本解析出 query 列表（去 ```代码围栏``` 后交给 _load_array）。"""
    s = (content or "").strip()
    if s.startswith("```"):                       # ```json\n[...]\n``` / ```\n[...]\n```
        s = s.strip("`").strip()
        if "\n" in s:
            first, rest = s.split("\n", 1)
            if first.strip().lower() in ("json", ""):
                s = rest.strip()
    data = _load_array(s)
    out = [x.strip() for x in data if isinstance(x, str) and x.strip()]
    return out[:max_queries]


async def generate(
    query: str,
    *,
    base_url: str,
    model: str,
    api_key: str = "",
    timeout: float = 8.0,
    max_queries: int = 4,
) -> list[str]:
    """调 LLM 生成检索 query 列表。任何异常都吞掉并返回 []（绝不抛、绝不阻断检索）。"""
    if not base_url or not model:
        return []
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT.format(max_n=max_queries)},
            {"role": "user", "content": query},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    if "gemma" in model.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(_endpoint(base_url), json=payload, headers=headers)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        result = _parse_content(content, max_queries=max_queries)
        log.info("llm_query_rewrite: query=%r -> %d queries", query, len(result))
        return result
    except Exception as exc:  # 任何异常都不得阻断检索 → 退回规则 rewrite
        log.warning("llm_query_rewrite failed (%s): %s; fallback to rule rewrite",
                    type(exc).__name__, exc)
        return []
