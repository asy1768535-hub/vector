from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import public_v1 as api
from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.user import User
from app.schemas.dify import DifyRecord
from app.schemas.public_v1 import (
    PublicChunkRead,
    PublicGraphRead,
    PublicScopeSelection,
    PublicSourceRead,
)
from app.services import public_v1 as service
from app.services.chat_answer import ChatError
from app.services.public_v1_contracts import PublicAPIError


NOW = datetime(2026, 7, 23, 8, 30, tzinfo=timezone.utc)


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=True,
        is_superuser=False,
        is_verified=True,
        created_at=NOW,
    )


def _library(organization_id: uuid.UUID) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization_id,
        slug="alpha",
        name="Alpha",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="collection_alpha",
        index_state="ready",
        created_at=NOW,
    )


def _prepared(*, with_record: bool = True) -> service.PreparedPublicRetrieval:
    organization_id = uuid.uuid4()
    library = _library(organization_id)
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    source = PublicSourceRead(
        rank=1,
        library_id=library.id,
        library_slug=library.slug,
        library_name=library.name,
        document_id=document_id,
        document_revision_id=revision_id,
        chunk_id=chunk_id,
        score=0.5,
    )
    chunk = PublicChunkRead(
        rank=1,
        library_id=library.id,
        library_slug=library.slug,
        document_id=document_id,
        document_revision_id=revision_id,
        chunk_id=chunk_id,
        content="grounded evidence",
        content_truncated=False,
        score=0.5,
    )
    record = DifyRecord(
        content="grounded evidence",
        score=0.5,
        title="Evidence",
        metadata={"public_rank": 1},
    )
    return service.PreparedPublicRetrieval(
        selection=PublicScopeSelection(library_slugs=[library.slug]),
        scope=service.ResolvedPublicScope(organization_id, None, (library,)),
        sources=(source,) if with_record else (),
        chunks=(chunk,) if with_record else (),
        graph=PublicGraphRead(
            available=False,
            entities=[],
            relations=[],
            documents_examined=0,
            truncated=False,
        ),
        records=(record,) if with_record else (),
    )


@pytest.fixture
def public_client():
    user = _user()
    db = object()
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app), user, db
    finally:
        app.dependency_overrides.clear()


def test_default_off_runs_before_authentication_body_validation_or_data_access():
    def forbidden_dependency():
        raise AssertionError("customer dependency must not run")

    app.dependency_overrides[current_active_user] = forbidden_dependency
    app.dependency_overrides[get_db] = forbidden_dependency
    try:
        with patch.object(settings, "public_api_v1_enabled", False):
            response = TestClient(app).post(
                "/api/v1/retrieval",
                json={"unexpected": "body"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["request_id"] == response.headers["X-Request-Id"]


def test_enabled_validation_uses_stable_content_free_envelope(public_client):
    client, _, _ = public_client
    with patch.object(settings, "public_api_v1_enabled", True):
        response = client.post(
            "/api/v1/retrieval",
            json={"scope": {"library_slugs": []}, "query": "secret question"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_invalid"
    assert response.json()["error"]["details"] == []
    assert "secret question" not in response.text
    assert response.json()["error"]["request_id"] == response.headers["X-Request-Id"]


def test_unknown_path_or_method_uses_same_public_error_boundary(public_client):
    client, _, _ = public_client
    with patch.object(settings, "public_api_v1_enabled", True):
        wrong_method = client.post("/api/v1/libraries")
        unknown_path = client.get("/api/v1/not-a-capability")
    for response in (wrong_method, unknown_path):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert response.json()["error"]["request_id"] == response.headers[
            "X-Request-Id"
        ]


def test_retrieval_route_delegates_once_and_returns_request_id(public_client):
    client, _, db = public_client
    prepared = _prepared()
    with (
        patch.object(settings, "public_api_v1_enabled", True),
        patch.object(
            api,
            "prepare_public_retrieval",
            new=AsyncMock(return_value=prepared),
        ) as retrieve,
    ):
        response = client.post(
            "/api/v1/retrieval",
            json={
                "scope": {"library_slugs": ["alpha"]},
                "query": "question",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_version"] == "public-retrieval-v1"
    assert payload["request_id"] == response.headers["X-Request-Id"]
    assert payload["chunks"][0]["content"] == "grounded evidence"
    assert retrieve.await_count == 1
    assert retrieve.await_args.args[0] is db


class _Provider:
    def __init__(self, values=(), error: Exception | None = None):
        self.values = list(values)
        self.error = error
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.values:
            return self.values.pop(0)
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


def test_stream_emits_meta_bounded_delta_and_exact_terminal_result(public_client):
    client, _, _ = public_client
    prepared = _prepared()
    provider = _Provider(("x" * 2_500, " final"))
    with (
        patch.object(settings, "public_api_v1_enabled", True),
        patch.object(settings, "chat_enabled", True),
        patch.object(settings, "chat_base_url", "http://model/v1"),
        patch.object(settings, "chat_model", "model"),
        patch.object(
            api,
            "prepare_public_retrieval",
            new=AsyncMock(return_value=prepared),
        ),
        patch.object(api, "recheck_public_scope", new=AsyncMock()) as recheck,
        patch.object(api.chat_answer, "stream_answer", return_value=provider),
    ):
        response = client.post(
            "/api/v1/answers/stream",
            json={
                "scope": {"library_slugs": ["alpha"]},
                "query": "question",
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: meta\n") == 1
    assert response.text.count("event: delta\n") == 3
    assert response.text.count("event: result\n") == 1
    assert "event: error\n" not in response.text
    assert '"answer":"' + ("x" * 2_500) + " final" in response.text
    assert response.headers["X-Request-Id"] in response.text
    assert provider.closed is True
    assert recheck.await_count == 2


def test_stream_provider_failure_is_content_free_and_has_no_result(public_client):
    client, _, _ = public_client
    prepared = _prepared()
    provider = _Provider(error=ChatError("secret provider payload"))
    with (
        patch.object(settings, "public_api_v1_enabled", True),
        patch.object(settings, "chat_enabled", True),
        patch.object(settings, "chat_base_url", "http://model/v1"),
        patch.object(settings, "chat_model", "model"),
        patch.object(
            api,
            "prepare_public_retrieval",
            new=AsyncMock(return_value=prepared),
        ),
        patch.object(api, "recheck_public_scope", new=AsyncMock()),
        patch.object(api.chat_answer, "stream_answer", return_value=provider),
    ):
        response = client.post(
            "/api/v1/answers/stream",
            json={
                "scope": {"library_slugs": ["alpha"]},
                "query": "secret question",
            },
        )
    assert response.status_code == 200
    assert response.text.count("event: error\n") == 1
    assert "event: result\n" not in response.text
    assert "secret provider payload" not in response.text
    assert "secret question" not in response.text
    assert '"code":"upstream_failed"' in response.text
    assert provider.closed is True


def test_stream_scope_revocation_before_provider_emits_error_without_model(
    public_client,
):
    client, _, _ = public_client
    prepared = _prepared()
    recheck = AsyncMock(
        side_effect=[
            None,
            PublicAPIError("scope_forbidden", status_code=403),
        ]
    )
    with (
        patch.object(settings, "public_api_v1_enabled", True),
        patch.object(settings, "chat_enabled", True),
        patch.object(settings, "chat_base_url", "http://model/v1"),
        patch.object(settings, "chat_model", "model"),
        patch.object(
            api,
            "prepare_public_retrieval",
            new=AsyncMock(return_value=prepared),
        ),
        patch.object(api, "recheck_public_scope", new=recheck),
        patch.object(api.chat_answer, "stream_answer") as provider,
    ):
        response = client.post(
            "/api/v1/answers/stream",
            json={
                "scope": {"library_slugs": ["alpha"]},
                "query": "secret question",
            },
        )
    assert response.status_code == 200
    assert response.text.count("event: error\n") == 1
    assert '"code":"scope_forbidden"' in response.text
    assert "event: meta\n" not in response.text
    assert "secret question" not in response.text
    provider.assert_not_called()
    assert recheck.await_count == 2


@pytest.mark.asyncio
async def test_stream_cancellation_closes_provider_iterator(monkeypatch):
    prepared = _prepared()
    provider = _Provider(("first", "second"))
    monkeypatch.setattr(api.chat_answer, "stream_answer", lambda *args, **kwargs: provider)
    stream = api._public_answer_events(
        request_id="0123456789abcdef0123456789abcdef",
        query="question",
        prepared=prepared,
    )
    assert "event: meta" in await anext(stream)
    assert "event: delta" in await anext(stream)
    await stream.aclose()
    assert provider.closed is True


def test_empty_stream_never_starts_provider(public_client):
    client, _, _ = public_client
    prepared = _prepared(with_record=False)
    with (
        patch.object(settings, "public_api_v1_enabled", True),
        patch.object(
            api,
            "prepare_public_retrieval",
            new=AsyncMock(return_value=prepared),
        ),
        patch.object(api, "recheck_public_scope", new=AsyncMock()),
        patch.object(api.chat_answer, "stream_answer") as provider,
    ):
        response = client.post(
            "/api/v1/answers/stream",
            json={
                "scope": {"library_slugs": ["alpha"]},
                "query": "question",
            },
        )
    assert response.status_code == 200
    assert service.NO_EVIDENCE_ANSWER in response.text
    assert response.text.count("event: result\n") == 1
    provider.assert_not_called()
