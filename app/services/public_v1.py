from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.config import settings
from app.models.library import Library
from app.models.user import User
from app.models.user_library_scope import UserLibraryScope
from app.schemas.dify import DifyRecord
from app.schemas.graph_catalog import (
    GraphCatalogEntityPageRead,
    GraphCatalogRelationPageRead,
)
from app.schemas.public_v1 import (
    PublicAnswerRequest,
    PublicAnswerResponse,
    PublicChannelCompatibilityRead,
    PublicChunkRead,
    PublicDocumentResponse,
    PublicEntityResponse,
    PublicEntitySearchRequest,
    PublicEvidenceResponse,
    PublicGraphEntityRead,
    PublicGraphRead,
    PublicGraphRelationRead,
    PublicIncompatibilityRead,
    PublicLibraryRead,
    PublicRelationResponse,
    PublicRelationSearchRequest,
    PublicResolvedScopeRead,
    PublicRetrievalRequest,
    PublicRetrievalResponse,
    PublicScopeSelection,
    PublicSourceRead,
)
from app.services import chat_answer
from app.services.federated_retrieval import run_federated_retrieval
from app.services.federated_retrieval_contracts import (
    FederatedRetrievalCommand,
    FederatedRetrievalError,
)
from app.services.graph_catalog import (
    search_graph_catalog_entities,
    search_graph_catalog_relations,
)
from app.services.graph_catalog_contracts import (
    GraphCatalogError,
    GraphCatalogScopeError,
    GraphCatalogSelection,
    GraphEntityCatalogQuery,
    GraphRelationCatalogQuery,
)
from app.services.graph_catalog_details import (
    get_graph_catalog_entity_detail,
    get_graph_catalog_relation_detail,
)
from app.services.graph_catalog_scope import resolve_graph_catalog_scope
from app.services.knowledge_catalog import (
    get_catalog_document_detail,
    get_catalog_evidence_detail,
)
from app.services.knowledge_catalog_contracts import KnowledgeCatalogError
from app.services.library_compatibility import assess_library_compatibility
from app.services.library_compatibility_contracts import (
    CompatibilityAssessment,
    CompatibilityChannel,
    LibraryCompatibilityError,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    authorize_library,
    credential_organization_scope,
    list_accessible_libraries,
    resolve_library_selection,
)
from app.services.personal_library_scopes import (
    PersonalLibraryScopeError,
    resolve_named_scope,
)
from app.services.public_api_operations import (
    PublicAnswerTimedOut,
    PublicOperationsError,
    PublicRateLimitExceeded,
    acquire_public_answer_lease,
    admit_public_request,
    run_with_public_answer_timeout,
)
from app.services.public_api_operations_contracts import (
    PublicAdmissionScope,
    PublicOperationContext,
)
from app.services.public_v1_contracts import PublicAPIError


MAX_PUBLIC_LIBRARIES = 500
MAX_PUBLIC_GRAPH_DOCUMENTS = 5
MAX_PUBLIC_GRAPH_ENTITIES = 50
MAX_PUBLIC_GRAPH_RELATIONS = 50
NO_EVIDENCE_ANSWER = "No grounded evidence was found in the selected knowledge libraries."


@dataclass(frozen=True, slots=True)
class ResolvedPublicScope:
    organization_id: uuid.UUID
    scope_id: uuid.UUID | None
    libraries: tuple[Library, ...]

    def as_read(self) -> PublicResolvedScopeRead:
        return PublicResolvedScopeRead(
            organization_id=self.organization_id,
            scope_id=self.scope_id,
            libraries=[public_library(row) for row in self.libraries],
        )


@dataclass(frozen=True, slots=True)
class PreparedPublicRetrieval:
    selection: PublicScopeSelection
    scope: ResolvedPublicScope
    sources: tuple[PublicSourceRead, ...]
    chunks: tuple[PublicChunkRead, ...]
    graph: PublicGraphRead
    records: tuple[DifyRecord, ...]

    def grounding_for(
        self,
        records: list | tuple,
    ) -> tuple[list[PublicSourceRead], list[PublicChunkRead]]:
        ranks: set[int] = set()
        for record in records:
            metadata = getattr(record, "metadata", None) or {}
            rank = metadata.get("public_rank")
            if isinstance(rank, int) and not isinstance(rank, bool):
                ranks.add(rank)
        sources = [row for row in self.sources if row.rank in ranks]
        chunks = [row for row in self.chunks if row.rank in ranks]
        rank_map = {row.rank: index for index, row in enumerate(chunks, start=1)}
        return (
            [row.model_copy(update={"rank": rank_map[row.rank]}) for row in sources],
            [row.model_copy(update={"rank": rank_map[row.rank]}) for row in chunks],
        )


def _operation_error(exc: PublicOperationsError) -> PublicAPIError:
    if isinstance(exc, PublicRateLimitExceeded):
        return PublicAPIError(
            "rate_limited",
            status_code=429,
            retry_after_seconds=exc.retry_after_seconds,
        )
    return PublicAPIError("service_unavailable", status_code=503)


async def admit_public_scope(
    scope: ResolvedPublicScope,
    operation_context: PublicOperationContext | None,
    *,
    include_library_ids: bool = True,
) -> None:
    if operation_context is None:
        return
    library_ids = (
        tuple(row.id for row in scope.libraries) if include_library_ids else ()
    )
    operation_context.bind_scope(
        organization_ids=(scope.organization_id,),
        library_ids=library_ids,
    )
    try:
        await admit_public_request(
            PublicAdmissionScope(
                organization_ids=(scope.organization_id,),
                api_key_id=operation_context.api_key_id,
            )
        )
    except PublicOperationsError as exc:
        raise _operation_error(exc) from exc


async def acquire_public_answer_capacity(
    prepared: PreparedPublicRetrieval,
    operation_context: PublicOperationContext | None,
) -> None:
    if operation_context is None:
        return
    try:
        operation_context.answer_lease = await acquire_public_answer_lease(
            request_id=operation_context.request_id,
            endpoint_key=operation_context.endpoint_key,
            organization_id=prepared.scope.organization_id,
            api_key_id=operation_context.api_key_id,
        )
    except PublicOperationsError as exc:
        raise _operation_error(exc) from exc
    operation_context.answer_model = settings.chat_model


def project_public_retrieval_metrics(
    operation_context: PublicOperationContext | None,
    prepared: PreparedPublicRetrieval,
    *,
    used_records: list | tuple | None = None,
) -> None:
    if operation_context is None:
        return
    if used_records is None:
        sources, chunks = prepared.sources, prepared.chunks
    else:
        sources, chunks = prepared.grounding_for(used_records)
    operation_context.source_count = len(sources)
    operation_context.chunk_count = len(chunks)
    operation_context.graph_entity_count = len(prepared.graph.entities)
    operation_context.graph_relation_count = len(prepared.graph.relations)


def public_library(library: Library) -> PublicLibraryRead:
    return PublicLibraryRead(
        id=library.id,
        organization_id=library.organization_id,
        slug=library.slug,
        name=library.name,
        index_state=library.index_state,
    )


def _scope_details(assessment: CompatibilityAssessment):
    return tuple(
        (item.library_slug, tuple(item.reason_codes))
        for item in assessment.incompatibilities
    )


def _map_compatibility_error(exc: Exception) -> PublicAPIError:
    if isinstance(exc, OrganizationAuthorizationError):
        return PublicAPIError("scope_forbidden", status_code=403)
    if isinstance(exc, PersonalLibraryScopeError):
        code = (
            "resource_not_found"
            if exc.code == "personal_scope_not_found"
            else "scope_forbidden"
        )
        status_code = 404 if code == "resource_not_found" else 403
        return PublicAPIError(code, status_code=status_code)
    return PublicAPIError("service_unavailable", status_code=503)


async def _saved_scope_selection(
    db,
    *,
    user: User,
    scope_id: uuid.UUID,
) -> tuple[uuid.UUID, tuple[str, ...]]:
    organization_id = await _owned_saved_scope_organization(
        db,
        user=user,
        scope_id=scope_id,
    )
    try:
        resolved = await resolve_named_scope(
            db,
            user=user,
            organization_id=organization_id,
            scope_id=scope_id,
        )
    except (OrganizationAuthorizationError, PersonalLibraryScopeError) as exc:
        raise _map_compatibility_error(exc) from exc
    if resolved.removed or not resolved.libraries:
        raise PublicAPIError("scope_forbidden", status_code=403)
    return organization_id, tuple(row.slug for row in resolved.libraries)


async def _owned_saved_scope_organization(
    db,
    *,
    user: User,
    scope_id: uuid.UUID,
) -> uuid.UUID:
    organization_id = (
        await db.execute(
            select(UserLibraryScope.organization_id).where(
                UserLibraryScope.id == scope_id,
                UserLibraryScope.user_id == user.id,
                UserLibraryScope.scope_kind == "named",
            )
        )
    ).scalar_one_or_none()
    if organization_id is None:
        raise PublicAPIError("resource_not_found", status_code=404)
    credential_scope = credential_organization_scope(user)
    if (
        credential_scope is not None
        and credential_scope.organization_id != organization_id
    ):
        raise PublicAPIError("scope_forbidden", status_code=403)
    return organization_id


async def _assess_selection(
    db,
    *,
    user: User,
    selection: PublicScopeSelection,
    channels: tuple[CompatibilityChannel, ...],
) -> tuple[CompatibilityAssessment, uuid.UUID | None]:
    expected_organization_id = None
    if selection.library_slugs is not None:
        slugs = tuple(selection.library_slugs)
    else:
        expected_organization_id, slugs = await _saved_scope_selection(
            db,
            user=user,
            scope_id=selection.scope_id,
        )
    try:
        assessment = await assess_library_compatibility(
            db,
            user=user,
            library_slugs=slugs,
            channels=channels,
        )
    except (OrganizationAuthorizationError, LibraryCompatibilityError) as exc:
        raise _map_compatibility_error(exc) from exc
    if (
        expected_organization_id is not None
        and assessment.organization_id != expected_organization_id
    ):
        raise PublicAPIError("scope_forbidden", status_code=403)
    return assessment, selection.scope_id


def _resolved_scope(
    assessment: CompatibilityAssessment,
    scope_id: uuid.UUID | None,
) -> ResolvedPublicScope:
    libraries = tuple(profile.library for profile in assessment.profiles)
    if (
        not libraries
        or len({row.id for row in libraries}) != len(libraries)
        or any(row.organization_id != assessment.organization_id for row in libraries)
    ):
        raise PublicAPIError("service_unavailable", status_code=503)
    return ResolvedPublicScope(assessment.organization_id, scope_id, libraries)


async def resolve_public_scope(
    db,
    *,
    user: User,
    selection: PublicScopeSelection,
    channels: tuple[CompatibilityChannel, ...],
) -> ResolvedPublicScope:
    assessment, scope_id = await _assess_selection(
        db,
        user=user,
        selection=selection,
        channels=channels,
    )
    if not assessment.compatible:
        raise PublicAPIError(
            "scope_incompatible",
            status_code=409,
            details=_scope_details(assessment),
        )
    return _resolved_scope(assessment, scope_id)


async def validate_public_scope(
    db,
    *,
    user: User,
    selection: PublicScopeSelection,
    channels: tuple[CompatibilityChannel, ...],
    operation_context: PublicOperationContext | None = None,
) -> tuple[ResolvedPublicScope, tuple[PublicChannelCompatibilityRead, ...]]:
    rows: list[PublicChannelCompatibilityRead] = []
    resolved: ResolvedPublicScope | None = None
    for channel in channels:
        assessment, scope_id = await _assess_selection(
            db,
            user=user,
            selection=selection,
            channels=(channel,),
        )
        candidate = _resolved_scope(assessment, scope_id)
        if resolved is None:
            resolved = candidate
            await admit_public_scope(resolved, operation_context)
        elif (
            candidate.organization_id != resolved.organization_id
            or tuple(row.id for row in candidate.libraries)
            != tuple(row.id for row in resolved.libraries)
        ):
            raise PublicAPIError("service_unavailable", status_code=503)
        rows.append(
            PublicChannelCompatibilityRead(
                channel=channel,
                compatible=assessment.compatible,
                incompatibilities=[
                    PublicIncompatibilityRead(
                        library_slug=item.library_slug,
                        reason_codes=list(item.reason_codes),
                    )
                    for item in assessment.incompatibilities
                ],
            )
        )
    if resolved is None:
        raise PublicAPIError("request_invalid", status_code=422)
    return resolved, tuple(rows)


async def list_public_libraries(
    db,
    *,
    user: User,
    operation_context: PublicOperationContext | None = None,
) -> tuple[tuple[Library, ...], bool]:
    try:
        rows = await list_accessible_libraries(db, user=user, action="read")
    except OrganizationAuthorizationError as exc:
        raise PublicAPIError("scope_forbidden", status_code=403) from exc
    visible = tuple(rows[:MAX_PUBLIC_LIBRARIES])
    if operation_context is not None and visible:
        organization_ids = tuple(
            dict.fromkeys(row.organization_id for row in visible)
        )
        operation_context.bind_scope(
            organization_ids=organization_ids,
            library_ids=(),
        )
        try:
            await admit_public_request(
                PublicAdmissionScope(
                    organization_ids=organization_ids,
                    api_key_id=operation_context.api_key_id,
                )
            )
        except PublicOperationsError as exc:
            raise _operation_error(exc) from exc
        operation_context.source_count = len(visible)
    return visible, len(rows) > MAX_PUBLIC_LIBRARIES


async def authorize_public_library(db, *, user: User, slug: str) -> Library:
    try:
        return await authorize_library(
            db,
            user=user,
            library_slug=slug,
            action="read",
        )
    except OrganizationAuthorizationError as exc:
        raise PublicAPIError("scope_forbidden", status_code=403) from exc


async def resolve_public_graph_scope(
    db,
    *,
    user: User,
    selection: PublicScopeSelection,
) -> ResolvedPublicScope:
    if selection.library_slugs is not None:
        try:
            accesses = await resolve_library_selection(
                db,
                user=user,
                library_slugs=tuple(selection.library_slugs),
                action="read",
            )
        except OrganizationAuthorizationError as exc:
            raise PublicAPIError("scope_forbidden", status_code=403) from exc
        libraries = tuple(access.library for access in accesses)
        if not libraries or len({row.organization_id for row in libraries}) != 1:
            raise PublicAPIError("scope_forbidden", status_code=403)
        organization_id = libraries[0].organization_id
        graph_selection = GraphCatalogSelection(
            organization_id=organization_id,
            library_slugs=tuple(selection.library_slugs),
        )
    else:
        organization_id = await _owned_saved_scope_organization(
            db,
            user=user,
            scope_id=selection.scope_id,
        )
        graph_selection = GraphCatalogSelection(
            organization_id=organization_id,
            scope_id=selection.scope_id,
        )
    try:
        resolved = await resolve_graph_catalog_scope(
            db,
            user=user,
            selection=graph_selection,
        )
    except GraphCatalogError as exc:
        raise _map_graph_error(exc) from exc
    if (
        resolved.organization_id != organization_id
        or not resolved.libraries
        or any(row.organization_id != organization_id for row in resolved.libraries)
    ):
        raise PublicAPIError("service_unavailable", status_code=503)
    return ResolvedPublicScope(
        organization_id=organization_id,
        scope_id=selection.scope_id,
        libraries=tuple(resolved.libraries),
    )


def _map_catalog_error(exc: KnowledgeCatalogError) -> PublicAPIError:
    if exc.code == "catalog_not_found":
        return PublicAPIError("resource_not_found", status_code=404)
    if exc.code == "catalog_invariant_failed":
        return PublicAPIError("service_unavailable", status_code=503)
    return PublicAPIError("request_invalid", status_code=422)


def _map_graph_error(exc: GraphCatalogError) -> PublicAPIError:
    if exc.code in {"graph_catalog_scope_forbidden", "graph_catalog_not_found"}:
        code = (
            "scope_forbidden"
            if exc.code == "graph_catalog_scope_forbidden"
            else "resource_not_found"
        )
        return PublicAPIError(code, status_code=403 if code == "scope_forbidden" else 404)
    if exc.code == "graph_catalog_scope_incompatible":
        details = ()
        if isinstance(exc, GraphCatalogScopeError):
            details = tuple(
                (row.library_slug, tuple(row.reason_codes))
                for row in exc.incompatibilities
            )
        return PublicAPIError(
            "scope_incompatible",
            status_code=409,
            details=details,
        )
    if exc.code == "graph_catalog_unavailable":
        return PublicAPIError("service_unavailable", status_code=503)
    return PublicAPIError("request_invalid", status_code=422)


async def get_public_document(
    db,
    *,
    user: User,
    slug: str,
    document_id: uuid.UUID,
    request_id: str,
    operation_context: PublicOperationContext | None = None,
) -> PublicDocumentResponse:
    library = await authorize_public_library(db, user=user, slug=slug)
    await admit_public_scope(
        ResolvedPublicScope(library.organization_id, None, (library,)),
        operation_context,
    )
    try:
        document = await get_catalog_document_detail(
            db,
            library=library,
            document_id=document_id,
        )
    except KnowledgeCatalogError as exc:
        raise _map_catalog_error(exc) from exc
    return PublicDocumentResponse(
        request_id=request_id,
        library=public_library(library),
        document=document,
    )


async def get_public_evidence(
    db,
    *,
    user: User,
    slug: str,
    evidence_id: uuid.UUID,
    request_id: str,
    operation_context: PublicOperationContext | None = None,
) -> PublicEvidenceResponse:
    library = await authorize_public_library(db, user=user, slug=slug)
    await admit_public_scope(
        ResolvedPublicScope(library.organization_id, None, (library,)),
        operation_context,
    )
    try:
        evidence = await get_catalog_evidence_detail(
            db,
            library=library,
            evidence_id=evidence_id,
        )
    except KnowledgeCatalogError as exc:
        raise _map_catalog_error(exc) from exc
    return PublicEvidenceResponse(
        request_id=request_id,
        library=public_library(library),
        evidence=evidence,
    )


async def get_public_entity(
    db,
    *,
    user: User,
    slug: str,
    entity_id: uuid.UUID,
    request_id: str,
    operation_context: PublicOperationContext | None = None,
) -> PublicEntityResponse:
    library = await authorize_public_library(db, user=user, slug=slug)
    await admit_public_scope(
        ResolvedPublicScope(library.organization_id, None, (library,)),
        operation_context,
    )
    try:
        entity = await get_graph_catalog_entity_detail(
            db,
            user=user,
            organization_id=library.organization_id,
            library_slug=library.slug,
            entity_id=entity_id,
        )
    except GraphCatalogError as exc:
        raise _map_graph_error(exc) from exc
    return PublicEntityResponse(request_id=request_id, entity=entity)


async def get_public_relation(
    db,
    *,
    user: User,
    slug: str,
    relation_id: uuid.UUID,
    request_id: str,
    operation_context: PublicOperationContext | None = None,
) -> PublicRelationResponse:
    library = await authorize_public_library(db, user=user, slug=slug)
    await admit_public_scope(
        ResolvedPublicScope(library.organization_id, None, (library,)),
        operation_context,
    )
    try:
        relation = await get_graph_catalog_relation_detail(
            db,
            user=user,
            organization_id=library.organization_id,
            library_slug=library.slug,
            relation_id=relation_id,
        )
    except GraphCatalogError as exc:
        raise _map_graph_error(exc) from exc
    return PublicRelationResponse(request_id=request_id, relation=relation)


async def search_public_entities(
    db,
    *,
    user: User,
    body: PublicEntitySearchRequest,
    operation_context: PublicOperationContext | None = None,
) -> tuple[ResolvedPublicScope, GraphCatalogEntityPageRead]:
    scope = await resolve_public_graph_scope(
        db,
        user=user,
        selection=body.scope,
    )
    await admit_public_scope(scope, operation_context)
    try:
        page = await search_graph_catalog_entities(
            db,
            user=user,
            query=GraphEntityCatalogQuery(
                selection=GraphCatalogSelection(
                    organization_id=scope.organization_id,
                    library_slugs=tuple(row.slug for row in scope.libraries),
                ),
                query_text=body.query,
                ontology_version_ids=tuple(body.ontology_version_ids),
                type_keys=tuple(body.type_keys),
                statuses=tuple(body.statuses),
                source_types=tuple(body.source_types),
                publication_state=body.publication_state,
                limit=body.limit,
            ),
            cursor_value=body.cursor,
        )
    except GraphCatalogError as exc:
        raise _map_graph_error(exc) from exc
    if operation_context is not None:
        operation_context.graph_entity_count = len(page.items)
    return scope, page


async def search_public_relations(
    db,
    *,
    user: User,
    body: PublicRelationSearchRequest,
    operation_context: PublicOperationContext | None = None,
) -> tuple[ResolvedPublicScope, GraphCatalogRelationPageRead]:
    scope = await resolve_public_graph_scope(
        db,
        user=user,
        selection=body.scope,
    )
    await admit_public_scope(scope, operation_context)
    try:
        page = await search_graph_catalog_relations(
            db,
            user=user,
            query=GraphRelationCatalogQuery(
                selection=GraphCatalogSelection(
                    organization_id=scope.organization_id,
                    library_slugs=tuple(row.slug for row in scope.libraries),
                ),
                query_text=body.query,
                ontology_version_ids=tuple(body.ontology_version_ids),
                type_keys=tuple(body.type_keys),
                statuses=tuple(body.statuses),
                review_statuses=tuple(body.review_statuses),
                source_types=tuple(body.source_types),
                publication_state=body.publication_state,
                limit=body.limit,
            ),
            cursor_value=body.cursor,
        )
    except GraphCatalogError as exc:
        raise _map_graph_error(exc) from exc
    if operation_context is not None:
        operation_context.graph_relation_count = len(page.items)
    return scope, page


def _uuid_or_none(value) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _retrieval_rows(result) -> tuple[
    tuple[PublicSourceRead, ...],
    tuple[PublicChunkRead, ...],
    tuple[DifyRecord, ...],
]:
    sources: list[PublicSourceRead] = []
    chunks: list[PublicChunkRead] = []
    records: list[DifyRecord] = []
    for hit in result.hits:
        document_id = _uuid_or_none(hit.source.document_id)
        revision_id = _uuid_or_none(hit.source.document_revision_id)
        chunk_id = _uuid_or_none(hit.source.chunk_id)
        source = PublicSourceRead(
            rank=hit.rank,
            library_id=hit.library_id,
            library_slug=hit.library_slug,
            library_name=hit.library_name,
            document_id=document_id,
            document_revision_id=revision_id,
            document_revision=hit.source.document_revision,
            chunk_id=chunk_id,
            seq=hit.source.seq,
            page=hit.source.page,
            title_path=list(hit.source.title_path),
            title=hit.title,
            score=hit.fusion_score,
            vector_score=hit.source.vector_score,
            rerank_score=hit.source.rerank_score,
        )
        chunk = PublicChunkRead(
            rank=hit.rank,
            library_id=hit.library_id,
            library_slug=hit.library_slug,
            document_id=document_id,
            document_revision_id=revision_id,
            chunk_id=chunk_id,
            title=hit.title,
            content=hit.content_excerpt,
            content_truncated=hit.content_truncated,
            score=hit.fusion_score,
        )
        metadata = {
            "public_rank": hit.rank,
            "library_id": str(hit.library_id),
            "library_slug": hit.library_slug,
            "document_id": str(document_id) if document_id else None,
            "document_revision_id": str(revision_id) if revision_id else None,
            "chunk_id": str(chunk_id) if chunk_id else None,
            "seq": hit.source.seq,
            "page": hit.source.page,
            "title_path": list(hit.source.title_path),
        }
        sources.append(source)
        chunks.append(chunk)
        records.append(
            DifyRecord(
                content=hit.content_excerpt,
                score=hit.fusion_score,
                title=hit.title,
                metadata=metadata,
            )
        )
    return tuple(sources), tuple(chunks), tuple(records)


async def _public_graph_context(
    db,
    *,
    scope: ResolvedPublicScope,
    chunks: tuple[PublicChunkRead, ...],
) -> PublicGraphRead:
    if not settings.graph_retrieval_enabled:
        return PublicGraphRead(
            available=False,
            entities=[],
            relations=[],
            documents_examined=0,
            truncated=False,
        )
    library_by_id = {row.id: row for row in scope.libraries}
    documents: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
    seen_documents: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for chunk in chunks:
        if chunk.document_id is None or chunk.document_revision_id is None:
            continue
        key = (chunk.library_id, chunk.document_id)
        if key in seen_documents:
            continue
        seen_documents.add(key)
        documents.append((chunk.library_id, chunk.document_id, chunk.document_revision_id))
    selected = documents[:MAX_PUBLIC_GRAPH_DOCUMENTS]
    truncated = len(documents) > len(selected)
    entities: list[PublicGraphEntityRead] = []
    relations: list[PublicGraphRelationRead] = []
    entity_keys: set[tuple[uuid.UUID, uuid.UUID]] = set()
    relation_keys: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for library_id, document_id, revision_id in selected:
        library = library_by_id.get(library_id)
        if library is None:
            raise PublicAPIError("service_unavailable", status_code=503)
        try:
            detail = await get_catalog_document_detail(
                db,
                library=library,
                document_id=document_id,
            )
        except KnowledgeCatalogError as exc:
            raise PublicAPIError("service_unavailable", status_code=503) from exc
        if detail.revision_id != revision_id:
            raise PublicAPIError("service_unavailable", status_code=503)
        library_read = public_library(library)
        truncated = truncated or detail.graph.entities_truncated or detail.graph.relations_truncated
        for fact in detail.graph.entities:
            key = (library_id, fact.entity_id)
            if key in entity_keys:
                continue
            if len(entities) >= MAX_PUBLIC_GRAPH_ENTITIES:
                truncated = True
                continue
            entity_keys.add(key)
            entities.append(PublicGraphEntityRead(library=library_read, fact=fact))
        for fact in detail.graph.relations:
            key = (library_id, fact.relation_id)
            if key in relation_keys:
                continue
            if len(relations) >= MAX_PUBLIC_GRAPH_RELATIONS:
                truncated = True
                continue
            relation_keys.add(key)
            relations.append(PublicGraphRelationRead(library=library_read, fact=fact))
    return PublicGraphRead(
        available=True,
        entities=entities,
        relations=relations,
        documents_examined=len(selected),
        truncated=truncated,
    )


def _map_federated_error(exc: FederatedRetrievalError) -> PublicAPIError:
    if exc.code == "federated_scope_forbidden":
        return PublicAPIError("scope_forbidden", status_code=403)
    if exc.code == "federated_scope_incompatible":
        return PublicAPIError(
            "scope_incompatible",
            status_code=409,
            details=tuple(
                (row.library_slug, tuple(row.reason_codes))
                for row in exc.incompatibilities
            ),
        )
    if exc.code == "federated_request_invalid":
        return PublicAPIError("request_invalid", status_code=422)
    if exc.code == "federated_branch_failed":
        return PublicAPIError("upstream_failed", status_code=502)
    return PublicAPIError("service_unavailable", status_code=503)


async def prepare_public_retrieval(
    db,
    *,
    user: User,
    body: PublicRetrievalRequest | PublicAnswerRequest,
    operation_context: PublicOperationContext | None = None,
) -> PreparedPublicRetrieval:
    scope = await resolve_public_scope(
        db,
        user=user,
        selection=body.scope,
        channels=("text",),
    )
    await admit_public_scope(scope, operation_context)
    try:
        result = await run_federated_retrieval(
            db,
            user=user,
            command=FederatedRetrievalCommand(
                organization_id=scope.organization_id,
                library_slugs=tuple(row.slug for row in scope.libraries),
                query=body.query,
                top_k=body.top_k,
                candidate_k=body.candidate_k,
                score_threshold=body.score_threshold,
            ),
        )
    except FederatedRetrievalError as exc:
        raise _map_federated_error(exc) from exc
    result_library_ids = tuple(profile.library.id for profile in result.assessment.profiles)
    if result_library_ids != tuple(row.id for row in scope.libraries):
        raise PublicAPIError("service_unavailable", status_code=503)
    sources, chunks, records = _retrieval_rows(result)
    graph = await _public_graph_context(db, scope=scope, chunks=chunks)
    prepared = PreparedPublicRetrieval(
        selection=body.scope,
        scope=scope,
        sources=sources,
        chunks=chunks,
        graph=graph,
        records=records,
    )
    project_public_retrieval_metrics(operation_context, prepared)
    return prepared


async def recheck_public_scope(
    db,
    *,
    user: User,
    prepared: PreparedPublicRetrieval,
) -> None:
    current = await resolve_public_scope(
        db,
        user=user,
        selection=prepared.selection,
        channels=("text",),
    )
    if (
        current.organization_id != prepared.scope.organization_id
        or tuple(row.id for row in current.libraries)
        != tuple(row.id for row in prepared.scope.libraries)
    ):
        raise PublicAPIError("scope_forbidden", status_code=403)


def build_public_retrieval_response(
    request_id: str,
    prepared: PreparedPublicRetrieval,
) -> PublicRetrievalResponse:
    return PublicRetrievalResponse(
        request_id=request_id,
        scope=prepared.scope.as_read(),
        sources=list(prepared.sources),
        chunks=list(prepared.chunks),
        graph=prepared.graph,
    )


def build_public_answer_response(
    request_id: str,
    answer: str,
    prepared: PreparedPublicRetrieval,
    used_records: list | tuple,
) -> PublicAnswerResponse:
    sources, chunks = prepared.grounding_for(used_records)
    return PublicAnswerResponse(
        request_id=request_id,
        answer=answer,
        sources=sources,
        chunks=chunks,
        graph=prepared.graph,
    )


def public_answer_records(prepared: PreparedPublicRetrieval) -> list[DifyRecord]:
    return list(prepared.records)


async def generate_public_answer(
    db,
    *,
    user: User,
    body: PublicAnswerRequest,
    request_id: str,
    operation_context: PublicOperationContext | None = None,
) -> PublicAnswerResponse:
    prepare_kwargs = (
        {"operation_context": operation_context}
        if operation_context is not None
        else {}
    )
    prepared = await prepare_public_retrieval(
        db,
        user=user,
        body=body,
        **prepare_kwargs,
    )
    await recheck_public_scope(db, user=user, prepared=prepared)
    records = public_answer_records(prepared)
    if not records:
        project_public_retrieval_metrics(
            operation_context,
            prepared,
            used_records=(),
        )
        return build_public_answer_response(
            request_id,
            NO_EVIDENCE_ANSWER,
            prepared,
            (),
        )
    if not settings.chat_enabled or not settings.chat_base_url or not settings.chat_model:
        raise PublicAPIError("answer_unavailable", status_code=503)
    if operation_context is not None:
        await acquire_public_answer_capacity(prepared, operation_context)
    provider_call = chat_answer.generate_answer(
        body.query,
        records,
        base_url=settings.chat_base_url,
        model=settings.chat_model,
        api_key=settings.chat_api_key,
        timeout=(
            min(
                settings.chat_timeout_seconds,
                settings.public_api_answer_max_seconds,
            )
            if operation_context is not None
            else settings.chat_timeout_seconds
        ),
        temperature=settings.chat_temperature,
        max_context_chars=settings.chat_max_context_chars,
    )
    try:
        if operation_context is None:
            result = await provider_call
        else:
            result = await run_with_public_answer_timeout(provider_call)
    except chat_answer.ChatError as exc:
        raise PublicAPIError("upstream_failed", status_code=502) from exc
    except PublicAnswerTimedOut as exc:
        raise PublicAPIError("upstream_failed", status_code=502) from exc
    project_public_retrieval_metrics(
        operation_context,
        prepared,
        used_records=result.used_records,
    )
    return build_public_answer_response(
        request_id,
        result.answer,
        prepared,
        result.used_records,
    )
