from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import schema_lifecycle as api
from app.config import settings
from app.main import create_app
from app.schemas.schema_lifecycle import SchemaCloneRequest
from app.services.schema_lifecycle_contracts import SchemaLifecycleError


LIBRARY_ID = uuid.uuid4()
VERSION_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


def _user():
    return SimpleNamespace(id=USER_ID, is_superuser=False)


def _library():
    return SimpleNamespace(
        id=LIBRARY_ID,
        slug="enterprise-kb",
        organization_id=uuid.uuid4(),
        deleted_at=None,
    )


def _context():
    return api.SchemaLifecycleContext(user=_user(), library=_library())


def test_schema_lifecycle_routes_are_mounted_with_exact_methods():
    app = create_app()
    paths = app.openapi()["paths"]
    assert "get" in paths["/libraries/{slug}/schema-lifecycle/versions"]
    assert "post" in paths[
        "/libraries/{slug}/schema-lifecycle/versions/{version_id}/clone"
    ]
    assert "post" in paths[
        "/libraries/{slug}/schema-lifecycle/versions/{version_id}/activate"
    ]
    for kind in ("entity-types", "relation-types", "attributes", "constraints"):
        assert "post" in paths[
            f"/libraries/{{slug}}/schema-lifecycle/versions/{{version_id}}/{kind}"
        ]
        assert "patch" in paths[
            f"/libraries/{{slug}}/schema-lifecycle/versions/{{version_id}}/{kind}/{{item_id}}"
        ]


def test_disabled_context_fails_before_library_or_authorization_queries():
    db = AsyncMock()
    with patch.object(settings, "schema_lifecycle_enabled", False):
        with pytest.raises(HTTPException) as error:
            asyncio.run(api._load_context("enterprise-kb", _user(), db))
    assert error.value.status_code == 404
    db.execute.assert_not_awaited()


def test_context_uses_shared_management_authority_and_hides_failures():
    db = AsyncMock()
    library = _library()
    with (
        patch.object(settings, "schema_lifecycle_enabled", True),
        patch.object(api, "load_active_library", AsyncMock(return_value=library)),
        patch.object(
            api,
            "resolve_loaded_library_management",
            AsyncMock(return_value=SimpleNamespace(library=library)),
        ) as authorize,
    ):
        context = asyncio.run(api._load_context(library.slug, _user(), db))
    assert context.library is library
    authorize.assert_awaited_once_with(db, user=context.user, library=library)

    with (
        patch.object(settings, "schema_lifecycle_enabled", True),
        patch.object(api, "load_active_library", AsyncMock(return_value=None)),
    ):
        with pytest.raises(HTTPException) as missing:
            asyncio.run(api._load_context("missing", _user(), db))
    assert missing.value.status_code == 403
    assert missing.value.detail == "forbidden"


def test_clone_endpoint_owns_commit_and_maps_conflicts_without_raw_details():
    db = AsyncMock()
    body = SchemaCloneRequest(
        expected_version_state_hash="a" * 64,
        idempotency_key="clone-1",
    )
    service_result = SimpleNamespace(
        action=SimpleNamespace(id=uuid.uuid4()),
        reused=False,
        bundle=SimpleNamespace(),
    )
    response = SimpleNamespace(
        action_id=service_result.action.id,
        reused=False,
        version=SimpleNamespace(),
    )
    with (
        patch.object(api, "clone_schema_version", AsyncMock(return_value=service_result)),
        patch.object(api, "_command_response", return_value=response),
    ):
        result = asyncio.run(
            api.clone_version(VERSION_ID, body, _context(), db)
        )
    assert result.action_id == service_result.action.id
    assert result is response
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()

    db.reset_mock()
    with patch.object(
        api,
        "clone_schema_version",
        AsyncMock(
            side_effect=SchemaLifecycleError(
                "schema_lifecycle_state_changed", "sensitive stale value"
            )
        ),
    ):
        with pytest.raises(HTTPException) as conflict:
            asyncio.run(api.clone_version(VERSION_ID, body, _context(), db))
    assert conflict.value.status_code == 409
    assert conflict.value.detail == "schema_lifecycle_state_changed"
    assert "sensitive" not in str(conflict.value.detail)
    db.rollback.assert_awaited_once()
