"""Qdrant 客户端封装（async httpx）。

复用 cpwsImportData/services/qdrant_client.py 的接口契约，扩展为：
  - 每个 library 自己的 collection
  - delete_points_by_document_id：库删文档时清向量
  - async：与 FastAPI 同语义
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=5.0)
_EXACT_TIE_PROBE_MAX = 201


class QdrantDeterminismError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if settings.qdrant_api_key:
        h["api-key"] = settings.qdrant_api_key
    return h


def _url(path: str) -> str:
    return f"{settings.qdrant_url.rstrip('/')}{path}"


async def collection_exists(collection: str) -> bool:
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(_url(f"/collections/{collection}"), headers=_headers())
    return resp.status_code == 200


async def ensure_collection(
    collection: str,
    *,
    dim: int,
    distance: str = "Cosine",
    shard_number: int = 1,
    hnsw_m: int = 16,
    hnsw_ef_construct: int = 128,
    quantization_type: str | None = "int8",
) -> bool:
    """存在则跳过；不存在则按参数创建。返回 True 表示新建。"""
    if await collection_exists(collection):
        log.debug("qdrant collection %s already exists", collection)
        return False

    payload: dict[str, Any] = {
        "vectors": {"size": dim, "distance": distance.capitalize()},
        "shard_number": shard_number,
        "hnsw_config": {"m": hnsw_m, "ef_construct": hnsw_ef_construct},
    }
    if quantization_type:
        payload["quantization_config"] = {
            "scalar": {"type": quantization_type, "quantile": 0.99, "always_ram": True}
        }
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.put(
            _url(f"/collections/{collection}"),
            headers=_headers(),
            json=payload,
        )
    resp.raise_for_status()
    log.info("qdrant created collection %s (dim=%s, distance=%s)", collection, dim, distance)
    return True


async def delete_collection(collection: str) -> None:
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.delete(_url(f"/collections/{collection}"), headers=_headers())
    if resp.status_code in (200, 404):
        return
    resp.raise_for_status()


async def upsert_points(collection: str, points: list[dict[str, Any]], *, timeout: float | None = None) -> None:
    if not points:
        return
    client_timeout = httpx.Timeout(timeout) if timeout is not None else _DEFAULT_TIMEOUT
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        resp = await client.put(
            _url(f"/collections/{collection}/points?wait=true"),
            headers=_headers(),
            json={"points": points},
        )
    resp.raise_for_status()


async def delete_points_by_document_id(collection: str, document_id: str) -> None:
    """按 payload.document_id 过滤删除（删文档全部 points）。collection 不存在视为成功（幂等）。"""
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.post(
            _url(f"/collections/{collection}/points/delete?wait=true"),
            headers=_headers(),
            json={"filter": {"must": [{"key": "document_id", "match": {"value": document_id}}]}},
        )
    if resp.status_code == 404:
        return
    resp.raise_for_status()


async def delete_points_by_document_revision_id(collection: str, document_revision_id: str) -> None:
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.post(
            _url(f"/collections/{collection}/points/delete?wait=true"),
            headers=_headers(),
            json={
                "filter": {
                    "must": [
                        {"key": "document_revision_id", "match": {"value": document_revision_id}}
                    ]
                }
            },
        )
    if resp.status_code == 404:
        return
    resp.raise_for_status()


async def delete_points_before_revision(collection: str, document_id: str, target_revision: int) -> None:
    """删该 document_id 中 document_revision 缺失或 < target 的 points（更新后清旧版本，#7 §6.1）。

    选择删除：document_id==X AND NOT(document_revision >= target) →
    保留 == target（当前版本），删 < target 与缺 revision 的历史 points。collection 不存在视为成功。
    """
    flt = {
        "must": [{"key": "document_id", "match": {"value": document_id}}],
        "must_not": [{"key": "document_revision", "range": {"gte": target_revision}}],
    }
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.post(
            _url(f"/collections/{collection}/points/delete?wait=true"),
            headers=_headers(),
            json={"filter": flt},
        )
    if resp.status_code == 404:
        return
    resp.raise_for_status()


async def search(
    collection: str,
    vector: list[float],
    *,
    limit: int,
    score_threshold: float | None = None,
    payload_filter: dict[str, Any] | None = None,
    with_payload: bool = True,
    exact: bool = False,
) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    if exact and limit >= _EXACT_TIE_PROBE_MAX:
        raise ValueError("exact limit must be below deterministic probe bound")
    requested_limit = limit
    query_limit = min(limit + 1, _EXACT_TIE_PROBE_MAX) if exact else limit
    payload: dict[str, Any] = {
        "vector": vector,
        "limit": query_limit,
        "with_payload": with_payload,
    }
    if score_threshold is not None and score_threshold > 0:
        payload["score_threshold"] = score_threshold
    if payload_filter:
        payload["filter"] = payload_filter
    if exact:
        payload["params"] = {"exact": True}
    while True:
        payload["limit"] = query_limit
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                _url(f"/collections/{collection}/points/search"),
                headers=_headers(),
                json=payload,
            )
        resp.raise_for_status()
        results = sorted(
            resp.json().get("result", []),
            key=lambda item: (
                -float(item.get("score") or 0.0),
                str((item.get("payload") or {}).get("chunk_id") or item.get("id")),
            ),
        )
        if not exact or len(results) <= requested_limit or len(results) < query_limit:
            return results[:requested_limit]
        cutoff_score = float(results[requested_limit - 1].get("score") or 0.0)
        next_score = float(results[requested_limit].get("score") or 0.0)
        if cutoff_score != next_score:
            return results[:requested_limit]
        tail_score = float(results[-1].get("score") or 0.0)
        if tail_score != cutoff_score:
            return results[:requested_limit]
        if query_limit >= _EXACT_TIE_PROBE_MAX:
            raise QdrantDeterminismError("exact search tie exceeds deterministic probe bound")
        query_limit = min(query_limit * 2, _EXACT_TIE_PROBE_MAX)
