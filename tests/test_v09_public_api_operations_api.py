from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import StreamingResponse

from app.api import public_v1 as api
from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.user import User
from app.schemas.dify import DifyRecord
from app.schemas.public_v1 import PublicChunkRead, PublicGraphRead, PublicSourceRead
from app.services import public_v1 as service
from app.services.chat_answer import ChatError
from app.services.organization_authorization import bind_credential_organization
from app.services.public_api_operations_contracts import (
    PUBLIC_ENDPOINT_KEYS,
    PublicAnswerLeaseGrant,
    PublicOperationContext,
)
from app.services.public_v1_contracts import (
    PublicAPIError,
    wrap_public_streaming_response,
)


NOW = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)


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


def _prepared(*, with_record: bool = True) -> service.PreparedPublicRetrieval:
    organization_id = uuid.uuid4()
    library = Library(
        id=uuid.uuid4(),
        organization_id=organization_id,
        slug="alpha",
        name="Alpha",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="ops_alpha",
        index_state="ready",
        created_at=NOW,
    )
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
        score=0.75,
    )
    chunk = PublicChunkRead(
        rank=1,
        library_id=library.id,
        library_slug=library.slug,
        document_id=document_id,
        document_revision_id=revision_id,
        chunk_id=chunk_id,
        content="private grounded evidence",
        content_truncated=False,
        score=0.75,
    )
    record = DifyRecord(
        content="private grounded evidence",
        score=0.75,
        title="Private title",
        metadata={"public_rank": 1},
    )
    return service.PreparedPublicRetrieval(
        selection={"library_slugs": ["alpha"]},
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
def operations_client():
    user = _user()
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: object()
    try:
        yield TestClient(app), user
    finally:
        app.dependency_overrides.clear()


@contextmanager
def _operation_flags():
    with (
        patch.object(settings, "public_api_v1_enabled", True),
        patch.object(settings, "public_api_operations_enabled", True),
    ):
        yield


def test_authenticated_validation_failure_is_recorded_without_request_content(
    operations_client,
):
    client, user = operations_client
    recorder = AsyncMock(return_value=True)
    with (
        _operation_flags(),
        patch(
            "app.services.public_v1_contracts.record_public_operation",
            new=recorder,
        ),
    ):
        response = client.post(
            "/api/v1/retrieval",
            json={
                "scope": {"library_slugs": []},
                "query": "private question",
            },
        )
    assert response.status_code == 422
    assert recorder.await_count == 1
    record = recorder.await_args.args[0]
    assert record.user_id == user.id
    assert record.endpoint_key == "retrieval.search"
    assert record.error_code == "request_invalid"
    assert record.outcome == "failed"
    assert record.organization_id is None
    assert "private question" not in repr(record)


def test_all_official_routes_have_one_unique_operational_endpoint_key():
    routes = [
        route
        for route in api.router.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/api/v1")
        and route.include_in_schema
    ]
    keys = []
    for route in routes:
        matches = [
            dependency.call.public_endpoint_key
            for dependency in route.dependant.dependencies
            if hasattr(dependency.call, "public_endpoint_key")
        ]
        assert len(matches) == 1, route.path
        keys.extend(matches)
    assert len(routes) == 11
    assert set(keys) == set(PUBLIC_ENDPOINT_KEYS)
    assert len(keys) == len(set(keys))


def test_unknown_and_unauthenticated_requests_do_not_create_records(operations_client):
    client, _ = operations_client
    recorder = AsyncMock(return_value=True)
    with (
        _operation_flags(),
        patch(
            "app.services.public_v1_contracts.record_public_operation",
            new=recorder,
        ),
    ):
        unknown = client.get("/api/v1/unknown-capability")
    assert unknown.status_code == 404
    recorder.assert_not_awaited()

    def unauthenticated():
        raise HTTPException(status_code=401, detail="secret credential failure")

    app.dependency_overrides[current_active_user] = unauthenticated
    try:
        with (
            _operation_flags(),
            patch(
                "app.services.public_v1_contracts.record_public_operation",
                new=recorder,
            ),
        ):
            response = client.get("/api/v1/libraries")
    finally:
        app.dependency_overrides[current_active_user] = lambda: _user()
    assert response.status_code == 401
    recorder.assert_not_awaited()


def test_api_key_uuid_is_recorded_but_no_credential_material(operations_client):
    client, user = operations_client
    organization_id = uuid.uuid4()
    api_key_id = uuid.uuid4()
    bind_credential_organization(user, organization_id, api_key_id)
    recorder = AsyncMock(return_value=True)

    async def list_libraries(db, *, user, operation_context):  # noqa: ARG001
        operation_context.bind_scope(organization_ids=(organization_id,))
        return (), False

    with (
        _operation_flags(),
        patch.object(api, "list_public_libraries", side_effect=list_libraries),
        patch(
            "app.services.public_v1_contracts.record_public_operation",
            new=recorder,
        ),
    ):
        response = client.get("/api/v1/libraries")
    assert response.status_code == 200
    record = recorder.await_args.args[0]
    assert record.organization_id == organization_id
    assert record.api_key_id == api_key_id
    assert "plaintext_key_material" not in repr(record)


def test_rate_limited_stream_returns_http_429_before_sse(operations_client):
    client, _ = operations_client
    recorder = AsyncMock(return_value=True)
    limited = PublicAPIError(
        "rate_limited",
        status_code=429,
        retry_after_seconds=17,
    )
    with (
        _operation_flags(),
        patch.object(
            api,
            "prepare_public_retrieval",
            new=AsyncMock(side_effect=limited),
        ),
        patch(
            "app.services.public_v1_contracts.record_public_operation",
            new=recorder,
        ),
    ):
        response = client.post(
            "/api/v1/answers/stream",
            json={"scope": {"library_slugs": ["alpha"]}, "query": "private"},
        )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert response.json()["error"]["code"] == "rate_limited"
    record = recorder.await_args.args[0]
    assert record.http_status == 429
    assert record.error_code == "rate_limited"
    assert "private" not in response.text


def test_sync_answer_failure_releases_lease_and_records_terminal_failure(
    operations_client,
):
    client, _ = operations_client
    prepared = _prepared()
    recorder = AsyncMock(return_value=True)
    releaser = AsyncMock(return_value=True)

    async def answer(db, *, user, body, request_id, operation_context):  # noqa: ARG001
        operation_context.bind_scope(
            organization_ids=(prepared.scope.organization_id,),
            library_ids=(prepared.scope.libraries[0].id,),
        )
        operation_context.answer_lease = PublicAnswerLeaseGrant(
            request_id=request_id,
            organization_id=prepared.scope.organization_id,
            api_key_id=None,
            expires_at=NOW + timedelta(seconds=330),
        )
        raise PublicAPIError("upstream_failed", status_code=502)

    with (
        _operation_flags(),
        patch.object(api, "generate_public_answer", side_effect=answer),
        patch(
            "app.services.public_v1_contracts.record_public_operation",
            new=recorder,
        ),
        patch(
            "app.services.public_v1_contracts.release_public_answer_lease",
            new=releaser,
        ),
    ):
        response = client.post(
            "/api/v1/answers",
            json={"scope": {"library_slugs": ["alpha"]}, "query": "private"},
        )
    assert response.status_code == 502
    releaser.assert_awaited_once_with(response.headers["X-Request-Id"])
    record = recorder.await_args.args[0]
    assert record.outcome == "failed"
    assert record.error_code == "upstream_failed"


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


@pytest.mark.parametrize(
    ("provider", "expected_outcome", "expected_error"),
    [
        (_Provider(("grounded",)), "completed", None),
        (_Provider(error=ChatError("private provider payload")), "failed", "upstream_failed"),
    ],
)
def test_stream_terminal_lifecycle_releases_once_and_records_outcome(
    operations_client,
    provider,
    expected_outcome,
    expected_error,
):
    client, _ = operations_client
    prepared = _prepared()
    recorder = AsyncMock(return_value=True)
    releaser = AsyncMock(return_value=True)

    async def prepare(db, *, user, body, operation_context):  # noqa: ARG001
        operation_context.bind_scope(
            organization_ids=(prepared.scope.organization_id,),
            library_ids=(prepared.scope.libraries[0].id,),
        )
        return prepared

    async def acquire(prepared, operation_context):  # noqa: ARG001
        operation_context.answer_lease = PublicAnswerLeaseGrant(
            request_id=operation_context.request_id,
            organization_id=operation_context.organization_ids[0],
            api_key_id=None,
            expires_at=NOW + timedelta(seconds=330),
        )
        operation_context.answer_model = "model"

    with (
        _operation_flags(),
        patch.object(settings, "chat_enabled", True),
        patch.object(settings, "chat_base_url", "http://model/v1"),
        patch.object(settings, "chat_model", "model"),
        patch.object(api, "prepare_public_retrieval", side_effect=prepare),
        patch.object(api, "recheck_public_scope", new=AsyncMock()),
        patch.object(api, "acquire_public_answer_capacity", side_effect=acquire),
        patch.object(api.chat_answer, "stream_answer", return_value=provider),
        patch(
            "app.services.public_v1_contracts.record_public_operation",
            new=recorder,
        ),
        patch(
            "app.services.public_v1_contracts.release_public_answer_lease",
            new=releaser,
        ),
    ):
        response = client.post(
            "/api/v1/answers/stream",
            json={"scope": {"library_slugs": ["alpha"]}, "query": "private"},
        )
    assert response.status_code == 200
    releaser.assert_awaited_once_with(response.headers["X-Request-Id"])
    assert recorder.await_count == 1
    record = recorder.await_args.args[0]
    assert record.is_stream is True
    assert record.outcome == expected_outcome
    assert record.error_code == expected_error
    assert record.answer_model == "model"
    assert "private grounded evidence" not in repr(record)
    assert "private provider payload" not in repr(record)
    assert provider.closed is True


def test_no_evidence_stream_records_success_without_answer_lease(operations_client):
    client, _ = operations_client
    prepared = _prepared(with_record=False)
    recorder = AsyncMock(return_value=True)
    releaser = AsyncMock(return_value=True)

    async def prepare(db, *, user, body, operation_context):  # noqa: ARG001
        operation_context.bind_scope(
            organization_ids=(prepared.scope.organization_id,),
            library_ids=(prepared.scope.libraries[0].id,),
        )
        return prepared

    acquire = AsyncMock()
    with (
        _operation_flags(),
        patch.object(api, "prepare_public_retrieval", side_effect=prepare),
        patch.object(api, "recheck_public_scope", new=AsyncMock()),
        patch.object(api, "acquire_public_answer_capacity", new=acquire),
        patch(
            "app.services.public_v1_contracts.record_public_operation",
            new=recorder,
        ),
        patch(
            "app.services.public_v1_contracts.release_public_answer_lease",
            new=releaser,
        ),
    ):
        response = client.post(
            "/api/v1/answers/stream",
            json={"scope": {"library_slugs": ["alpha"]}, "query": "private"},
        )
    assert response.status_code == 200
    assert service.NO_EVIDENCE_ANSWER in response.text
    acquire.assert_not_awaited()
    releaser.assert_not_awaited()
    record = recorder.await_args.args[0]
    assert record.outcome == "completed"
    assert record.answer_model is None


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_kind", ["disconnected", "cancelled"])
async def test_stream_disconnect_and_cancellation_finalize_capacity_once(
    terminal_kind,
):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/answers/stream",
            "headers": [],
        }
    )
    request_id = "0123456789abcdef0123456789abcdef"
    organization_id = uuid.uuid4()
    context = PublicOperationContext(
        request_id=request_id,
        endpoint_key="answers.stream",
        http_method="POST",
        user_id=uuid.uuid4(),
        is_stream=True,
    )
    context.bind_scope(organization_ids=(organization_id,))
    context.answer_lease = PublicAnswerLeaseGrant(
        request_id=request_id,
        organization_id=organization_id,
        api_key_id=None,
        expires_at=NOW + timedelta(seconds=330),
    )
    request.state.public_operation_context = context
    blocker = asyncio.Event()

    async def body():
        yield b"first"
        await blocker.wait()
        yield b"never"

    response = wrap_public_streaming_response(
        request,
        StreamingResponse(body(), status_code=200),
    )
    recorder = AsyncMock(return_value=True)
    releaser = AsyncMock(return_value=True)
    with (
        patch(
            "app.services.public_v1_contracts.record_public_operation",
            new=recorder,
        ),
        patch(
            "app.services.public_v1_contracts.release_public_answer_lease",
            new=releaser,
        ),
    ):
        assert await anext(response.body_iterator) == b"first"
        if terminal_kind == "disconnected":
            await response.body_iterator.aclose()
        else:
            task = asyncio.create_task(anext(response.body_iterator))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    releaser.assert_awaited_once_with(request_id)
    assert recorder.await_count == 1
    assert recorder.await_args.args[0].outcome == terminal_kind
