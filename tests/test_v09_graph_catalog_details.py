from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.user import User
from app.services import graph_catalog_details as service
from app.services.graph_catalog_contracts import (
    GraphCatalogError,
    GraphCatalogResolvedScope,
)


ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000921")


def _user() -> User:
    return User(id=uuid.uuid4(), email="reader@example.com", hashed_password="x")


def _library() -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=ORGANIZATION_ID,
        slug="alpha",
        name="Alpha",
        qdrant_collection="c_alpha",
        embedding_model="model",
        embedding_dim=8,
        chunk_size=1000,
        chunk_overlap=120,
        lifecycle_mode="managed",
        index_state="ready",
    )


def _entity_core(library: Library, entity_id: uuid.UUID):
    now = datetime.now(timezone.utc)
    entity = Entity(
        id=entity_id,
        library_id=library.id,
        ontology_version_id=uuid.uuid4(),
        entity_type_id=uuid.uuid4(),
        canonical_name="Acme",
        normalized_name="acme",
        properties={"country": "CN"},
        status="pending_review",
        source_type="extracted",
        created_at=now,
        updated_at=now,
    )
    return SimpleNamespace(
        Entity=entity,
        type_key="company",
        type_label="Company",
        publication_id=None,
        publication_status=None,
        publication_item_hash=None,
        evidence_count=0,
        document_count=0,
    )


def _relation_core(library: Library, relation_id: uuid.UUID):
    now = datetime.now(timezone.utc)
    relation = KnowledgeRelation(
        id=relation_id,
        library_id=library.id,
        ontology_version_id=uuid.uuid4(),
        relation_type_id=uuid.uuid4(),
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        properties={"percent": 10},
        status="active",
        review_status="approved",
        source_type="manual",
        created_at=now,
        updated_at=now,
    )
    return SimpleNamespace(
        KnowledgeRelation=relation,
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
        publication_item_hash="b" * 64,
        evidence_count=0,
        document_count=0,
    )


def _result(*, first=None, scalar=None, rows=None):
    return SimpleNamespace(
        first=lambda: first,
        scalar_one=lambda: scalar,
        all=lambda: rows or [],
    )


@pytest.mark.asyncio
async def test_entity_detail_composes_staged_fact_without_source_text():
    library = _library()
    entity_id = uuid.uuid4()
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _result(first=_entity_core(library, entity_id)),
                _result(scalar=0),
                _result(rows=[]),
            ]
        )
    )
    with (
        patch(
            "app.services.graph_catalog_details.resolve_graph_catalog_scope",
            new=AsyncMock(
                return_value=GraphCatalogResolvedScope(ORGANIZATION_ID, (library,))
            ),
        ),
        patch(
            "app.services.graph_catalog_details._entity_aliases",
            new=AsyncMock(return_value=([], 0)),
        ),
        patch(
            "app.services.graph_catalog_details._entity_evidence",
            new=AsyncMock(return_value=([], 0)),
        ),
        patch(
            "app.services.graph_catalog_details._documents",
            new=AsyncMock(return_value=([], 0)),
        ),
        patch(
            "app.services.graph_catalog_details._job",
            new=AsyncMock(return_value=None),
        ),
    ):
        detail = await service.get_graph_catalog_entity_detail(
            db,
            user=_user(),
            organization_id=ORGANIZATION_ID,
            library_slug="alpha",
            entity_id=entity_id,
        )
    assert detail.entity.publication_state == "staged"
    assert detail.properties == {"country": "CN"}
    assert detail.evidence == []
    assert "text_quote" not in detail.model_dump()


@pytest.mark.asyncio
async def test_relation_detail_composes_published_fact():
    library = _library()
    relation_id = uuid.uuid4()
    db = SimpleNamespace(execute=AsyncMock(return_value=_result(first=_relation_core(library, relation_id))))
    with (
        patch(
            "app.services.graph_catalog_details.resolve_graph_catalog_scope",
            new=AsyncMock(
                return_value=GraphCatalogResolvedScope(ORGANIZATION_ID, (library,))
            ),
        ),
        patch(
            "app.services.graph_catalog_details._relation_evidence",
            new=AsyncMock(return_value=([], 0)),
        ),
        patch(
            "app.services.graph_catalog_details._documents",
            new=AsyncMock(return_value=([], 0)),
        ),
        patch(
            "app.services.graph_catalog_details._job",
            new=AsyncMock(return_value=None),
        ),
    ):
        detail = await service.get_graph_catalog_relation_detail(
            db,
            user=_user(),
            organization_id=ORGANIZATION_ID,
            library_slug="alpha",
            relation_id=relation_id,
        )
    assert detail.relation.publication_state == "published"
    assert detail.relation.source.canonical_name == "Acme"
    assert detail.properties == {"percent": 10}


@pytest.mark.asyncio
async def test_entity_alias_projection_repeats_governance_state_hash():
    library = _library()
    alias = EntityAlias(
        id=uuid.uuid4(),
        library_id=library.id,
        entity_id=uuid.uuid4(),
        alias="ACME",
        normalized_alias="acme",
        source_type="manual",
        status="active",
    )
    rows = SimpleNamespace(all=lambda: [alias])
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one=lambda: 1),
                SimpleNamespace(scalars=lambda: rows),
            ]
        )
    )
    projected, total = await service._entity_aliases(db, library, alias.entity_id)
    assert total == 1
    assert projected[0].governance_state_hash == service.alias_governance_state_hash(alias)


@pytest.mark.asyncio
async def test_missing_entity_is_hidden_as_catalog_not_found():
    library = _library()
    db = SimpleNamespace(execute=AsyncMock(return_value=_result(first=None)))
    with patch(
        "app.services.graph_catalog_details.resolve_graph_catalog_scope",
        new=AsyncMock(return_value=GraphCatalogResolvedScope(ORGANIZATION_ID, (library,))),
    ):
        with pytest.raises(GraphCatalogError) as exc:
            await service.get_graph_catalog_entity_detail(
                db,
                user=_user(),
                organization_id=ORGANIZATION_ID,
                library_slug="alpha",
                entity_id=uuid.uuid4(),
            )
    assert exc.value.code == "graph_catalog_not_found"
