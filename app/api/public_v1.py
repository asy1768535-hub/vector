from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.models.user import User
from app.schemas.public_v1 import (
    PublicAnswerRequest,
    PublicAnswerResponse,
    PublicDocumentResponse,
    PublicEntityResponse,
    PublicEntitySearchRequest,
    PublicEntitySearchResponse,
    PublicErrorEnvelope,
    PublicEvidenceResponse,
    PublicLibrariesResponse,
    PublicRelationResponse,
    PublicRelationSearchRequest,
    PublicRelationSearchResponse,
    PublicRetrievalRequest,
    PublicRetrievalResponse,
    PublicScopeValidateRequest,
    PublicScopeValidationResponse,
    PublicStreamDeltaRead,
    PublicStreamMetaRead,
)
from app.services import chat_answer
from app.services.public_v1 import (
    NO_EVIDENCE_ANSWER,
    build_public_answer_response,
    build_public_retrieval_response,
    acquire_public_answer_capacity,
    generate_public_answer,
    get_public_document,
    get_public_entity,
    get_public_evidence,
    get_public_relation,
    list_public_libraries,
    prepare_public_retrieval,
    project_public_retrieval_metrics,
    public_answer_records,
    public_library,
    recheck_public_scope,
    search_public_entities,
    search_public_relations,
    validate_public_scope,
)
from app.services.organization_authorization import credential_organization_scope
from app.services.public_api_operations_contracts import (
    PublicEndpointKey,
    PublicOperationContext,
)
from app.services.public_v1_contracts import (
    PublicAPIError,
    PublicV1Route,
    public_error_envelope,
    public_request_id,
    split_public_delta,
)


_ERROR_RESPONSES = {
    code: {
        "model": PublicErrorEnvelope,
        "description": description,
    }
    for code, description in {
        401: "Authentication is required.",
        403: "The selected scope is not available.",
        404: "The capability or resource was not found.",
        409: "The selected Libraries are incompatible.",
        422: "The request does not match the public v1 contract.",
        429: "The public API request limit was reached.",
        500: "The request could not be completed.",
        502: "An upstream knowledge service failed.",
        503: "The knowledge service is temporarily unavailable.",
    }.items()
}

router = APIRouter(
    prefix="/api/v1",
    tags=["public-v1"],
    route_class=PublicV1Route,
    responses=_ERROR_RESPONSES,
)


def require_public_api_v1_enabled() -> None:
    if not settings.public_api_v1_enabled:
        raise PublicAPIError("not_found", status_code=404)


def public_operation_dependency(
    endpoint_key: PublicEndpointKey,
    *,
    is_stream: bool = False,
):
    async def bind_operation_context(
        request: Request,
        user: User = Depends(current_active_user),
    ) -> PublicOperationContext | None:
        if not settings.public_api_operations_enabled:
            return None
        credential_scope = credential_organization_scope(user)
        context = PublicOperationContext(
            request_id=public_request_id(request),
            endpoint_key=endpoint_key,
            http_method=request.method,
            user_id=user.id,
            api_key_id=(
                credential_scope.api_key_id
                if credential_scope is not None
                else None
            ),
            is_stream=is_stream,
            rollout_capability=(
                "mcp_adapter"
                if request.headers.get("X-Vector-KB-Client") == "mcp-adapter"
                else "public_api_v1"
            ),
        )
        request.state.public_operation_context = context
        return context

    bind_operation_context.public_endpoint_key = endpoint_key
    return bind_operation_context


def _operation_kwargs(
    operation_context: PublicOperationContext | None,
) -> dict[str, PublicOperationContext]:
    return (
        {"operation_context": operation_context}
        if operation_context is not None
        else {}
    )


def _sse(event: str, payload) -> str:
    data = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {data}\n\n"


async def _public_answer_events(
    *,
    request_id: str,
    query: str,
    prepared,
    operation_context: PublicOperationContext | None = None,
) -> AsyncIterator[str]:
    yield _sse(
        "meta",
        PublicStreamMetaRead(request_id=request_id),
    )
    records = public_answer_records(prepared)
    if not records:
        for delta in split_public_delta(NO_EVIDENCE_ANSWER):
            yield _sse(
                "delta",
                PublicStreamDeltaRead(request_id=request_id, text=delta),
            )
        project_public_retrieval_metrics(
            operation_context,
            prepared,
            used_records=(),
        )
        if operation_context is not None:
            operation_context.stream_outcome = "completed"
        yield _sse(
            "result",
            build_public_answer_response(
                request_id,
                NO_EVIDENCE_ANSWER,
                prepared,
                (),
            ),
        )
        return

    provider = None
    answer_parts: list[str] = []
    answer_length = 0
    try:
        _, used_records = chat_answer.build_context(
            records,
            settings.chat_max_context_chars,
        )
        provider = chat_answer.stream_answer(
            query,
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
        async for provider_delta in provider:
            for delta in split_public_delta(provider_delta):
                answer_length += len(delta)
                if answer_length > chat_answer.CHAT_OUTPUT_MAX_CHARS:
                    raise PublicAPIError("upstream_failed", status_code=502)
                answer_parts.append(delta)
                yield _sse(
                    "delta",
                    PublicStreamDeltaRead(request_id=request_id, text=delta),
                )
        if not answer_parts:
            raise PublicAPIError("upstream_failed", status_code=502)
        project_public_retrieval_metrics(
            operation_context,
            prepared,
            used_records=used_records,
        )
        if operation_context is not None:
            operation_context.stream_outcome = "completed"
        yield _sse(
            "result",
            build_public_answer_response(
                request_id,
                "".join(answer_parts),
                prepared,
                used_records,
            ),
        )
    except asyncio.CancelledError:
        if operation_context is not None:
            operation_context.stream_outcome = "cancelled"
        raise
    except chat_answer.ChatError:
        if operation_context is not None:
            operation_context.stream_outcome = "failed"
            operation_context.stream_error_code = "upstream_failed"
        yield _sse(
            "error",
            public_error_envelope(
                request_id,
                PublicAPIError("upstream_failed", status_code=502),
            ),
        )
    except PublicAPIError as exc:
        if operation_context is not None:
            operation_context.stream_outcome = "failed"
            operation_context.stream_error_code = exc.code
        yield _sse("error", public_error_envelope(request_id, exc))
    except Exception:  # noqa: BLE001
        if operation_context is not None:
            operation_context.stream_outcome = "failed"
            operation_context.stream_error_code = "internal_error"
        yield _sse(
            "error",
            public_error_envelope(
                request_id,
                PublicAPIError("internal_error", status_code=500),
            ),
        )
    finally:
        close = getattr(provider, "aclose", None)
        if callable(close):
            await close()


async def _checked_public_answer_events(
    *,
    db,
    user: User,
    request_id: str,
    query: str,
    prepared,
    operation_context: PublicOperationContext | None = None,
) -> AsyncIterator[str]:
    try:
        await recheck_public_scope(db, user=user, prepared=prepared)
    except asyncio.CancelledError:
        if operation_context is not None:
            operation_context.stream_outcome = "cancelled"
        raise
    except PublicAPIError as exc:
        if operation_context is not None:
            operation_context.stream_outcome = "failed"
            operation_context.stream_error_code = exc.code
        yield _sse("error", public_error_envelope(request_id, exc))
        return
    except Exception:  # noqa: BLE001
        if operation_context is not None:
            operation_context.stream_outcome = "failed"
            operation_context.stream_error_code = "internal_error"
        yield _sse(
            "error",
            public_error_envelope(
                request_id,
                PublicAPIError("internal_error", status_code=500),
            ),
        )
        return
    if operation_context is None:
        async for event in _public_answer_events(
            request_id=request_id,
            query=query,
            prepared=prepared,
        ):
            yield event
        return
    try:
        async with asyncio.timeout(settings.public_api_answer_max_seconds):
            async for event in _public_answer_events(
                request_id=request_id,
                query=query,
                prepared=prepared,
                operation_context=operation_context,
            ):
                yield event
    except TimeoutError:
        operation_context.stream_outcome = "failed"
        operation_context.stream_error_code = "upstream_failed"
        yield _sse(
            "error",
            public_error_envelope(
                request_id,
                PublicAPIError("upstream_failed", status_code=502),
            ),
        )


@router.get("/libraries", response_model=PublicLibrariesResponse)
async def public_libraries(
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("libraries.list")
    ),
) -> PublicLibrariesResponse:
    libraries, truncated = await list_public_libraries(
        db,
        user=user,
        **_operation_kwargs(operation_context),
    )
    return PublicLibrariesResponse(
        request_id=public_request_id(request),
        libraries=[public_library(row) for row in libraries],
        truncated=truncated,
    )


@router.post("/scopes/validate", response_model=PublicScopeValidationResponse)
async def public_scope_validation(
    body: PublicScopeValidateRequest,
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("scopes.validate")
    ),
) -> PublicScopeValidationResponse:
    scope, compatibility = await validate_public_scope(
        db,
        user=user,
        selection=body.scope,
        channels=tuple(body.channels),
        **_operation_kwargs(operation_context),
    )
    return PublicScopeValidationResponse(
        request_id=public_request_id(request),
        scope=scope.as_read(),
        compatibility=list(compatibility),
    )


@router.get(
    "/libraries/{slug}/documents/{document_id}",
    response_model=PublicDocumentResponse,
)
async def public_document(
    slug: str,
    document_id: uuid.UUID,
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("documents.get")
    ),
) -> PublicDocumentResponse:
    return await get_public_document(
        db,
        user=user,
        slug=slug,
        document_id=document_id,
        request_id=public_request_id(request),
        **_operation_kwargs(operation_context),
    )


@router.get(
    "/libraries/{slug}/entities/{entity_id}",
    response_model=PublicEntityResponse,
)
async def public_entity(
    slug: str,
    entity_id: uuid.UUID,
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("entities.get")
    ),
) -> PublicEntityResponse:
    return await get_public_entity(
        db,
        user=user,
        slug=slug,
        entity_id=entity_id,
        request_id=public_request_id(request),
        **_operation_kwargs(operation_context),
    )


@router.get(
    "/libraries/{slug}/relations/{relation_id}",
    response_model=PublicRelationResponse,
)
async def public_relation(
    slug: str,
    relation_id: uuid.UUID,
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("relations.get")
    ),
) -> PublicRelationResponse:
    return await get_public_relation(
        db,
        user=user,
        slug=slug,
        relation_id=relation_id,
        request_id=public_request_id(request),
        **_operation_kwargs(operation_context),
    )


@router.get(
    "/libraries/{slug}/evidence/{evidence_id}",
    response_model=PublicEvidenceResponse,
)
async def public_evidence(
    slug: str,
    evidence_id: uuid.UUID,
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("evidence.get")
    ),
) -> PublicEvidenceResponse:
    return await get_public_evidence(
        db,
        user=user,
        slug=slug,
        evidence_id=evidence_id,
        request_id=public_request_id(request),
        **_operation_kwargs(operation_context),
    )


@router.post("/entities/search", response_model=PublicEntitySearchResponse)
async def public_entity_search(
    body: PublicEntitySearchRequest,
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("entities.search")
    ),
) -> PublicEntitySearchResponse:
    scope, page = await search_public_entities(
        db,
        user=user,
        body=body,
        **_operation_kwargs(operation_context),
    )
    return PublicEntitySearchResponse(
        request_id=public_request_id(request),
        scope=scope.as_read(),
        items=page.items,
        next_cursor=page.next_cursor,
    )


@router.post("/relations/search", response_model=PublicRelationSearchResponse)
async def public_relation_search(
    body: PublicRelationSearchRequest,
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("relations.search")
    ),
) -> PublicRelationSearchResponse:
    scope, page = await search_public_relations(
        db,
        user=user,
        body=body,
        **_operation_kwargs(operation_context),
    )
    return PublicRelationSearchResponse(
        request_id=public_request_id(request),
        scope=scope.as_read(),
        items=page.items,
        next_cursor=page.next_cursor,
    )


@router.post("/retrieval", response_model=PublicRetrievalResponse)
async def public_retrieval(
    body: PublicRetrievalRequest,
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("retrieval.search")
    ),
) -> PublicRetrievalResponse:
    prepared = await prepare_public_retrieval(
        db,
        user=user,
        body=body,
        **_operation_kwargs(operation_context),
    )
    return build_public_retrieval_response(public_request_id(request), prepared)


@router.post("/answers", response_model=PublicAnswerResponse)
async def public_answer(
    body: PublicAnswerRequest,
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("answers.create")
    ),
) -> PublicAnswerResponse:
    return await generate_public_answer(
        db,
        user=user,
        body=body,
        request_id=public_request_id(request),
        **_operation_kwargs(operation_context),
    )


@router.post(
    "/answers/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Typed public answer event stream.",
            "content": {
                "text/event-stream": {
                    "example": (
                        "event: meta\n"
                        'data: {"contract_version":"public-answer-v1",'
                        '"request_id":"0123456789abcdef0123456789abcdef"}\n\n'
                        "event: delta\n"
                        'data: {"request_id":"0123456789abcdef0123456789abcdef",'
                        '"text":"Acme"}\n\n'
                        "event: result\n"
                        "data: {\"contract_version\":\"public-answer-v1\",...}\n\n"
                    )
                }
            },
        },
        **_ERROR_RESPONSES,
    },
)
async def public_answer_stream(
    body: PublicAnswerRequest,
    request: Request,
    _: None = Depends(require_public_api_v1_enabled),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    operation_context: PublicOperationContext | None = Depends(
        public_operation_dependency("answers.stream", is_stream=True)
    ),
) -> StreamingResponse:
    request_id = public_request_id(request)
    prepared = await prepare_public_retrieval(
        db,
        user=user,
        body=body,
        **_operation_kwargs(operation_context),
    )
    await recheck_public_scope(db, user=user, prepared=prepared)
    if prepared.records and (
        not settings.chat_enabled
        or not settings.chat_base_url
        or not settings.chat_model
    ):
        raise PublicAPIError("answer_unavailable", status_code=503)
    if prepared.records:
        await acquire_public_answer_capacity(prepared, operation_context)
    return StreamingResponse(
        _checked_public_answer_events(
            db=db,
            user=user,
            request_id=request_id,
            query=body.query,
            prepared=prepared,
            operation_context=operation_context,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-Id": request_id,
        },
    )


@router.api_route(
    "/{public_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def unknown_public_v1_route(public_path: str) -> None:  # noqa: ARG001
    raise PublicAPIError("not_found", status_code=404)
