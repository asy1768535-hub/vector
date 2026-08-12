from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.schemas.admin import LibraryCreate, LibraryUpdate
from app.services import embedding


@pytest.mark.parametrize(
    "url",
    [
        "ftp://embedding.example/v1/embeddings",
        "file:///tmp/embeddings",
        "https://attacker.example/v1/embeddings",
        "https://trusted.embedding.example.evil/v1/embeddings",
        "https://trusted.embedding.example@attacker.example/v1/embeddings",
        "http://localhost/v1/embeddings",
        "http://127.0.0.1/v1/embeddings",
        "http://10.0.0.1/v1/embeddings",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_library_endpoint_rejects_non_global_and_confusable_urls(monkeypatch, url: str):
    monkeypatch.setattr(settings, "embedding_base_url", "https://trusted.embedding.example/v1/embeddings")
    with pytest.raises(ValueError):
        LibraryCreate(slug="endpoint_test", name="Endpoint test", embedding_base_url=url)
    with pytest.raises(ValueError):
        LibraryUpdate(embedding_base_url=url)
    with pytest.raises(embedding.EmbeddingError):
        asyncio.run(embedding.embed_texts(["attack"], base_url=url))


class _EmbeddingResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {"data": [{"embedding": [0.1, 0.2]}]}


class _FakeEmbeddingClient:
    def __init__(self, captured: dict[str, object], **_kwargs):
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, json, headers):
        self.captured.update(url=url, json=json, headers=headers)
        return _EmbeddingResponse()


def test_canonical_global_endpoint_keeps_global_key(monkeypatch):
    global_url = "https://Trusted.Embedding.Example:443/v1/embeddings/"
    monkeypatch.setattr(settings, "embedding_base_url", global_url)
    monkeypatch.setattr(settings, "embedding_api_key", "GLOBAL-EMBEDDING-KEY")

    global_capture: dict[str, object] = {}

    with pytest.MonkeyPatch.context() as local:
        local.setattr(
            embedding.httpx,
            "AsyncClient",
            lambda **kwargs: _FakeEmbeddingClient(global_capture, **kwargs),
        )
        assert asyncio.run(
            embedding.embed_texts(
                ["global"],
                base_url="https://trusted.embedding.example/v1/embeddings",
            )
        ) == [[0.1, 0.2]]

    assert global_capture["headers"] == {"Authorization": "Bearer GLOBAL-EMBEDDING-KEY"}
