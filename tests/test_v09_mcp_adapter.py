from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import SecretStr, ValidationError

from app.mcp_adapter.client import MCPAdapterError, PublicV1Client
from app.mcp_adapter.config import MCPAdapterSettings
from app.mcp_adapter.server import create_mcp_server
from app.schemas.documents import ImportFileResponse
from app.schemas.public_v1 import (
    PublicEntitySearchRequest,
    PublicLibrariesResponse,
    PublicLibraryRead,
    PublicRetrievalRequest,
    PublicScopeSelection,
)


REQUEST_ID = "0123456789abcdef0123456789abcdef"
LIBRARY_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
ORGANIZATION_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")


def _settings(**overrides: object) -> MCPAdapterSettings:
    values: dict[str, object] = {
        "enabled": True,
        "base_url": "https://knowledge.example.test",
        "api_key": SecretStr("vkb_secret_value"),
        "timeout_seconds": 12,
    }
    values.update(overrides)
    return MCPAdapterSettings(**values)


def _libraries_response() -> PublicLibrariesResponse:
    return PublicLibrariesResponse(
        request_id=REQUEST_ID,
        libraries=[
            PublicLibraryRead(
                id=LIBRARY_ID,
                organization_id=ORGANIZATION_ID,
                slug="projects",
                name="Projects",
                index_state="ready",
            )
        ],
        truncated=False,
    )


def _upload_response() -> ImportFileResponse:
    return ImportFileResponse.model_validate(
        {
            "status": "success",
            "imported_count": 1,
            "failed_count": 0,
            "documents": [
                {
                    "document_id": "00000000-0000-4000-8000-000000000004",
                    "title": "notes.txt",
                    "chunk_count": 1,
                    "status": "pending",
                    "job_id": "00000000-0000-4000-8000-000000000005",
                }
            ],
            "errors": [],
        }
    )


def test_mcp_settings_are_default_off_and_require_secret_when_enabled() -> None:
    settings = MCPAdapterSettings()
    assert settings.enabled is False
    assert settings.transport == "stdio"
    assert settings.upload_enabled is False
    assert settings.max_upload_bytes == 10 * 1024 * 1024

    with pytest.raises(ValidationError, match="API key"):
        MCPAdapterSettings(enabled=True)

    configured = _settings()
    assert "vkb_secret_value" not in repr(configured)
    assert "vkb_secret_value" not in configured.model_dump_json()


def test_mcp_settings_reject_cleartext_remote_upstream_and_public_bind() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        _settings(base_url="http://knowledge.example.test")
    with pytest.raises(ValidationError, match="loopback"):
        _settings(transport="streamable-http", http_host="0.0.0.0")

    local = _settings(
        base_url="http://127.0.0.1:8000",
        transport="streamable-http",
    )
    assert local.http_host == "127.0.0.1"


@pytest.mark.asyncio
async def test_client_sends_bearer_to_normalized_public_v1_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_libraries_response().model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PublicV1Client(_settings(base_url="https://knowledge.example.test/api/v1/"), http_client)
        result = await client.list_libraries()

    assert result == _libraries_response()
    assert seen[0].url == "https://knowledge.example.test/api/v1/libraries"
    assert seen[0].headers["authorization"] == "Bearer vkb_secret_value"


@pytest.mark.asyncio
async def test_client_preserves_public_error_without_leaking_body_or_secret() -> None:
    secret_body = "internal response body vkb_secret_value"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "library_forbidden",
                    "request_id": REQUEST_ID,
                    "message": "The selected knowledge library is unavailable.",
                    "details": [],
                },
                "ignored": secret_body,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PublicV1Client(_settings(), http_client)
        with pytest.raises(MCPAdapterError) as caught:
            await client.list_libraries()

    rendered = str(caught.value)
    assert caught.value.code == "library_forbidden"
    assert caught.value.request_id == REQUEST_ID
    assert "vkb_secret_value" not in rendered
    assert "internal response body" not in rendered
    assert "knowledge.example.test" not in rendered


@pytest.mark.asyncio
async def test_client_maps_malformed_timeout_and_transport_failures_safely() -> None:
    responses = [
        httpx.Response(502, text="private upstream body"),
        httpx.Response(200, text="not-json"),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PublicV1Client(_settings(), http_client)
        with pytest.raises(MCPAdapterError, match="upstream_failed"):
            await client.list_libraries()
        with pytest.raises(MCPAdapterError, match="upstream_invalid_response"):
            await client.list_libraries()

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret target", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout_handler)
    ) as http_client:
        client = PublicV1Client(_settings(), http_client)
        with pytest.raises(MCPAdapterError, match="upstream_timeout") as caught:
            await client.list_libraries()
        assert caught.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_client_does_not_swallow_cancellation() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PublicV1Client(_settings(), http_client)
        with pytest.raises(asyncio.CancelledError):
            await client.list_libraries()


@pytest.mark.asyncio
async def test_client_uploads_to_permission_checked_library_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=_upload_response().model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PublicV1Client(
            _settings(base_url="https://knowledge.example.test/api/v1"),
            http_client,
        )
        result = await client.upload_file(
            "projects",
            "notes.txt",
            b"release notes",
            "text/plain",
        )

    assert result == _upload_response()
    assert seen[0].method == "POST"
    assert seen[0].url == "https://knowledge.example.test/libraries/projects/import-file"
    assert seen[0].headers["authorization"] == "Bearer vkb_secret_value"
    assert b'filename="notes.txt"' in seen[0].content
    assert b"release notes" in seen[0].content


@pytest.mark.asyncio
async def test_client_lists_service_credential_permissions() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "library_slug": "projects",
                    "library_name": "Projects",
                    "actions": ["insert", "read"],
                    "organization_id": str(ORGANIZATION_ID),
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PublicV1Client(_settings(), http_client)
        result = await client.list_permissions()

    assert result[0].library_slug == "projects"
    assert result[0].actions == ["insert", "read"]
    assert seen[0].url == "https://knowledge.example.test/me/permissions"
    assert seen[0].headers["authorization"] == "Bearer vkb_secret_value"


@pytest.mark.asyncio
async def test_client_upload_errors_do_not_leak_upstream_body() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="private upload details vkb_secret_value")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PublicV1Client(_settings(), http_client)
        with pytest.raises(MCPAdapterError, match="library_forbidden") as caught:
            await client.upload_file("projects", "notes.txt", b"x", "text/plain")

    assert "private upload details" not in str(caught.value)
    assert "vkb_secret_value" not in str(caught.value)


@pytest.mark.asyncio
async def test_client_methods_match_frozen_http_contracts() -> None:
    client = PublicV1Client(_settings(), AsyncMock())
    client._request = AsyncMock(return_value=_libraries_response())
    scope = PublicScopeSelection(library_slugs=["projects"])
    entity_request = PublicEntitySearchRequest(scope=scope, query="Acme", limit=25)
    retrieval_request = PublicRetrievalRequest(
        scope=scope,
        query="Who invested?",
        top_k=8,
        candidate_k=20,
        score_threshold=0.2,
    )
    object_id = uuid.UUID("00000000-0000-4000-8000-000000000003")

    await client.list_libraries()
    await client.validate_scope(scope=scope, channels=["text", "graph"])
    await client.get_document("projects", object_id)
    await client.get_entity("projects", object_id)
    await client.get_relation("projects", object_id)
    await client.get_evidence("projects", object_id)
    await client.search_entities(entity_request)
    await client.search_relations(entity_request.model_dump())
    await client.retrieve(retrieval_request)
    await client.answer(retrieval_request.model_dump())

    calls = client._request.await_args_list
    assert [(call.args[0], call.args[1]) for call in calls] == [
        ("GET", "/libraries"),
        ("POST", "/scopes/validate"),
        ("GET", f"/libraries/projects/documents/{object_id}"),
        ("GET", f"/libraries/projects/entities/{object_id}"),
        ("GET", f"/libraries/projects/relations/{object_id}"),
        ("GET", f"/libraries/projects/evidence/{object_id}"),
        ("POST", "/entities/search"),
        ("POST", "/relations/search"),
        ("POST", "/retrieval"),
        ("POST", "/answers"),
    ]
    assert calls[1].kwargs["json_body"] == {
        "scope": {"library_slugs": ["projects"], "scope_id": None},
        "channels": ["text", "graph"],
    }
    assert calls[6].kwargs["json_body"]["limit"] == 25
    assert calls[8].kwargs["json_body"]["top_k"] == 8


@pytest.mark.asyncio
async def test_client_rejects_unbounded_library_slug_before_network() -> None:
    http_client = AsyncMock(spec=httpx.AsyncClient)
    client = PublicV1Client(_settings(), http_client)

    with pytest.raises(MCPAdapterError, match="library_slug_invalid"):
        await client.get_document("../hidden", LIBRARY_ID)

    http_client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_fastmcp_discovers_bounded_tools_and_read_resources() -> None:
    client = AsyncMock(spec=PublicV1Client)
    client.list_libraries.return_value = _libraries_response()
    server = create_mcp_server(client)

    tools = await server.list_tools()
    resources = await server.list_resources()
    templates = await server.list_resource_templates()
    tool_names = {tool.name for tool in tools}

    assert tool_names == {
        "list_libraries",
        "list_permissions",
        "validate_scope",
        "get_document",
        "get_entity",
        "get_relation",
        "get_evidence",
        "search_entities",
        "search_relations",
        "retrieve",
        "search_knowledge",
        "answer",
    }
    assert {str(resource.uri) for resource in resources} == {"vector-kb://libraries"}
    assert len(templates) == 4
    schemas = json.dumps([tool.inputSchema for tool in tools]).lower()
    assert "api_key" not in schemas
    assert "authorization" not in schemas

    _, structured = await server.call_tool("list_libraries", {})
    assert structured["contract_version"] == "public-libraries-v1"
    assert structured["libraries"][0]["slug"] == "projects"


@pytest.mark.asyncio
async def test_fastmcp_discovers_and_calls_upload_only_when_enabled() -> None:
    client = AsyncMock(spec=PublicV1Client)
    client.upload_file.return_value = _upload_response()
    server = create_mcp_server(client, upload_enabled=True, max_upload_bytes=32)

    tools = await server.list_tools()
    assert "upload_file" in {tool.name for tool in tools}

    _, structured = await server.call_tool(
        "upload_file",
        {
            "library_slug": "projects",
            "filename": "notes.txt",
            "content_base64": base64.b64encode(b"release notes").decode("ascii"),
        },
    )

    assert structured["imported_count"] == 1
    client.upload_file.assert_awaited_once_with(
        "projects",
        "notes.txt",
        b"release notes",
        "text/plain",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content_base64", "error"),
    [
        ("../notes.txt", "eA==", "upload_invalid"),
        ("notes.exe", "eA==", "upload_invalid"),
        ("notes.txt", "!!!", "upload_invalid"),
        ("notes.txt", "eHl6", "upload_too_large"),
    ],
)
async def test_fastmcp_rejects_invalid_uploads_before_network(
    filename: str,
    content_base64: str,
    error: str,
) -> None:
    client = AsyncMock(spec=PublicV1Client)
    server = create_mcp_server(client, upload_enabled=True, max_upload_bytes=2)

    with pytest.raises(ToolError, match=error):
        await server.call_tool(
            "upload_file",
            {
                "library_slug": "projects",
                "filename": filename,
                "content_base64": content_base64,
            },
        )

    client.upload_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_fastmcp_surfaces_only_sanitized_adapter_errors() -> None:
    client = AsyncMock(spec=PublicV1Client)
    client.list_libraries.side_effect = MCPAdapterError(
        "library_forbidden",
        request_id=REQUEST_ID,
        status_code=403,
    )
    server = create_mcp_server(client)

    with pytest.raises(ToolError) as caught:
        await server.call_tool("list_libraries", {})

    assert "library_forbidden" in str(caught.value)
    assert REQUEST_ID in str(caught.value)
    assert "vkb_secret_value" not in str(caught.value)


@pytest.mark.asyncio
async def test_official_client_session_discovers_and_calls_adapter() -> None:
    client = AsyncMock(spec=PublicV1Client)
    client.list_libraries.return_value = _libraries_response()
    server = create_mcp_server(client)

    async with create_connected_server_and_client_session(server) as session:
        tools = await session.list_tools()
        resources = await session.list_resources()
        templates = await session.list_resource_templates()
        result = await session.call_tool("list_libraries")

    assert "list_libraries" in {tool.name for tool in tools.tools}
    assert {str(resource.uri) for resource in resources.resources} == {
        "vector-kb://libraries"
    }
    assert len(templates.resourceTemplates) == 4
    assert result.isError is False
    assert result.structuredContent["libraries"][0]["slug"] == "projects"


def test_mcp_package_has_no_domain_queries_or_write_surface() -> None:
    package = Path("app/mcp_adapter")
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package.rglob("*.py")
    ).lower()

    forbidden_imports = (
        "app.models",
        "app.db",
        "sqlalchemy",
        "app.services.public_v1",
        "app.services.graph",
        "app.services.retrieval",
    )
    forbidden_writes = (
        '"/publications',
        '"/governance',
        '"/schema',
        '"delete"',
        '"patch"',
        '"put"',
    )
    assert not any(value in text for value in forbidden_imports)
    assert not any(value in text for value in forbidden_writes)
