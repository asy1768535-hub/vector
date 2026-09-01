from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.models.library import Library
from app.models.user import User
from app.schemas.dify import DifyRecord
from app.schemas.graph_catalog import (
    GraphCatalogEntityDetailRead,
    GraphCatalogEntityPageRead,
    GraphCatalogRelationDetailRead,
    GraphCatalogRelationPageRead,
)
from app.schemas.knowledge_catalog import (
    CatalogDocumentDetailRead,
    CatalogEvidenceDetailRead,
)
from app.schemas.public_v1 import (
    PublicAnswerRequest,
    PublicChunkRead,
    PublicGraphRead,
    PublicEntitySearchRequest,
    PublicRelationSearchRequest,
    PublicRetrievalRequest,
    PublicScopeSelection,
    PublicSourceRead,
)
from app.services import public_v1 as service
from app.services.chat_answer import ChatAnswer
from app.services.federated_retrieval_contracts import (
    FederatedHit,
    FederatedRetrievalResult,
    FederatedSourceProjection,
)
from app.services.library_compatibility_contracts import (
    CompatibilityAssessment,
    CompatibilityFingerprint,
    LibraryCompatibilityProfile,
    LibraryIncompatibility,
)
from app.services.graph_catalog_contracts import GraphCatalogResolvedScope
from app.services.public_v1_contracts import PublicAPIError


NOW = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
REQUEST_ID = "0123456789abcdef0123456789abcdef"


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


def _library(organization_id: uuid.UUID, slug: str) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization_id,
        slug=slug,
        name=slug.title(),
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection=f"collection_{slug}",
        index_state="ready",
        created_at=NOW,
    )


def _profile(library: Library) -> LibraryCompatibilityProfile:
    return LibraryCompatibilityProfile(
        library=library,
        embedding=CompatibilityFingerprint("embedding-v1", "a" * 64, True),
        retrieval=CompatibilityFingerprint("retrieval-v1", "b" * 64, True),
        graph=CompatibilityFingerprint("graph-v1", "c" * 64, True),
    )


def _assessment(
    organization_id: uuid.UUID,
    libraries: tuple[Library, ...],
    *,
    channels=("text",),
    incompatibilities=(),
) -> CompatibilityAssessment:
    return CompatibilityAssessment(
        organization_id=organization_id,
        channels=channels,
        profiles=tuple(_profile(library) for library in libraries),
        compatible=not incompatibilities,
        incompatibilities=tuple(incompatibilities),
    )


def _prepared(
    organization_id: uuid.UUID,
    library: Library,
    *,
    with_record: bool = True,
) -> service.PreparedPublicRetrieval:
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    sources = (
        PublicSourceRead(
            rank=1,
            library_id=library.id,
            library_slug=library.slug,
            library_name=library.name,
            document_id=document_id,
            document_revision_id=revision_id,
            chunk_id=chunk_id,
            score=0.5,
        ),
    )
    chunks = (
        PublicChunkRead(
            rank=1,
            library_id=library.id,
            library_slug=library.slug,
            document_id=document_id,
            document_revision_id=revision_id,
            chunk_id=chunk_id,
            content="grounded evidence",
            content_truncated=False,
            score=0.5,
        ),
    )
    records = (
        DifyRecord(
            content="grounded evidence",
            score=0.5,
            title="Evidence",
            metadata={"public_rank": 1},
        ),
    ) if with_record else ()
    return service.PreparedPublicRetrieval(
        selection=PublicScopeSelection(library_slugs=[library.slug]),
        scope=service.ResolvedPublicScope(organization_id, None, (library,)),
        sources=sources if with_record else (),
        chunks=chunks if with_record else (),
        graph=PublicGraphRead(
            available=False,
            entities=[],
            relations=[],
            documents_examined=0,
            truncated=False,
        ),
        records=records,
    )


@pytest.mark.asyncio
async def test_explicit_scope_preserves_order_and_uses_exact_channel(monkeypatch):
    organization_id = uuid.uuid4()
    libraries = (
        _library(organization_id, "alpha"),
        _library(organization_id, "beta"),
    )
    assess = AsyncMock(return_value=_assessment(organization_id, libraries))
    monkeypatch.setattr(service, "assess_library_compatibility", assess)
    resolved = await service.resolve_public_scope(
        object(),
        user=_user(),
        selection=PublicScopeSelection(library_slugs=["alpha", "beta"]),
        channels=("text",),
    )
    assert [row.slug for row in resolved.libraries] == ["alpha", "beta"]
    assert resolved.organization_id == organization_id
    assert assess.await_args.kwargs["library_slugs"] == ("alpha", "beta")
    assert assess.await_args.kwargs["channels"] == ("text",)


@pytest.mark.asyncio
async def test_incompatible_scope_fails_as_one_unit_with_bounded_details(monkeypatch):
    organization_id = uuid.uuid4()
    libraries = (
        _library(organization_id, "alpha"),
        _library(organization_id, "beta"),
    )
    monkeypatch.setattr(
        service,
        "assess_library_compatibility",
        AsyncMock(
            return_value=_assessment(
                organization_id,
                libraries,
                incompatibilities=(
                    LibraryIncompatibility(
                        "beta",
                        ("embedding_profile_mismatch",),
                    ),
                ),
            )
        ),
    )
    with pytest.raises(PublicAPIError) as exc_info:
        await service.resolve_public_scope(
            object(),
            user=_user(),
            selection=PublicScopeSelection(library_slugs=["alpha", "beta"]),
            channels=("text",),
        )
    assert exc_info.value.code == "scope_incompatible"
    assert exc_info.value.status_code == 409
    assert exc_info.value.details == (
        ("beta", ("embedding_profile_mismatch",)),
    )


@pytest.mark.asyncio
async def test_scope_validation_assesses_text_and_graph_independently(monkeypatch):
    organization_id = uuid.uuid4()
    libraries = (
        _library(organization_id, "alpha"),
        _library(organization_id, "beta"),
    )
    assess = AsyncMock(
        side_effect=[
            _assessment(organization_id, libraries, channels=("text",)),
            _assessment(
                organization_id,
                libraries,
                channels=("graph",),
                incompatibilities=(
                    LibraryIncompatibility("beta", ("graph_profile_mismatch",)),
                ),
            ),
        ]
    )
    monkeypatch.setattr(service, "assess_library_compatibility", assess)
    scope, rows = await service.validate_public_scope(
        object(),
        user=_user(),
        selection=PublicScopeSelection(library_slugs=["alpha", "beta"]),
        channels=("text", "graph"),
    )
    assert [row.slug for row in scope.libraries] == ["alpha", "beta"]
    assert [(row.channel, row.compatible) for row in rows] == [
        ("text", True),
        ("graph", False),
    ]
    assert rows[1].incompatibilities[0].reason_codes == [
        "graph_profile_mismatch"
    ]


@pytest.mark.asyncio
async def test_document_evidence_and_graph_details_delegate_to_catalog_services(
    monkeypatch,
):
    organization_id = uuid.uuid4()
    library = _library(organization_id, "alpha")
    user = _user()
    authorize = AsyncMock(return_value=library)
    document = CatalogDocumentDetailRead.model_construct()
    evidence = CatalogEvidenceDetailRead.model_construct()
    entity = GraphCatalogEntityDetailRead.model_construct()
    relation = GraphCatalogRelationDetailRead.model_construct()
    document_service = AsyncMock(return_value=document)
    evidence_service = AsyncMock(return_value=evidence)
    entity_service = AsyncMock(return_value=entity)
    relation_service = AsyncMock(return_value=relation)
    monkeypatch.setattr(service, "authorize_public_library", authorize)
    monkeypatch.setattr(service, "get_catalog_document_detail", document_service)
    monkeypatch.setattr(service, "get_catalog_evidence_detail", evidence_service)
    monkeypatch.setattr(service, "get_graph_catalog_entity_detail", entity_service)
    monkeypatch.setattr(service, "get_graph_catalog_relation_detail", relation_service)
    document_id, evidence_id, entity_id, relation_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    document_response = await service.get_public_document(
        object(),
        user=user,
        slug="alpha",
        document_id=document_id,
        request_id=REQUEST_ID,
    )
    evidence_response = await service.get_public_evidence(
        object(),
        user=user,
        slug="alpha",
        evidence_id=evidence_id,
        request_id=REQUEST_ID,
    )
    entity_response = await service.get_public_entity(
        object(),
        user=user,
        slug="alpha",
        entity_id=entity_id,
        request_id=REQUEST_ID,
    )
    relation_response = await service.get_public_relation(
        object(),
        user=user,
        slug="alpha",
        relation_id=relation_id,
        request_id=REQUEST_ID,
    )
    assert document_response.document is document
    assert evidence_response.evidence is evidence
    assert entity_response.entity is entity
    assert relation_response.relation is relation
    assert authorize.await_count == 4
    assert document_service.await_args.kwargs == {
        "library": library,
        "document_id": document_id,
    }
    assert evidence_service.await_args.kwargs == {
        "library": library,
        "evidence_id": evidence_id,
    }
    assert entity_service.await_args.kwargs["organization_id"] == organization_id
    assert relation_service.await_args.kwargs["organization_id"] == organization_id


@pytest.mark.asyncio
async def test_graph_searches_preserve_filters_cursor_and_resolved_scope(monkeypatch):
    organization_id = uuid.uuid4()
    libraries = (
        _library(organization_id, "alpha"),
        _library(organization_id, "beta"),
    )
    scope = service.ResolvedPublicScope(organization_id, None, libraries)
    resolve = AsyncMock(return_value=scope)
    entity_page = GraphCatalogEntityPageRead(items=[], next_cursor="entity-next")
    relation_page = GraphCatalogRelationPageRead(items=[], next_cursor="relation-next")
    entity_search = AsyncMock(return_value=entity_page)
    relation_search = AsyncMock(return_value=relation_page)
    monkeypatch.setattr(service, "resolve_public_graph_scope", resolve)
    monkeypatch.setattr(service, "search_graph_catalog_entities", entity_search)
    monkeypatch.setattr(service, "search_graph_catalog_relations", relation_search)
    user = _user()
    entity_body = PublicEntitySearchRequest(
        scope={"library_slugs": ["alpha", "beta"]},
        query="Acme",
        statuses=["active"],
        cursor="entity-cursor",
        limit=25,
    )
    relation_body = PublicRelationSearchRequest(
        scope={"library_slugs": ["alpha", "beta"]},
        type_keys=["invested_in"],
        review_statuses=["approved"],
        cursor="relation-cursor",
        limit=30,
    )
    actual_entity_scope, actual_entity_page = await service.search_public_entities(
        object(),
        user=user,
        body=entity_body,
    )
    actual_relation_scope, actual_relation_page = await service.search_public_relations(
        object(),
        user=user,
        body=relation_body,
    )
    assert actual_entity_scope is actual_relation_scope is scope
    assert actual_entity_page is entity_page
    assert actual_relation_page is relation_page
    assert resolve.await_count == 2
    entity_query = entity_search.await_args.kwargs["query"]
    relation_query = relation_search.await_args.kwargs["query"]
    assert entity_query.selection.library_slugs == ("alpha", "beta")
    assert entity_query.query_text == "Acme"
    assert entity_query.statuses == ("active",)
    assert entity_search.await_args.kwargs["cursor_value"] == "entity-cursor"
    assert relation_query.type_keys == ("invested_in",)
    assert relation_query.review_statuses == ("approved",)
    assert relation_search.await_args.kwargs["cursor_value"] == "relation-cursor"


@pytest.mark.asyncio
async def test_single_library_graph_scope_uses_catalog_allowance_without_compatibility(
    monkeypatch,
):
    organization_id = uuid.uuid4()
    library = _library(organization_id, "alpha")
    access = type("Access", (), {"library": library})()
    authorize = AsyncMock(return_value=(access,))
    graph_resolve = AsyncMock(
        return_value=GraphCatalogResolvedScope(
            organization_id=organization_id,
            libraries=(library,),
        )
    )
    compatibility = AsyncMock()
    monkeypatch.setattr(service, "resolve_library_selection", authorize)
    monkeypatch.setattr(service, "resolve_graph_catalog_scope", graph_resolve)
    monkeypatch.setattr(service, "assess_library_compatibility", compatibility)
    resolved = await service.resolve_public_graph_scope(
        object(),
        user=_user(),
        selection=PublicScopeSelection(library_slugs=["alpha"]),
    )
    assert resolved.libraries == (library,)
    assert graph_resolve.await_args.kwargs["selection"].library_slugs == ("alpha",)
    compatibility.assert_not_awaited()


@pytest.mark.asyncio
async def test_retrieval_projects_existing_federation_without_answer_model(monkeypatch):
    organization_id = uuid.uuid4()
    library = _library(organization_id, "alpha")
    scope = service.ResolvedPublicScope(organization_id, None, (library,))
    assessment = _assessment(organization_id, (library,))
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    result = FederatedRetrievalResult(
        assessment=assessment,
        hits=(
            FederatedHit(
                rank=1,
                fusion_score=0.5,
                library_id=library.id,
                library_slug=library.slug,
                library_name=library.name,
                local_rank=1,
                local_score=0.9,
                title="Contract",
                content_excerpt="Evidence text",
                content_truncated=False,
                source=FederatedSourceProjection(
                    document_id=str(document_id),
                    document_revision_id=str(revision_id),
                    document_revision=2,
                    chunk_id=str(chunk_id),
                    page=3,
                    title_path=("Terms",),
                    vector_score=0.9,
                ),
            ),
        ),
        timings=(),
        total_elapsed_ms=1,
    )
    monkeypatch.setattr(service, "resolve_public_scope", AsyncMock(return_value=scope))
    retrieve = AsyncMock(return_value=result)
    monkeypatch.setattr(service, "run_federated_retrieval", retrieve)
    answer = AsyncMock()
    monkeypatch.setattr(service.chat_answer, "generate_answer", answer)
    monkeypatch.setattr(settings, "graph_retrieval_enabled", False)
    prepared = await service.prepare_public_retrieval(
        object(),
        user=_user(),
        body=PublicRetrievalRequest(
            scope={"library_slugs": ["alpha"]},
            query="contract",
            top_k=5,
            candidate_k=10,
        ),
    )
    assert prepared.sources[0].document_id == document_id
    assert prepared.chunks[0].content == "Evidence text"
    assert prepared.graph.available is False
    assert retrieve.await_args.kwargs["command"].library_slugs == ("alpha",)
    answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_answer_rechecks_scope_immediately_before_model(monkeypatch):
    organization_id = uuid.uuid4()
    library = _library(organization_id, "alpha")
    prepared = _prepared(organization_id, library)
    events: list[str] = []

    async def recheck(*args, **kwargs):  # noqa: ARG001
        events.append("recheck")

    async def generate(*args, **kwargs):  # noqa: ARG001
        events.append("model")
        return ChatAnswer(answer="Grounded answer [1]", used_records=list(prepared.records))

    monkeypatch.setattr(
        service,
        "prepare_public_retrieval",
        AsyncMock(return_value=prepared),
    )
    monkeypatch.setattr(service, "recheck_public_scope", recheck)
    monkeypatch.setattr(service.chat_answer, "generate_answer", generate)
    monkeypatch.setattr(settings, "chat_enabled", True)
    monkeypatch.setattr(settings, "chat_base_url", "http://model/v1")
    monkeypatch.setattr(settings, "chat_model", "model")
    response = await service.generate_public_answer(
        object(),
        user=_user(),
        body=PublicAnswerRequest(
            scope={"library_slugs": ["alpha"]},
            query="question",
        ),
        request_id=REQUEST_ID,
    )
    assert events == ["recheck", "model"]
    assert response.answer == "Grounded answer [1]"
    assert response.sources[0].rank == response.chunks[0].rank == 1


@pytest.mark.asyncio
async def test_empty_retrieval_returns_fixed_answer_without_provider(monkeypatch):
    organization_id = uuid.uuid4()
    library = _library(organization_id, "alpha")
    prepared = _prepared(organization_id, library, with_record=False)
    monkeypatch.setattr(
        service,
        "prepare_public_retrieval",
        AsyncMock(return_value=prepared),
    )
    monkeypatch.setattr(service, "recheck_public_scope", AsyncMock())
    provider = AsyncMock()
    monkeypatch.setattr(service.chat_answer, "generate_answer", provider)
    response = await service.generate_public_answer(
        object(),
        user=_user(),
        body=PublicAnswerRequest(
            scope={"library_slugs": ["alpha"]},
            query="question",
        ),
        request_id=REQUEST_ID,
    )
    assert response.answer == service.NO_EVIDENCE_ANSWER
    assert response.sources == response.chunks == []
    provider.assert_not_awaited()
