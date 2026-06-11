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
) -> list[list[float]]:
    """批量向量化。返回与 texts 等长的向量列表。

    base_url：库级覆盖；不传则用全局 settings.embedding_base_url。
    model：同上。
    """
    if not texts:
        return []
    payload = {"model": model or settings.embedding_model, "input": list(texts)}
    url = base_url or settings.embedding_base_url
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
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
    text: str, *, model: str | None = None, base_url: str | None = None
) -> list[float]:
    vectors = await embed_texts([text], model=model, base_url=base_url)
    return vectors[0]
