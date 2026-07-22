from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.graph_catalog import (
    GraphCatalogEntityDetailRead,
    GraphCatalogEntityListItemRead,
    GraphCatalogEvidenceCountsRead,
    GraphCatalogLibraryRead,
    GraphCatalogRelatedDocumentRead,
    GraphCatalogSearchRequest,
    GraphCatalogTypeRead,
    GraphRelationCatalogSearchRequest,
)
from app.services.graph_catalog_contracts import (
    GraphCatalogError,
    GraphCatalogSelection,
    GraphEntityCatalogCursor,
    GraphEntityCatalogQuery,
    GraphRelationCatalogCursor,
    GraphRelationCatalogQuery,
    decode_entity_cursor,
    decode_relation_cursor,
    encode_entity_cursor,
    encode_relation_cursor,
    graph_catalog_filter_fingerprint,
)


ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000901")
LIBRARY_ID = uuid.UUID("00000000-0000-0000-0000-000000000902")
ENTITY_ID = uuid.UUID("00000000-0000-0000-0000-000000000903")
RELATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000904")
SCOPE_ID = uuid.UUID("00000000-0000-0000-0000-000000000905")


def test_search_request_requires_exactly_one_strict_scope():
    with pytest.raises(ValidationError):
        GraphCatalogSearchRequest()
    with pytest.raises(ValidationError):
        GraphCatalogSearchRequest(library_slugs=["alpha"], scope_id=SCOPE_ID)
    with pytest.raises(ValidationError):
        GraphCatalogSearchRequest(library_slugs=["alpha", "alpha"])
    with pytest.raises(ValidationError):
        GraphCatalogSearchRequest(library_slugs=["alpha"], extra_field=True)

    request = GraphCatalogSearchRequest(library_slugs=["alpha"], query="  Acme  Corp ")
    assert request.library_slugs == ["alpha"]
    assert request.query == "  Acme  Corp "


def test_relation_request_rejects_duplicate_filters():
    with pytest.raises(ValidationError):
        GraphRelationCatalogSearchRequest(
            scope_id=SCOPE_ID,
            review_statuses=["approved", "approved"],
        )


def test_detail_properties_are_bounded_and_document_title_may_be_absent():
    document = GraphCatalogRelatedDocumentRead(
        document_id=uuid.uuid4(),
        document_revision_id=uuid.uuid4(),
        title=None,
        evidence_count=1,
    )
    assert document.title is None

    entity = GraphCatalogEntityListItemRead(
        id=ENTITY_ID,
        library=GraphCatalogLibraryRead(id=LIBRARY_ID, slug="alpha", name="Alpha"),
        ontology_version_id=uuid.uuid4(),
        entity_type=GraphCatalogTypeRead(id=uuid.uuid4(), key="company", label="Company"),
        canonical_name="Acme",
        normalized_name="acme",
        status="active",
        source_type="manual",
        publication_state="staged",
        publication=None,
        counts=GraphCatalogEvidenceCountsRead(evidence=0, documents=0),
        created_at="2026-07-22T00:00:00Z",
        updated_at="2026-07-22T00:00:00Z",
    )
    with pytest.raises(ValidationError):
        GraphCatalogEntityDetailRead(
            entity=entity,
            properties={"payload": "x" * 65_537},
            aliases=[],
            alias_count=0,
            aliases_truncated=False,
            evidence=[],
            evidence_count=0,
            evidence_truncated=False,
            documents=[],
            document_count=0,
            documents_truncated=False,
            related_relations=[],
            relation_count=0,
            relations_truncated=False,
        )


def test_selection_and_queries_normalize_and_validate():
    selection = GraphCatalogSelection(
        organization_id=ORGANIZATION_ID,
        library_slugs=("alpha",),
    )
    entity = GraphEntityCatalogQuery(
        selection=selection,
        query_text="  ＡＣＭＥ   Corp  ",
        type_keys=("company",),
        statuses=("active",),
        source_types=("manual",),
        limit=25,
    )
    relation = GraphRelationCatalogQuery(
        selection=selection,
        query_text=" Invested  In ",
        type_keys=("invested_in",),
        review_statuses=("approved",),
    )
    assert entity.query_text == "ACME Corp"
    assert relation.query_text == "Invested In"

    with pytest.raises(GraphCatalogError, match="Graph Catalog") as exc:
        GraphCatalogSelection(organization_id=ORGANIZATION_ID)
    assert exc.value.code == "graph_catalog_scope_invalid"

    with pytest.raises(GraphCatalogError) as exc:
        GraphEntityCatalogQuery(selection=selection, statuses=("deleted",))
    assert exc.value.code == "graph_catalog_filter_invalid"


def test_entity_cursor_is_canonical_and_filter_bound():
    selection = GraphCatalogSelection(ORGANIZATION_ID, library_slugs=("alpha",))
    query = GraphEntityCatalogQuery(selection=selection, query_text="Acme")
    fingerprint = graph_catalog_filter_fingerprint(
        kind="entity",
        organization_id=ORGANIZATION_ID,
        library_ids=(LIBRARY_ID,),
        filters=query.filter_payload(),
    )
    cursor = GraphEntityCatalogCursor("acme", "alpha", ENTITY_ID, fingerprint)
    encoded = encode_entity_cursor(cursor)
    assert decode_entity_cursor(encoded, expected_filter_fingerprint=fingerprint) == cursor

    with pytest.raises(GraphCatalogError) as exc:
        decode_entity_cursor(encoded + "=", expected_filter_fingerprint=fingerprint)
    assert exc.value.code == "graph_catalog_cursor_invalid"
    with pytest.raises(GraphCatalogError):
        decode_entity_cursor(encoded, expected_filter_fingerprint="0" * 64)


def test_relation_cursor_is_canonical_and_filter_bound():
    selection = GraphCatalogSelection(ORGANIZATION_ID, scope_id=SCOPE_ID)
    query = GraphRelationCatalogQuery(selection=selection, type_keys=("invested_in",))
    fingerprint = graph_catalog_filter_fingerprint(
        kind="relation",
        organization_id=ORGANIZATION_ID,
        library_ids=(LIBRARY_ID,),
        filters=query.filter_payload(),
    )
    cursor = GraphRelationCatalogCursor(
        "invested_in",
        "acme",
        "beta",
        "alpha",
        RELATION_ID,
        fingerprint,
    )
    encoded = encode_relation_cursor(cursor)
    assert decode_relation_cursor(encoded, expected_filter_fingerprint=fingerprint) == cursor
    with pytest.raises(GraphCatalogError):
        decode_relation_cursor("not-json", expected_filter_fingerprint=fingerprint)
