from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.mcp_adapter.config import MCPAdapterSettings
from app.schemas.admin import PermissionMatrixRow
from app.schemas.documents import ImportFileResponse
from app.schemas.public_v1 import (
    PublicAnswerRequest,
    PublicAnswerResponse,
    PublicDocumentResponse,
    PublicEntityResponse,
    PublicEntitySearchRequest,
    PublicEntitySearchResponse,
    PublicErrorRead,
    PublicEvidenceResponse,
    LibrarySlug,
    PublicLibrariesResponse,
    PublicRelationResponse,
    PublicRelationSearchRequest,
    PublicRelationSearchResponse,
    PublicRetrievalRequest,
    PublicRetrievalResponse,
    PublicScopeSelection,
    PublicScopeValidateRequest,
    PublicScopeValidationResponse,
)

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)
RequestBody = BaseModel | Mapping[str, Any]
_LIBRARY_SLUG = TypeAdapter(LibrarySlug)
_PERMISSION_ROWS = TypeAdapter(list[PermissionMatrixRow])


class MCPAdapterError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        request_id: str | None = None,
        status_code: int | None = None,
        reason_codes: Sequence[str] = (),
    ) -> None:
        self.code = code
        self.request_id = request_id
        self.status_code = status_code
        self.reason_codes = tuple(reason_codes[:20])
        parts = [code]
        if request_id:
            parts.append(f"request_id={request_id}")
        if self.reason_codes:
            parts.append(f"reasons={','.join(self.reason_codes)}")
        super().__init__(" ".join(parts))


class PublicV1Client:
    def __init__(
        self,
        settings: MCPAdapterSettings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        base_url = str(settings.base_url).rstrip("/")
        self._service_base_url = base_url.removesuffix("/api/v1")
        self._base_url = (
            base_url if base_url.endswith("/api/v1") else f"{base_url}/api/v1"
        )
        self._authorization = (
            f"Bearer {settings.api_key.get_secret_value().strip()}"
        )
        self._timeout = settings.timeout_seconds
        self._http_client = http_client or httpx.AsyncClient()
        self._owns_http_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        response_model: type[ResponseModelT],
        *,
        json_body: dict[str, Any] | None = None,
    ) -> ResponseModelT:
        try:
            response = await self._http_client.request(
                method,
                f"{self._base_url}{path}",
                headers={
                    "Authorization": self._authorization,
                    "X-Vector-KB-Client": "mcp-adapter",
                },
                json=json_body,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise MCPAdapterError("upstream_timeout") from None
        except httpx.RequestError:
            raise MCPAdapterError("upstream_unavailable") from None

        if not response.is_success:
            raise self._public_error(response)

        try:
            return response_model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise MCPAdapterError(
                "upstream_invalid_response",
                status_code=response.status_code,
            ) from None

    @staticmethod
    def _public_error(response: httpx.Response) -> MCPAdapterError:
        try:
            payload = response.json()
            error = PublicErrorRead.model_validate(payload["error"])
        except (KeyError, TypeError, ValueError, ValidationError):
            return MCPAdapterError(
                "upstream_failed",
                status_code=response.status_code,
            )
        reasons = [
            reason
            for detail in error.details
            for reason in detail.reason_codes
        ]
        return MCPAdapterError(
            error.code,
            request_id=error.request_id,
            status_code=response.status_code,
            reason_codes=reasons,
        )

    @staticmethod
    def _body(body: RequestBody, model: type[BaseModel]) -> dict[str, Any]:
        validated = model.model_validate(body)
        return validated.model_dump(mode="json")

    @staticmethod
    def _slug(value: str) -> str:
        try:
            return _LIBRARY_SLUG.validate_python(value)
        except ValidationError:
            raise MCPAdapterError("library_slug_invalid") from None

    async def list_libraries(self) -> PublicLibrariesResponse:
        return await self._request("GET", "/libraries", PublicLibrariesResponse)

    async def list_permissions(self) -> list[PermissionMatrixRow]:
        try:
            response = await self._http_client.request(
                "GET",
                f"{self._service_base_url}/me/permissions",
                headers={
                    "Authorization": self._authorization,
                    "X-Vector-KB-Client": "mcp-adapter",
                },
                timeout=self._timeout,
                follow_redirects=False,
            )
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise MCPAdapterError("upstream_timeout") from None
        except httpx.RequestError:
            raise MCPAdapterError("upstream_unavailable") from None
        if not response.is_success:
            code = {
                401: "authentication_required",
                403: "permission_forbidden",
            }.get(response.status_code, "permission_lookup_failed")
            raise MCPAdapterError(code, status_code=response.status_code)
        try:
            return _PERMISSION_ROWS.validate_python(response.json())
        except (ValueError, ValidationError):
            raise MCPAdapterError(
                "upstream_invalid_response",
                status_code=response.status_code,
            ) from None

    async def validate_scope(
        self,
        *,
        scope: PublicScopeSelection,
        channels: list[str],
    ) -> PublicScopeValidationResponse:
        body = PublicScopeValidateRequest(scope=scope, channels=channels)
        return await self._request(
            "POST",
            "/scopes/validate",
            PublicScopeValidationResponse,
            json_body=body.model_dump(mode="json"),
        )

    async def get_document(
        self,
        slug: str,
        document_id: uuid.UUID,
    ) -> PublicDocumentResponse:
        slug = self._slug(slug)
        return await self._request(
            "GET",
            f"/libraries/{slug}/documents/{document_id}",
            PublicDocumentResponse,
        )

    async def get_entity(
        self,
        slug: str,
        entity_id: uuid.UUID,
    ) -> PublicEntityResponse:
        slug = self._slug(slug)
        return await self._request(
            "GET",
            f"/libraries/{slug}/entities/{entity_id}",
            PublicEntityResponse,
        )

    async def get_relation(
        self,
        slug: str,
        relation_id: uuid.UUID,
    ) -> PublicRelationResponse:
        slug = self._slug(slug)
        return await self._request(
            "GET",
            f"/libraries/{slug}/relations/{relation_id}",
            PublicRelationResponse,
        )

    async def get_evidence(
        self,
        slug: str,
        evidence_id: uuid.UUID,
    ) -> PublicEvidenceResponse:
        slug = self._slug(slug)
        return await self._request(
            "GET",
            f"/libraries/{slug}/evidence/{evidence_id}",
            PublicEvidenceResponse,
        )

    async def search_entities(
        self,
        body: RequestBody,
    ) -> PublicEntitySearchResponse:
        return await self._request(
            "POST",
            "/entities/search",
            PublicEntitySearchResponse,
            json_body=self._body(body, PublicEntitySearchRequest),
        )

    async def search_relations(
        self,
        body: RequestBody,
    ) -> PublicRelationSearchResponse:
        return await self._request(
            "POST",
            "/relations/search",
            PublicRelationSearchResponse,
            json_body=self._body(body, PublicRelationSearchRequest),
        )

    async def retrieve(
        self,
        body: RequestBody,
    ) -> PublicRetrievalResponse:
        return await self._request(
            "POST",
            "/retrieval",
            PublicRetrievalResponse,
            json_body=self._body(body, PublicRetrievalRequest),
        )

    async def answer(
        self,
        body: RequestBody,
    ) -> PublicAnswerResponse:
        return await self._request(
            "POST",
            "/answers",
            PublicAnswerResponse,
            json_body=self._body(body, PublicAnswerRequest),
        )

    async def upload_file(
        self,
        slug: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> ImportFileResponse:
        slug = self._slug(slug)
        try:
            response = await self._http_client.request(
                "POST",
                f"{self._service_base_url}/libraries/{slug}/import-file",
                headers={
                    "Authorization": self._authorization,
                    "X-Vector-KB-Client": "mcp-adapter",
                },
                files={"file": (filename, content, content_type)},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise MCPAdapterError("upstream_timeout") from None
        except httpx.RequestError:
            raise MCPAdapterError("upstream_unavailable") from None

        if not response.is_success:
            code = {
                401: "authentication_required",
                403: "library_forbidden",
                404: "library_not_found",
                409: "upload_conflict",
                413: "upload_too_large",
                415: "upload_type_unsupported",
                422: "upload_invalid",
            }.get(response.status_code, "upload_failed")
            raise MCPAdapterError(code, status_code=response.status_code)
        try:
            return ImportFileResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            raise MCPAdapterError(
                "upstream_invalid_response",
                status_code=response.status_code,
            ) from None
