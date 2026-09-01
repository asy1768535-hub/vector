from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import graph_catalog as api
from app.auth.backend import current_active_user
from app.config import Settings, settings, validate_graph_catalog_startup
from app.db import get_db
from app.main import app
from app.models.user import User
from app.schemas.graph_catalog import (
    GraphCatalogEntityPageRead,
    GraphCatalogSearchRequest,
    GraphRelationCatalogSearchRequest,
)
from app.services.graph_catalog_contracts import (
    GraphCatalogError,
    GraphCatalogScopeError,
    GraphCatalogScopeIncompatibility,
)


ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000931")


def _user() -> User:
    return User(id=uuid.uuid4(), email="reader@example.com", hashed_password="x")


def test_graph_catalog_defaults_off_and_requires_security_dependencies():
    assert settings.graph_catalog_enabled is False
    validate_graph_catalog_startup(Settings(_env_file=None))
    with pytest.raises(RuntimeError, match="graph Catalog requires"):
        validate_graph_catalog_startup(Settings(_env_file=None, graph_catalog_enabled=True))
    validate_graph_catalog_startup(
        Settings(
            _env_file=None,
            graph_catalog_enabled=True,
            organization_authorization_enabled=True,
            cross_library_compatibility_enabled=True,
        )
    )


def test_disabled_route_dependency_is_hidden():
    with patch.object(settings, "graph_catalog_enabled", False):
        with pytest.raises(HTTPException) as exc:
            api.require_graph_catalog_enabled()
    assert exc.value.status_code == 404
    assert exc.value.detail == "not found"


def test_graph_catalog_routes_are_mounted_and_default_off():
    app.dependency_overrides[current_active_user] = _user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch.object(settings, "graph_catalog_enabled", False):
            response = TestClient(app).post(
                f"/organizations/{ORGANIZATION_ID}/graph-catalog/entities:search",
                json={"library_slugs": ["alpha"]},
            )
        assert response.status_code == 404
        paths = set(app.openapi()["paths"])
        assert (
            "/organizations/{organization_id}/graph-catalog/entities:search" in paths
        )
        assert (
            "/organizations/{organization_id}/graph-catalog/relations:search" in paths
        )
        assert (
            "/organizations/{organization_id}/graph-catalog/libraries/{library_slug}/"
            "entities/{entity_id}" in paths
        )
        assert (
            "/organizations/{organization_id}/graph-catalog/libraries/{library_slug}/"
            "relations/{relation_id}" in paths
        )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_entity_search_maps_request_to_service_contract():
    response = GraphCatalogEntityPageRead(items=[], next_cursor=None)
    db = SimpleNamespace(rollback=AsyncMock())
    body = GraphCatalogSearchRequest(
        library_slugs=["alpha"],
        query="Acme",
        statuses=["active"],
        limit=25,
    )
    with patch(
        "app.api.graph_catalog.search_graph_catalog_entities",
        new=AsyncMock(return_value=response),
    ) as search:
        actual = await api.search_entities(
            ORGANIZATION_ID,
            body,
            None,
            _user(),
            db,
        )
    assert actual is response
    query = search.await_args.kwargs["query"]
    assert query.selection.organization_id == ORGANIZATION_ID
    assert query.selection.library_slugs == ("alpha",)
    assert query.query_text == "Acme"
    assert query.statuses == ("active",)
    assert query.limit == 25


@pytest.mark.asyncio
async def test_relation_search_maps_incompatibility_to_bounded_409():
    db = SimpleNamespace(rollback=AsyncMock())
    body = GraphRelationCatalogSearchRequest(library_slugs=["alpha", "beta"])
    error = GraphCatalogScopeError(
        "graph_catalog_scope_incompatible",
        incompatibilities=(
            GraphCatalogScopeIncompatibility("beta", ("graph_profile_mismatch",)),
        ),
    )
    with patch(
        "app.api.graph_catalog.search_graph_catalog_relations",
        new=AsyncMock(side_effect=error),
    ):
        with pytest.raises(HTTPException) as exc:
            await api.search_relations(ORGANIZATION_ID, body, None, _user(), db)
    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "code": "graph_catalog_scope_incompatible",
        "incompatibilities": [
            {
                "library_slug": "beta",
                "reason_codes": ["graph_profile_mismatch"],
            }
        ],
    }
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_detail_not_found_does_not_expose_hidden_identity():
    db = SimpleNamespace(rollback=AsyncMock())
    with patch(
        "app.api.graph_catalog.get_graph_catalog_entity_detail",
        new=AsyncMock(side_effect=GraphCatalogError("graph_catalog_not_found")),
    ):
        with pytest.raises(HTTPException) as exc:
            await api.entity_detail(
                ORGANIZATION_ID,
                "hidden-library",
                uuid.uuid4(),
                None,
                _user(),
                db,
            )
    assert exc.value.status_code == 404
    assert exc.value.detail == "not found"
