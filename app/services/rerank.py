"""Rerank 客户端（标准 /rerank 格式，Infinity / TEI / Jina / Cohere 兼容）。

召回后用 reranker 按 query 对候选重排。默认关闭（全局未开或未配地址即跳过）。
仿 app/services/embedding.py：async httpx + 有 key 才发 Authorization 头。
"""
from __future__ import annotations

import logging
from typing import Sequence

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0)


class RerankError(RuntimeError):
    pass


def is_configured() -> bool:
    """是否配好了 reranker（地址 + 模型都非空）。"""
    return bool(settings.rerank_base_url and settings.rerank_model)


def fill_order(ranked_order: Sequence[int], total: int) -> list[int]:
    """重排后补满：reranker 返回的合法下标序 + 原向量序里未覆盖的下标。

    reranker 可能只返回部分候选（少于 top_k）；用原向量顺序（0..total-1）把余下名额
    补满，保证最终候选数不少于召回数（再由调用方截到 top_k），避免丢召回结果。

    生产防御：外部 reranker 异常时可能返回越界下标（如 total=3 却给 999，会让 raw[i]
    越界）或重复下标（会重复返回同一条）。这里只接受 0<=idx<total 且去重（保留首次出现）。
    纯函数，便于单测。
    """
    seen: set[int] = set()
    order: list[int] = []
    for idx in ranked_order:
        if 0 <= idx < total and idx not in seen:
            seen.add(idx)
            order.append(idx)
    order.extend(i for i in range(total) if i not in seen)
    return order


async def rank_candidates(
    query: str,
    contents: Sequence[str],
    *,
    top_k: int,
    enabled: bool,
    log_label: str = "",
) -> tuple[list[int], dict[int, float]]:
    """召回候选重排 → (order, rerank_scores)。Dify 检索与库内查询共用。

    - enabled=False 或无候选：返回原向量序、空分数（不发请求）。
    - 重排成功：order 为重排序补满后截到 top_k；rerank_scores 仅含合法且命中的下标。
    - 重排失败：打 ERROR（带 log_label 区分来源）后回退原向量序（绝不阻断检索）。
    """
    total = len(contents)
    order = list(range(total))
    scores: dict[int, float] = {}
    if enabled and total:
        try:
            ranked = await rerank(query, contents, top_n=top_k)
            order = fill_order([idx for idx, _ in ranked], total)
            # 与 fill_order 同口径：合法 + 首次出现优先。重复 index 时 ranked 已按分降序，
            # 首次即最高分；若用字典推导式会被末项（较低分）覆盖，造成顺序按高分、展示按低分。
            for idx, sc in ranked:
                if 0 <= idx < total and idx not in scores:
                    scores[idx] = sc
        except Exception as exc:  # noqa: BLE001
            suffix = f" ({log_label})" if log_label else ""
            log.error("rerank failed%s, fallback to vector order: %s", suffix, exc)
            order = list(range(total))
    return order[:top_k], scores


def _parse_results(body: dict, top_n: int) -> list[tuple[int, float]]:
    """解析 rerank 响应 {"results":[{"index","relevance_score"}]}。

    兼容两种包装：标准格式直接在 body["results"]；DashScope 原生在 body["output"]["results"]。
    单项结构两家一致（index + relevance_score）。

    跳过缺 index 的项，缺分数按 0.0，按分降序，去重后截到 top_n。纯函数，便于单测。

    去重在截断之前：标准 reranker 不该返回重复 index，但异常时若重复，降序后保留首次
    （最高分）出现，避免重复 index 占用 top_n 名额、把合法的不同文档挤出结果。
    """
    results = body.get("results")
    if results is None:
        results = (body.get("output") or {}).get("results")
    results = results or []
    out: list[tuple[int, float]] = []
    for item in results:
        idx = item.get("index")
        if idx is None:
            continue
        score = item.get("relevance_score")
        out.append((int(idx), float(score) if score is not None else 0.0))
    out.sort(key=lambda item: (-item[1], item[0]))
    deduped: list[tuple[int, float]] = []
    seen: set[int] = set()
    for idx, sc in out:
        if idx not in seen:
            seen.add(idx)
            deduped.append((idx, sc))
    return deduped[:top_n]


async def rerank(
    query: str,
    documents: Sequence[str],
    *,
    top_n: int,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
) -> list[tuple[int, float]]:
    """对 documents 按与 query 的相关性重排。

    返回 [(原始下标, relevance_score), ...]，按分降序，长度 <= top_n。
    标准 /rerank 响应：{"results": [{"index": i, "relevance_score": s}, ...]}。

    provider 显式传入时覆盖全局 settings.rerank_provider（调用方可强制 "standard"，
    不受 .env 的 RERANK_PROVIDER 影响）；为 None 时回退 settings。
    """
    if not documents:
        return []
    url = base_url or settings.rerank_base_url
    # key 优先级：显式传入 > RERANK_API_KEY > 复用 EMBEDDING_API_KEY（同服务商同 key 省事）
    key = api_key if api_key is not None else (settings.rerank_api_key or settings.embedding_api_key)
    mdl = model or settings.rerank_model
    if (provider or settings.rerank_provider or "standard").lower() == "dashscope":
        # DashScope 原生 text-rerank：input/parameters 结构，结果在 output.results
        payload = {
            "model": mdl,
            "input": {"query": query, "documents": list(documents)},
            "parameters": {"return_documents": False, "top_n": top_n},
        }
    else:
        payload = {
            "model": mdl,
            "query": query,
            "documents": list(documents),
            "top_n": top_n,
        }
    headers = {"Authorization": f"Bearer {key}"} if key else None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RerankError(f"rerank service {resp.status_code}: {resp.text[:300]}")
    return _parse_results(resp.json(), top_n)
