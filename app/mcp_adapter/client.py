from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from app.mcp_adapter.config import MCPAdapterSettings
from app.schemas.admin import PermissionMatrixRow
from app.schemas.dify import DifyRetrievalRequest, DifyRetrievalResponse
from app.schemas.documents import ImportFileResponse

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


class MCPAdapterError(RuntimeError):
    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        suffix = f" status_code={status_code}" if status_code is not None else ""
        super().__init__(f"{code}{suffix}")


class VectorKnowledgeClient:
    def __init__(
        self,
        settings: MCPAdapterSettings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = str(settings.base_url).rstrip("/")
        self._authorization = f"Bearer {settings.api_key.get_secret_value().strip()}"
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
        json_body: Mapping[str, Any] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
    ) -> ResponseModelT:
        try:
            response = await self._http_client.request(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": self._authorization},
                json=dict(json_body) if json_body is not None else None,
                files=files,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise MCPAdapterError("upstream_timeout") from None
        except httpx.RequestError:
            raise MCPAdapterError("upstream_unavailable") from None

        if response.status_code in {401, 403}:
            raise MCPAdapterError("upstream_forbidden", status_code=response.status_code)
        if not response.is_success:
            raise MCPAdapterError("upstream_failed", status_code=response.status_code)

        try:
            return response_model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise MCPAdapterError(
                "upstream_invalid_response",
                status_code=response.status_code,
            ) from None

    async def search_knowledge(
        self,
        request: DifyRetrievalRequest,
    ) -> DifyRetrievalResponse:
        return await self._request(
            "POST",
            "/retrieval",
            DifyRetrievalResponse,
            json_body=request.model_dump(mode="json", exclude_none=True),
        )

    async def list_permissions(self) -> list[PermissionMatrixRow]:
        result = await self._http_client.request(
            "GET",
            f"{self._base_url}/me/permissions",
            headers={"Authorization": self._authorization},
            timeout=self._timeout,
            follow_redirects=False,
        )
        if result.status_code in {401, 403}:
            raise MCPAdapterError("upstream_forbidden", status_code=result.status_code)
        if not result.is_success:
            raise MCPAdapterError("upstream_failed", status_code=result.status_code)
        try:
            return [PermissionMatrixRow.model_validate(row) for row in result.json()]
        except (ValueError, TypeError, ValidationError):
            raise MCPAdapterError(
                "upstream_invalid_response",
                status_code=result.status_code,
            ) from None

    async def upload_file(
        self,
        knowledge_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> ImportFileResponse:
        library_slug = quote(knowledge_id, safe="")
        return await self._request(
            "POST",
            f"/libraries/{library_slug}/import-file",
            ImportFileResponse,
            files={"file": (filename, content, content_type)},
        )
