"""bge-m3 embedding 客户端（OpenAI 兼容 /v1/embeddings，async httpx）。

迁移自 cpwsImportData/importdata/ingest.py 的 get_embeddings()，改为 async 并去掉案件域字段。
"""
from __future__ import annotations

import logging
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=60.0, pool=5.0)


class EmbeddingError(RuntimeError):
    pass


class EmbeddingEndpointError(ValueError):
    """Embedding endpoint is not safe for a library-level override."""


def canonical_embedding_endpoint(value: str) -> str:
    """Return the comparison form used for the global trusted endpoint."""
    if not isinstance(value, str) or not value.strip():
        raise EmbeddingEndpointError("embedding endpoint must be a non-empty URL")
    raw = value.strip()
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise EmbeddingEndpointError("embedding endpoint contains invalid whitespace")
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        port = parsed.port
    except ValueError as exc:
        raise EmbeddingEndpointError("embedding endpoint URL is invalid") from exc
    if scheme not in {"http", "https"}:
        raise EmbeddingEndpointError("embedding endpoint scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise EmbeddingEndpointError("embedding endpoint userinfo is not allowed")
    if parsed.query or parsed.fragment:
        raise EmbeddingEndpointError("embedding endpoint query and fragment are not allowed")
    host = parsed.hostname
    if not host:
        raise EmbeddingEndpointError("embedding endpoint host is required")
    canonical_host = host.rstrip(".").lower()
    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        canonical_netloc = canonical_host
    else:
        canonical_netloc = f"{canonical_host}:{port}"
    path = parsed.path or "/"
    path = path.rstrip("/") or "/"
    return urlunsplit((scheme, canonical_netloc, path, "", ""))


def validate_library_embedding_endpoint(value: str | None) -> str | None:
    """Allow only the globally configured embedding endpoint as a library override."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise EmbeddingEndpointError("embedding endpoint must be a string")
    if not value.strip():
        return value
    raw = value.strip()
    candidate = canonical_embedding_endpoint(raw)
    global_endpoint = canonical_embedding_endpoint(settings.embedding_base_url)
    if candidate != global_endpoint:
        raise EmbeddingEndpointError("library embedding endpoint must match the global endpoint")
    return raw


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
    requested_url = base_url.strip() if isinstance(base_url, str) and base_url.strip() else settings.embedding_base_url
    try:
        url = validate_library_embedding_endpoint(requested_url)
        assert url is not None
        is_global_endpoint = canonical_embedding_endpoint(url) == canonical_embedding_endpoint(
            settings.embedding_base_url
        )
    except EmbeddingEndpointError as exc:
        raise EmbeddingError(str(exc)) from exc
    key = (api_key if api_key is not None else settings.embedding_api_key) if is_global_endpoint else None
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
