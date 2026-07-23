from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import graph_governance as api
from app.config import settings
from app.models.entity_type import EntityType
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.models.user import User
from app.services.graph_governance_context import (
    MAX_TYPE_OPTIONS_PER_ONTOLOGY,
    load_graph_governance_write_context,
)
from app.services.graph_governance_contracts import GraphGovernanceError
from app.services.organization_authorization import OrganizationAuthorizationError


NOW = datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _DB:
    def __init__(self, *results):
        self.results = list(results)

    async def execute(self, _statement):
        assert self.results, "unexpected query"
        return self.results.pop(0)


def _library() -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        slug="graph-console",
        name="Graph Console",
        embedding_model="model",
        embedding_dim=8,
        qdrant_collection="graph_console",
        created_at=NOW,
    )


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email="graph-console@example.com",
        hashed_password="hash",
        is_active=True,
    )


def _ontology(library: Library, *, key: str, number: int) -> OntologyVersion:
    return OntologyVersion(
        id=uuid.uuid4(),
        library_id=library.id,
        version_key=key,
        version_no=number,
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def test_write_context_is_active_bounded_ordered_and_read_only():
    library = _library()
    first = _ontology(library, key="default", number=1)
    second = _ontology(library, key="legal", number=2)
    entity_type = EntityType(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=first.id,
        key="company",
        label="Company",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    relation_type = RelationType(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=first.id,
        key="invests_in",
        label="Invests in",
        direction="directed",
        default_review_policy="pending_review",
        requires_evidence=True,
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    db = _DB(
        _Result((first, second)),
        _Result((entity_type,)),
        _Result((relation_type,)),
    )

    result = asyncio.run(load_graph_governance_write_context(db, library=library))

    assert result.contract_version == "graph-governance-context-v1"
    assert result.library_id == library.id and result.library_slug == library.slug
    assert [value.id for value in result.ontology_versions] == [first.id, second.id]
    assert result.ontology_versions[0].entity_types[0].key == "company"
    relation = result.ontology_versions[0].relation_types[0]
    assert (relation.key, relation.direction, relation.requires_evidence) == (
        "invests_in",
        "directed",
        True,
    )
    assert not hasattr(db, "commit")


def test_write_context_fails_closed_when_one_ontology_exceeds_type_bound():
    library = _library()
    ontology = _ontology(library, key="default", number=1)
    entity_types = tuple(
        EntityType(
            id=uuid.uuid4(),
            library_id=library.id,
            ontology_version_id=ontology.id,
            key=f"type_{index:03d}",
            label=f"Type {index}",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
        for index in range(MAX_TYPE_OPTIONS_PER_ONTOLOGY + 1)
    )
    db = _DB(_Result((ontology,)), _Result(entity_types), _Result())

    with pytest.raises(GraphGovernanceError) as exc_info:
        asyncio.run(load_graph_governance_write_context(db, library=library))

    assert exc_info.value.code == "graph_governance_unavailable"


@pytest.mark.asyncio
async def test_writer_dependency_accepts_insert_or_management_and_is_cookie_only():
    library, user = _library(), _user()
    db = AsyncMock()
    denial = OrganizationAuthorizationError("organization_forbidden")
    with (
        patch.object(settings, "graph_governance_enabled", True),
        patch.object(
            api.deps_module,
            "load_active_library",
            new=AsyncMock(return_value=library),
        ),
        patch.object(
            api,
            "resolve_loaded_library_access",
            new=AsyncMock(side_effect=denial),
        ) as insert_access,
        patch.object(
            api,
            "resolve_loaded_library_management",
            new=AsyncMock(),
        ) as management_access,
    ):
        result = await api.require_governance_writer(library.slug, user, db)

    assert result.library is library and result.user is user
    assert insert_access.await_args.kwargs["action"] == "insert"
    management_access.assert_awaited_once()
    assert "current_cookie_user" in inspect.getsource(api.require_governance_writer)


@pytest.mark.asyncio
async def test_writer_dependency_denies_when_insert_and_management_fail():
    library, user = _library(), _user()
    denial = OrganizationAuthorizationError("organization_forbidden")
    with (
        patch.object(settings, "graph_governance_enabled", True),
        patch.object(
            api.deps_module,
            "load_active_library",
            new=AsyncMock(return_value=library),
        ),
        patch.object(
            api,
            "resolve_loaded_library_access",
            new=AsyncMock(side_effect=denial),
        ),
        patch.object(
            api,
            "resolve_loaded_library_management",
            new=AsyncMock(side_effect=denial),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await api.require_governance_writer(library.slug, user, AsyncMock())

    assert exc_info.value.status_code == 403 and exc_info.value.detail == "forbidden"


def test_context_route_is_mounted_and_default_off_before_library_lookup():
    paths = {route.path for route in api.router.routes}
    assert "/libraries/{slug}/graph-governance/context" in paths
    db = AsyncMock()
    with patch.object(settings, "graph_governance_enabled", False):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(api._load_context("hidden", _user(), db))
    assert exc_info.value.status_code == 404
    db.execute.assert_not_awaited()
