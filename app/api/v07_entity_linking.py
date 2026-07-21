from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import require_lib
from app.models.library import Library
from app.schemas.v07_entity_linking import (
    EntityLinkingErrorCode,
    EntityLinkingErrorResponse,
    EntityLinkingResolveRequest,
    EntityLinkingResolveResponse,
)
from app.services import entity_linking
from app.services.entity_linking_observability import (
    EntityLinkingObservation,
    EntityLinkingResultCode,
    emit_entity_linking_observation,
)


def _error_response(status_code: int, detail: EntityLinkingErrorCode) -> JSONResponse:
    body = EntityLinkingErrorResponse(detail=detail)
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


class SanitizedEntityLinkingRoute(APIRoute):
    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        route_handler = super().get_route_handler()

        async def sanitized_route_handler(request: Request) -> Response:
            started_ns = time.perf_counter_ns()
            try:
                return await route_handler(request)
            except RequestValidationError:
                emit_entity_linking_observation(
                    EntityLinkingObservation(
                        request_id=uuid.uuid4().hex,
                        result_code="entity_linking_invalid_request",
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        duration_us=(time.perf_counter_ns() - started_ns + 999) // 1000,
                    )
                )
                return _error_response(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "entity_linking_invalid_request",
                )

        return sanitized_route_handler


router = APIRouter(
    prefix="/libraries/{slug}/v07/entity-links",
    tags=["v0.7-entity-linking"],
    route_class=SanitizedEntityLinkingRoute,
)


_SERVICE_ERROR_STATUS: dict[entity_linking.EntityLinkingServiceErrorCode, int] = {
    "publication_changed": status.HTTP_409_CONFLICT,
    "entity_type_not_found": status.HTTP_409_CONFLICT,
    "entity_linking_limit_exceeded": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "graph_publication_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "graph_publication_invariant_failed": status.HTTP_503_SERVICE_UNAVAILABLE,
}


async def _rollback_quietly(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:
        pass


@router.post(
    "/resolve",
    response_model=EntityLinkingResolveResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": EntityLinkingErrorResponse},
        status.HTTP_409_CONFLICT: {"model": EntityLinkingErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": EntityLinkingErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": EntityLinkingErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": EntityLinkingErrorResponse},
        status.HTTP_504_GATEWAY_TIMEOUT: {"model": EntityLinkingErrorResponse},
    },
)
async def resolve_entity_links(
    body: EntityLinkingResolveRequest,
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> EntityLinkingResolveResponse | JSONResponse:
    started_ns = time.perf_counter_ns()
    request_id = uuid.uuid4().hex

    def observe(
        result_code: EntityLinkingResultCode,
        status_code: int,
        response: EntityLinkingResolveResponse | None = None,
        *,
        publication_recheck_failed: bool = False,
    ) -> None:
        counts = response.counts if response is not None else None
        emit_entity_linking_observation(
            EntityLinkingObservation(
                request_id=request_id,
                result_code=result_code,
                status_code=status_code,
                library_id=library.id,
                ontology_version_id=body.ontology_version_id,
                publication_id=(response.publication.id if response is not None else None),
                mention_count=len(body.mentions),
                linked_exact_count=(counts.linked_exact if counts is not None else 0),
                linked_lexical_count=(counts.linked_lexical if counts is not None else 0),
                ambiguous_count=(counts.ambiguous if counts is not None else 0),
                not_found_count=(counts.not_found if counts is not None else 0),
                candidate_count=(counts.candidates if counts is not None else 0),
                duration_us=(time.perf_counter_ns() - started_ns + 999) // 1000,
                publication_recheck_failed=publication_recheck_failed,
            )
        )

    if not settings.entity_linking_enabled:
        observe(
            "entity_linking_disabled",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "entity_linking_disabled",
        )

    try:
        async with asyncio.timeout(settings.entity_linking_timeout_seconds):
            response = await entity_linking.execute_entity_linking(
                db,
                library,
                body,
                config=settings,
            )
            observe("success", status.HTTP_200_OK, response)
            return response
    except entity_linking.EntityLinkingServiceError as exc:
        await _rollback_quietly(db)
        status_code = _SERVICE_ERROR_STATUS.get(exc.code)
        if status_code is None:
            observe(
                "entity_linking_internal_error",
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
            return _error_response(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "entity_linking_internal_error",
            )
        observe(
            exc.code,
            status_code,
            publication_recheck_failed=exc.code == "publication_changed",
        )
        return _error_response(status_code, exc.code)
    except TimeoutError:
        await _rollback_quietly(db)
        observe("entity_linking_timeout", status.HTTP_504_GATEWAY_TIMEOUT)
        return _error_response(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "entity_linking_timeout",
        )
    except Exception:
        await _rollback_quietly(db)
        observe(
            "entity_linking_internal_error",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "entity_linking_internal_error",
        )
