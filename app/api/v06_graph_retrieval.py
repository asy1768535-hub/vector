from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Coroutine, Sequence
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
from app.schemas.v06_graph_retrieval import (
    GraphRetrievalAmbiguousCandidate,
    GraphRetrievalErrorCode,
    GraphRetrievalErrorResponse,
    GraphRetrievalQueryRequest,
    GraphRetrievalQueryResponse,
)
from app.services import graph_retrieval
from app.services.graph_retrieval_observability import (
    GraphRetrievalObservation,
    GraphRetrievalResultCode,
    emit_graph_retrieval_observation,
)


def _error_response(
    status_code: int,
    detail: GraphRetrievalErrorCode,
    *,
    candidates: Sequence[GraphRetrievalAmbiguousCandidate] = (),
) -> JSONResponse:
    body = GraphRetrievalErrorResponse(
        detail=detail,
        candidates=list(candidates),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


class SanitizedGraphRetrievalRoute(APIRoute):
    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        route_handler = super().get_route_handler()

        async def sanitized_route_handler(request: Request) -> Response:
            started_ns = time.perf_counter_ns()
            try:
                return await route_handler(request)
            except RequestValidationError:
                emit_graph_retrieval_observation(
                    GraphRetrievalObservation(
                        request_id=uuid.uuid4().hex,
                        result_code="graph_retrieval_invalid_request",
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        duration_us=(time.perf_counter_ns() - started_ns + 999) // 1000,
                    )
                )
                return _error_response(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "graph_retrieval_invalid_request",
                )

        return sanitized_route_handler


router = APIRouter(
    prefix="/libraries/{slug}/v06/graph",
    tags=["v0.6-graph-retrieval"],
    route_class=SanitizedGraphRetrievalRoute,
)


_SERVICE_ERROR_STATUS: dict[GraphRetrievalErrorCode, int] = {
    "seed_not_found": status.HTTP_404_NOT_FOUND,
    "seed_ambiguous": status.HTTP_409_CONFLICT,
    "publication_changed": status.HTTP_409_CONFLICT,
    "relation_type_not_found": status.HTTP_409_CONFLICT,
    "graph_retrieval_limit_exceeded": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "graph_publication_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "graph_publication_invariant_failed": status.HTTP_503_SERVICE_UNAVAILABLE,
}


async def _rollback_quietly(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:
        pass


@router.post(
    "/query",
    response_model=GraphRetrievalQueryResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": GraphRetrievalErrorResponse},
        status.HTTP_409_CONFLICT: {"model": GraphRetrievalErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": GraphRetrievalErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": GraphRetrievalErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": GraphRetrievalErrorResponse},
        status.HTTP_504_GATEWAY_TIMEOUT: {"model": GraphRetrievalErrorResponse},
    },
)
async def query_published_graph(
    _request: Request,
    body: GraphRetrievalQueryRequest,
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> GraphRetrievalQueryResponse | JSONResponse:
    started_ns = time.perf_counter_ns()
    request_id = uuid.uuid4().hex
    library_id = library.id

    def observe(
        result_code: GraphRetrievalResultCode,
        status_code: int,
        response: GraphRetrievalQueryResponse | None = None,
    ) -> None:
        emit_graph_retrieval_observation(
            GraphRetrievalObservation(
                request_id=request_id,
                result_code=result_code,
                status_code=status_code,
                library_id=library_id,
                ontology_version_id=body.ontology_version_id,
                publication_id=(response.publication.id if response is not None else None),
                max_hops=body.max_hops,
                include_evidence_locators=body.include_evidence_locators,
                node_count=(response.counts.nodes if response is not None else 0),
                relation_count=(response.counts.relations if response is not None else 0),
                evidence_locator_count=(
                    response.counts.evidence_locators if response is not None else 0
                ),
                truncated_nodes=(response.truncated.nodes if response is not None else False),
                truncated_relations=(
                    response.truncated.relations if response is not None else False
                ),
                truncated_evidence=(
                    response.truncated.evidence if response is not None else False
                ),
                duration_us=(time.perf_counter_ns() - started_ns + 999) // 1000,
            )
        )

    if not settings.graph_retrieval_enabled:
        observe(
            "graph_retrieval_disabled",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "graph_retrieval_disabled",
        )

    try:
        async with asyncio.timeout(settings.graph_retrieval_timeout_seconds):
            response = await graph_retrieval.execute_graph_retrieval_query(
                db,
                library,
                body,
                config=settings,
            )
            observe("success", status.HTTP_200_OK, response)
            return response
    except graph_retrieval.GraphRetrievalServiceError as exc:
        await _rollback_quietly(db)
        status_code = _SERVICE_ERROR_STATUS.get(exc.code)
        if status_code is None:
            observe(
                "graph_retrieval_internal_error",
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
            return _error_response(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "graph_retrieval_internal_error",
            )
        observe(exc.code, status_code)
        return _error_response(
            status_code,
            exc.code,
            candidates=exc.candidates,
        )
    except TimeoutError:
        await _rollback_quietly(db)
        observe(
            "graph_retrieval_timeout",
            status.HTTP_504_GATEWAY_TIMEOUT,
        )
        return _error_response(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "graph_retrieval_timeout",
        )
    except Exception:
        await _rollback_quietly(db)
        observe(
            "graph_retrieval_internal_error",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "graph_retrieval_internal_error",
        )
