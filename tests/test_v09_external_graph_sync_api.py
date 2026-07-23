from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import external_graph_sync as api
from app.config import settings
from app.models.library import Library
from app.schemas.external_graph_sync import (
    ExternalGraphSyncBatchRequest,
    ExternalGraphSyncBatchResponse,
)


def _library() -> Library:
    return Library(
        id=uuid.uuid4(),
        slug="alpha",
        name="Alpha",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="alpha",
    )


def _delete() -> ExternalGraphSyncBatchRequest:
    return ExternalGraphSyncBatchRequest(
        idempotency_key="delete-1",
        items=[
            {
                "action": "delete",
                "fact_kind": "entity",
                "external_type": "employee",
                "external_id": "E-1",
            }
        ],
    )


@pytest.mark.asyncio
async def test_context_is_default_off_before_library_lookup(monkeypatch) -> None:
    monkeypatch.setattr(settings, "external_graph_sync_enabled", False)
    db = SimpleNamespace()
    with pytest.raises(HTTPException) as caught:
        await api._context("alpha", SimpleNamespace(), db)
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_batch_requires_delete_permission(monkeypatch) -> None:
    context = api.ExternalGraphSyncContext(
        SimpleNamespace(id=uuid.uuid4()),
        _library(),
    )
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    with (
        patch.object(
            api,
            "resolve_loaded_library_access",
            new=AsyncMock(side_effect=api.OrganizationAuthorizationError("forbidden")),
        ),
        patch.object(api, "sync_batch", new=AsyncMock()) as sync,
    ):
        with pytest.raises(HTTPException) as caught:
            await api.run_batch("hr", _delete(), context, db)
    assert caught.value.status_code == 403
    sync.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_commits_sanitized_service_result() -> None:
    actor_id = uuid.uuid4()
    context = api.ExternalGraphSyncContext(
        SimpleNamespace(id=actor_id),
        _library(),
    )
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    expected = ExternalGraphSyncBatchResponse(
        operation_id=uuid.uuid4(),
        replayed=False,
        status="applied",
        created_count=0,
        updated_count=0,
        unchanged_count=0,
        deleted_count=1,
        conflict_count=0,
        stale_count=0,
        items=[],
    )
    with (
        patch.object(
            api,
            "resolve_loaded_library_access",
            new=AsyncMock(),
        ),
        patch.object(api, "sync_batch", new=AsyncMock(return_value=expected)) as sync,
    ):
        result = await api.run_batch("hr", _delete(), context, db)
    assert result == expected
    sync.assert_awaited_once()
    assert sync.await_args.kwargs["actor_id"] == actor_id
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


def test_router_exposes_only_bounded_sync_and_read_routes() -> None:
    paths: dict[str, set[str]] = {}
    for route in api.router.routes:
        paths.setdefault(route.path, set()).update(route.methods)
    assert paths == {
        "/libraries/{slug}/external-graph-sync/sources/{source_key}/policy": {
            "GET",
            "PUT",
        },
        "/libraries/{slug}/external-graph-sync/sources/{source_key}/batches": {
            "POST"
        },
        "/libraries/{slug}/external-graph-sync/sources/{source_key}/mappings": {
            "GET"
        },
        "/libraries/{slug}/external-graph-sync/conflicts": {"GET"},
        "/libraries/{slug}/external-graph-sync/conflicts/{conflict_id}": {
            "PATCH"
        },
    }
