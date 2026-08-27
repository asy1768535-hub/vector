from __future__ import annotations

import base64
import binascii

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from app.mcp_adapter.client import MCPAdapterError, VectorKnowledgeClient
from app.schemas.admin import PermissionMatrixRow
from app.schemas.dify import (
    DifyRetrievalRequest,
    DifyRetrievalResponse,
    MetadataConditionGroup,
    RetrievalSetting,
)
from app.schemas.documents import ImportFileResponse

_UPLOAD_CONTENT_TYPES = {
    ".txt": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
}


def _decode_upload(filename: str, content_base64: str, max_bytes: int) -> tuple[bytes, str]:
    if (
        not filename
        or len(filename) > 255
        or "/" in filename
        or "\\" in filename
        or not (content_type := _UPLOAD_CONTENT_TYPES.get("." + filename.rsplit(".", 1)[-1].lower()))
    ):
        raise MCPAdapterError("request_invalid")
    if len(content_base64) > 4 * ((max_bytes + 2) // 3):
        raise MCPAdapterError("request_invalid")
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise MCPAdapterError("request_invalid") from None
    if not content or len(content) > max_bytes:
        raise MCPAdapterError("request_invalid")
    return content, content_type


def _tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, MCPAdapterError):
        return ToolError(exc.code)
    if isinstance(exc, ValidationError):
        return ToolError("request_invalid")
    return ToolError("tool_failed")


def create_mcp_server(
    client: VectorKnowledgeClient,
    *,
    host: str = "127.0.0.1",
    port: int = 8001,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    max_upload_bytes: int = 10 * 1024 * 1024,
) -> FastMCP:
    server = FastMCP(
        name="Vector Knowledge",
        instructions=(
            "Search authorized vector knowledge libraries and upload UTF-8 text, JSON, "
            "or CSV files. Existing service permissions are always enforced."
        ),
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts or ["127.0.0.1", "localhost"],
            allowed_origins=allowed_origins or [],
        ),
    )

    @server.tool()
    async def search_knowledge(
        knowledge_id: str,
        query: str,
        top_k: int = 5,
        score_threshold: float = 0.0,
        metadata_condition: MetadataConditionGroup | None = None,
    ) -> DifyRetrievalResponse:
        """Search one authorized knowledge library and return matching chunks."""
        try:
            request = DifyRetrievalRequest(
                knowledge_id=knowledge_id,
                query=query,
                retrieval_setting=RetrievalSetting(
                    top_k=top_k,
                    score_threshold=score_threshold,
                ),
                metadata_condition=metadata_condition,
            )
            return await client.search_knowledge(request)
        except Exception as exc:  # noqa: BLE001
            raise _tool_error(exc) from None

    @server.tool()
    async def list_permissions() -> list[PermissionMatrixRow]:
        """List library permissions visible to the configured API key."""
        try:
            return await client.list_permissions()
        except Exception as exc:  # noqa: BLE001
            raise _tool_error(exc) from None

    @server.tool()
    async def upload_file(
        knowledge_id: str,
        filename: str,
        content_base64: str,
    ) -> ImportFileResponse:
        """Upload one base64-encoded UTF-8 .txt, .json, or .csv file to an authorized library."""
        try:
            content, content_type = _decode_upload(
                filename,
                content_base64,
                max_upload_bytes,
            )
            return await client.upload_file(
                knowledge_id,
                filename,
                content,
                content_type,
            )
        except Exception as exc:  # noqa: BLE001
            raise _tool_error(exc) from None

    return server
