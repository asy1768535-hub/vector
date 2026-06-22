"""bge-m3 embedding 客户端（OpenAI 兼容 /v1/embeddings，async httpx）。

迁移自 cpwsImportData/importdata/ingest.py 的 get_embeddings()，改为 async 并去掉案件域字段。
"""
from __future__ import annotations

import logging
from typing import Sequence

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=60.0, pool=5.0)


class EmbeddingError(RuntimeError):
    pass


async def embed_texts(
    texts: Sequence[str],
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> list[list[float]]:
    """批量向量化。返回与 texts 等长的向量列表。

    base_url：库级覆盖；不传则用全局 settings.embedding_base_url。
    model：同上。
    api_key：远程服务鉴权（如阿里云 DashScope）；不传则用全局 settings.embedding_api_key。
        为空时不发 Authorization 头（本地 bge-m3 无需鉴权）。
    """
    if not texts:
        return []
    payload = {"model": model or settings.embedding_model, "input": list(texts)}
    url = base_url or settings.embedding_base_url
    key = api_key if api_key is not None else settings.embedding_api_key
    headers = {"Authorization": f"Bearer {key}"} if key else None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise EmbeddingError(f"embedding service {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    data = body.get("data") or []
    if len(data) != len(texts):
        raise EmbeddingError(
            f"embedding count mismatch: got {len(data)}, expected {len(texts)}"
        )
    out: list[list[float]] = []
    for item in data:
        vec = item.get("embedding")
        if not isinstance(vec, list):
            raise EmbeddingError(f"missing embedding in item: {item}")
        out.append(vec)
    return out


async def embed_one(
    text: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> list[float]:
    vectors = await embed_texts([text], model=model, base_url=base_url, api_key=api_key)
    return vectors[0]
