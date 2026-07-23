from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.schemas.public_v1 import (
    PublicErrorDetailRead,
    PublicErrorEnvelope,
    PublicErrorRead,
)


log = logging.getLogger(__name__)
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

ERROR_MESSAGES = {
    "not_found": "The requested API capability was not found.",
    "authentication_required": "Authentication is required.",
    "scope_forbidden": "The selected knowledge scope is not available.",
    "scope_incompatible": "The selected knowledge libraries are incompatible.",
    "request_invalid": "The request does not match the public v1 contract.",
    "resource_not_found": "The requested resource was not found.",
    "service_unavailable": "The knowledge service is temporarily unavailable.",
    "answer_unavailable": "The answer service is temporarily unavailable.",
    "upstream_failed": "An upstream knowledge service failed.",
    "internal_error": "The request could not be completed.",
}


class PublicAPIError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        status_code: int,
        details: tuple[tuple[str, tuple[str, ...]], ...] = (),
    ) -> None:
        self.code = code if code in ERROR_MESSAGES else "internal_error"
        self.status_code = status_code
        self.details = _sanitize_details(details)
        super().__init__(ERROR_MESSAGES[self.code])


def _sanitize_details(
    details: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    result: list[tuple[str, tuple[str, ...]]] = []
    for slug, reasons in details[:20]:
        if not isinstance(slug, str) or _SLUG_RE.fullmatch(slug) is None:
            continue
        bounded = tuple(
            reason
            for reason in reasons[:20]
            if isinstance(reason, str) and _REASON_RE.fullmatch(reason) is not None
        )
        if bounded:
            result.append((slug, bounded))
    return tuple(result)


def public_request_id(request: Request) -> str:
    value = getattr(request.state, "public_v1_request_id", None)
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
        return value
    value = uuid.uuid4().hex
    request.state.public_v1_request_id = value
    return value


def public_error_envelope(request_id: str, error: PublicAPIError) -> PublicErrorEnvelope:
    return PublicErrorEnvelope(
        error=PublicErrorRead(
            code=error.code,
            request_id=request_id,
            message=ERROR_MESSAGES[error.code],
            details=[
                PublicErrorDetailRead(library_slug=slug, reason_codes=list(reasons))
                for slug, reasons in error.details
            ],
        )
    )


def public_error_response(request_id: str, error: PublicAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content=public_error_envelope(request_id, error).model_dump(mode="json"),
        headers={"X-Request-Id": request_id},
    )


def _http_error(error: HTTPException) -> PublicAPIError:
    code = {
        401: "authentication_required",
        403: "scope_forbidden",
        404: "resource_not_found",
        409: "scope_incompatible",
        422: "request_invalid",
        502: "upstream_failed",
        503: "service_unavailable",
    }.get(error.status_code, "internal_error")
    status_code = error.status_code if code != "internal_error" else 500
    return PublicAPIError(code, status_code=status_code)


class PublicV1Route(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            request_id = uuid.uuid4().hex
            request.state.public_v1_request_id = request_id
            try:
                response = await original(request)
            except PublicAPIError as exc:
                return public_error_response(request_id, exc)
            except RequestValidationError:
                return public_error_response(
                    request_id,
                    PublicAPIError("request_invalid", status_code=422),
                )
            except HTTPException as exc:
                return public_error_response(request_id, _http_error(exc))
            except Exception:  # noqa: BLE001
                log.error(
                    "public v1 request failed: request_id=%s path=%s",
                    request_id,
                    request.url.path,
                )
                return public_error_response(
                    request_id,
                    PublicAPIError("internal_error", status_code=500),
                )
            response.headers["X-Request-Id"] = request_id
            return response

        return route_handler


def split_public_delta(value: str, limit: int = 2048) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        return ()
    safe_limit = min(2048, max(1, limit))
    return tuple(value[index : index + safe_limit] for index in range(0, len(value), safe_limit))
