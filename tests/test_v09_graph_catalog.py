from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.library import Library
from app.models.entity import Entity
from app.models.knowledge_relation import KnowledgeRelation
from app.models.user import User
from app.schemas.graph_catalog import (
    GraphCatalogEntityPageRead,
    GraphCatalogRelationPageRead,
)
from app.services import graph_catalog as service
from app.services.graph_catalog_contracts import (
    GraphCatalogError,
    GraphCatalogResolvedScope,
    GraphCatalogScopeError,
    GraphCatalogSelection,
    GraphEntityCatalogQuery,
    GraphRelationCatalogQuery,
)
from app.services.graph_catalog_scope import resolve_graph_catalog_scope


ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000911")
OTHER_ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000912")
SCOPE_ID = uuid.UUID("00000000-0000-0000-0000-000000000913")


def _user() -> User:
    return User(id=uuid.uuid4(), email="reader@example.com", hashed_password="x")


def _library(slug: str, *, organization_id: uuid.UUID = ORGANIZATION_ID) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization_id,
        slug=slug,
        name=slug.title(),
        qdrant_collection=f"c_{slug}",
        embedding_model="model",
        embedding_dim=8,
        chunk_size=1000,
        chunk_overlap=120,
        lifecycle_mode="managed",
        index_state="ready",
    )


@pytest.mark.asyncio
async def test_explicit_single_library_skips_graph_compatibility():
    user = _user()
    library = _library("alpha")
    access = SimpleNamespace(library=library)
    selection = GraphCatalogSelection(ORGANIZATION_ID, library_slugs=("alpha",))
    with (
        patch(
            "app.services.graph_catalog_scope.resolve_organization_membership",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_catalog_scope.resolve_library_selection",
            new=AsyncMock(return_value=(access,)),
        ),
        patch(
            "app.services.graph_catalog_scope.assess_library_compatibility",
            new=AsyncMock(),
        ) as assess,
    ):
        resolved = await resolve_graph_catalog_scope(object(), user=user, selection=selection)
    assert resolved.libraries == (library,)
    assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_library_scope_requires_graph_compatibility():
    user = _user()
    alpha, beta = _library("alpha"), _library("beta")
    selection = GraphCatalogSelection(
        ORGANIZATION_ID,
        library_slugs=("alpha", "beta"),
    )
    assessment = SimpleNamespace(
        organization_id=ORGANIZATION_ID,
        profiles=(SimpleNamespace(library=alpha), SimpleNamespace(library=beta)),
        compatible=False,
        incompatibilities=(
            SimpleNamespace(library_slug="beta", reason_codes=("graph_profile_mismatch",)),
        ),
    )
    with (
        patch(
            "app.services.graph_catalog_scope.resolve_organization_membership",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_catalog_scope.resolve_library_selection",
            new=AsyncMock(
                return_value=(SimpleNamespace(library=alpha), SimpleNamespace(library=beta))
            ),
        ),
        patch(
            "app.services.graph_catalog_scope.assess_library_compatibility",
            new=AsyncMock(return_value=assessment),
        ),
    ):
        with pytest.raises(GraphCatalogScopeError) as exc:
            await resolve_graph_catalog_scope(object(), user=user, selection=selection)
    assert exc.value.code == "graph_catalog_scope_incompatible"
    assert exc.value.incompatibilities[0].library_slug == "beta"


@pytest.mark.asyncio
async def test_named_scope_fails_whole_selection_when_one_item_is_unavailable():
    user = _user()
    alpha = _library("alpha")
    missing_id = uuid.uuid4()
    selection = GraphCatalogSelection(ORGANIZATION_ID, scope_id=SCOPE_ID)
    with (
        patch(
            "app.services.graph_catalog_scope.resolve_organization_membership",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_catalog_scope.read_named_scope_library_ids",
            new=AsyncMock(return_value=(alpha.id, missing_id)),
        ),
        patch(
            "app.services.graph_catalog_scope.list_accessible_libraries",
            new=AsyncMock(return_value=(alpha,)),
        ),
    ):
        with pytest.raises(GraphCatalogScopeError) as exc:
            await resolve_graph_catalog_scope(object(), user=user, selection=selection)
    assert exc.value.code == "graph_catalog_scope_forbidden"


@pytest.mark.asyncio
async def test_credential_organization_binding_fails_before_database_access():
    user = _user()
    selection = GraphCatalogSelection(ORGANIZATION_ID, library_slugs=("alpha",))
    with (
        patch(
            "app.services.graph_catalog_scope.credential_organization_scope",
            return_value=SimpleNamespace(organization_id=OTHER_ORGANIZATION_ID),
        ),
        patch(
            "app.services.graph_catalog_scope.resolve_organization_membership",
            new=AsyncMock(),
        ) as membership,
    ):
        with pytest.raises(GraphCatalogScopeError) as exc:
            await resolve_graph_catalog_scope(object(), user=user, selection=selection)
    assert exc.value.code == "graph_catalog_scope_forbidden"
    membership.assert_not_awaited()


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _DB:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.rows)


def _entity_row(library: Library, name: str, entity_id: uuid.UUID):
    now = datetime.now(timezone.utc)
    entity = Entity(
        id=entity_id,
        library_id=library.id,
        ontology_version_id=uuid.uuid4(),
        entity_type_id=uuid.uuid4(),
        canonical_name=name,
        normalized_name=name.casefold(),
        status="active",
        source_type="manual",
        created_at=now,
        updated_at=now,
    )
    return SimpleNamespace(
        Entity=entity,
        library_slug=library.slug,
        library_name=library.name,
        type_key="company",
        type_label="Company",
        publication_id=None,
        publication_status=None,
        publication_item_hash=None,
        evidence_count=2,
        document_count=1,
    )


def _relation_row(library: Library, relation_id: uuid.UUID):
    now = datetime.now(timezone.utc)
    relation = KnowledgeRelation(
        id=relation_id,
        library_id=library.id,
        ontology_version_id=uuid.uuid4(),
        relation_type_id=uuid.uuid4(),
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        status="pending_review",
        review_status="pending_review",
        source_type="extracted",
        created_at=now,
        updated_at=now,
    )
    return SimpleNamespace(
        KnowledgeRelation=relation,
        library_slug=library.slug,
        library_name=library.name,
        type_key="invested_in",
        type_label="Invested In",
        direction="directed",
        source_id=relation.source_entity_id,
        source_name="Acme",
        source_normalized_name="acme",
        source_type_id=uuid.uuid4(),
        source_type_key="company",
        source_type_label="Company",
        target_id=relation.target_entity_id,
        target_name="Beta",
        target_normalized_name="beta",
        target_type_id=uuid.uuid4(),
        target_type_key="company",
        target_type_label="Company",
        publication_id=uuid.uuid4(),
        publication_status="active",
        publication_item_hash="a" * 64,
        evidence_count=1,
        document_count=1,
    )


@pytest.mark.asyncio
async def test_entity_search_keeps_same_names_separate_and_emits_cursor():
    user = _user()
    alpha, beta = _library("alpha"), _library("beta")
    rows = [
        _entity_row(alpha, "Acme", uuid.UUID(int=1)),
        _entity_row(beta, "Acme", uuid.UUID(int=2)),
    ]
    db = _DB(rows)
    query = GraphEntityCatalogQuery(
        GraphCatalogSelection(ORGANIZATION_ID, library_slugs=("alpha", "beta")),
        limit=1,
    )
    scope = GraphCatalogResolvedScope(ORGANIZATION_ID, (alpha, beta))
    with patch(
        "app.services.graph_catalog.resolve_graph_catalog_scope",
        new=AsyncMock(return_value=scope),
    ):
        response = await service.search_graph_catalog_entities(db, user=user, query=query)
    assert isinstance(response, GraphCatalogEntityPageRead)
    assert [item.library.slug for item in response.items] == ["alpha"]
    assert response.next_cursor is not None
    assert "ORDER BY entities.normalized_name" in str(db.statements[0])


@pytest.mark.asyncio
async def test_relation_search_projects_publication_endpoints_and_counts():
    user = _user()
    alpha = _library("alpha")
    db = _DB([_relation_row(alpha, uuid.UUID(int=4))])
    query = GraphRelationCatalogQuery(
        GraphCatalogSelection(ORGANIZATION_ID, library_slugs=("alpha",)),
    )
    scope = GraphCatalogResolvedScope(ORGANIZATION_ID, (alpha,))
    with patch(
        "app.services.graph_catalog.resolve_graph_catalog_scope",
        new=AsyncMock(return_value=scope),
    ):
        response = await service.search_graph_catalog_relations(db, user=user, query=query)
    assert isinstance(response, GraphCatalogRelationPageRead)
    assert response.items[0].publication_state == "published"
    assert response.items[0].source.canonical_name == "Acme"
    assert response.items[0].counts.evidence == 1


@pytest.mark.asyncio
async def test_invalid_database_projection_maps_to_catalog_unavailable():
    user = _user()
    alpha = _library("alpha")
    row = _entity_row(alpha, "Acme", uuid.UUID(int=9))
    row.Entity.canonical_name = ""
    query = GraphEntityCatalogQuery(
        GraphCatalogSelection(ORGANIZATION_ID, library_slugs=("alpha",)),
    )
    scope = GraphCatalogResolvedScope(ORGANIZATION_ID, (alpha,))
    with patch(
        "app.services.graph_catalog.resolve_graph_catalog_scope",
        new=AsyncMock(return_value=scope),
    ):
        with pytest.raises(GraphCatalogError) as exc:
            await service.search_graph_catalog_entities(_DB([row]), user=user, query=query)
    assert exc.value.code == "graph_catalog_unavailable"
