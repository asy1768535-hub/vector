from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.models.user import User
from app.schemas.federated_retrieval import (
    FederatedHitRead,
    FederatedLibraryProfileRead,
    FederatedLibraryTimingRead,
    FederatedRetrievalTestRequest,
    FederatedRetrievalTestResponse,
    FederatedSourceRead,
)
from app.services.federated_retrieval import run_federated_retrieval
from app.services.federated_retrieval_contracts import (
    FederatedRetrievalCommand,
    FederatedRetrievalError,
)
from app.services.library_compatibility_contracts import (
    FEDERATED_FUSION_CONTRACT_VERSION,
    FEDERATED_RETRIEVAL_CONTRACT_VERSION,
)
from app.services.organization_authorization import (
    OrganizationAdminContext,
    OrganizationAuthorizationError,
    resolve_organization_admin,
)


log = logging.getLogger(__name__)
router = APIRouter(tags=["federated-retrieval"])


@dataclass(frozen=True, slots=True)
class FederatedAdminRequestContext:
    user: User
    admin: OrganizationAdminContext


async def require_federated_organization_admin(
    organization_id: uuid.UUID,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> FederatedAdminRequestContext:
    if not settings.federated_retrieval_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    try:
        admin = await resolve_organization_admin(
            db,
            organization_id=organization_id,
            user=user,
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return FederatedAdminRequestContext(user=user, admin=admin)


def _federated_http_error(exc: FederatedRetrievalError) -> HTTPException:
    if exc.code == "federated_scope_forbidden":
        return HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if exc.code == "federated_scope_incompatible":
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": exc.code,
                "incompatibilities": [
                    {
                        "library_slug": item.library_slug,
                        "reason_codes": list(item.reason_codes),
                    }
                    for item in exc.incompatibilities
                ],
            },
        )
    if exc.code == "federated_request_invalid":
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)
    if exc.code == "federated_branch_failed":
        return HTTPException(status.HTTP_502_BAD_GATEWAY, exc.code)
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "federated_retrieval_failed")


def _response(
    body: FederatedRetrievalTestRequest,
    command: FederatedRetrievalCommand,
    result,
) -> FederatedRetrievalTestResponse:
    return FederatedRetrievalTestResponse(
        contract_version=FEDERATED_RETRIEVAL_CONTRACT_VERSION,
        fusion_contract_version=FEDERATED_FUSION_CONTRACT_VERSION,
        organization_id=command.organization_id,
        library_slugs=list(command.library_slugs),
        query=command.query,
        top_k=body.top_k,
        candidate_k=body.candidate_k,
        score_threshold=body.score_threshold,
        profiles=[
            FederatedLibraryProfileRead(
                library_id=profile.library.id,
                library_slug=profile.library.slug,
                library_name=profile.library.name,
                embedding_profile_sha256=profile.embedding.fingerprint or "",
                retrieval_profile_sha256=profile.retrieval.fingerprint or "",
            )
            for profile in result.assessment.profiles
        ],
        hits=[
            FederatedHitRead(
                rank=hit.rank,
                fusion_score=hit.fusion_score,
                library_id=hit.library_id,
                library_slug=hit.library_slug,
                library_name=hit.library_name,
                local_rank=hit.local_rank,
                local_score=hit.local_score,
                title=hit.title,
                content_excerpt=hit.content_excerpt,
                content_truncated=hit.content_truncated,
                source=FederatedSourceRead(
                    document_id=hit.source.document_id,
                    document_revision_id=hit.source.document_revision_id,
                    document_revision=hit.source.document_revision,
                    chunk_id=hit.source.chunk_id,
                    seq=hit.source.seq,
                    page=hit.source.page,
                    title_path=list(hit.source.title_path),
                    external_id=hit.source.external_id,
                    vector_score=hit.source.vector_score,
                    rerank_score=hit.source.rerank_score,
                    local_rrf_score=hit.source.local_rrf_score,
                    dense_rank=hit.source.dense_rank,
                    keyword_rank=hit.source.keyword_rank,
                    rewrite_sources=list(hit.source.rewrite_sources),
                ),
            )
            for hit in result.hits
        ],
        timings=[
            FederatedLibraryTimingRead(
                library_slug=item.library_slug,
                candidate_count=item.candidate_count,
                elapsed_ms=item.elapsed_ms,
            )
            for item in result.timings
        ],
        total_elapsed_ms=result.total_elapsed_ms,
    )


@router.post(
    "/organizations/{organization_id}/retrieval-tests",
    response_model=FederatedRetrievalTestResponse,
)
async def federated_retrieval_test(
    organization_id: uuid.UUID,
    body: FederatedRetrievalTestRequest,
    context: FederatedAdminRequestContext = Depends(
        require_federated_organization_admin
    ),
    db: AsyncSession = Depends(get_db),
) -> FederatedRetrievalTestResponse:
    command = FederatedRetrievalCommand(
        organization_id=organization_id,
        library_slugs=tuple(body.library_slugs),
        query=body.query,
        top_k=body.top_k,
        candidate_k=body.candidate_k,
        score_threshold=body.score_threshold,
    )
    try:
        result = await run_federated_retrieval(
            db,
            user=context.user,
            command=command,
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    except FederatedRetrievalError as exc:
        raise _federated_http_error(exc) from exc
    except Exception as exc:
        log.error(
            "federated retrieval failed without a stable error: organization_id=%s",
            organization_id,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "federated_retrieval_failed",
        ) from exc
    return _response(body, command, result)
