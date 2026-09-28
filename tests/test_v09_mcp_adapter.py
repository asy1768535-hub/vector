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
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import SecretStr, ValidationError

from app.mcp_adapter.client import MCPAdapterError, PublicV1Client
from app.mcp_adapter.auth import PublicAPIKeyVerifier
from app.mcp_adapter.cli import configure_http_transport
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
    settings = MCPAdapterSettings(_env_file=None)
    assert settings.enabled is False
    assert settings.transport == "stdio"
    assert settings.upload_enabled is False
    assert settings.max_upload_bytes == 10 * 1024 * 1024

    with pytest.raises(ValidationError, match="API key"):
        MCPAdapterSettings(_env_file=None, enabled=True)

    configured = _settings()
    assert "vkb_secret_value" not in repr(configured)
    assert "vkb_secret_value" not in configured.model_dump_json()


def test_http_mcp_requires_each_request_to_supply_its_own_key() -> None:
    settings = MCPAdapterSettings(
        _env_file=None,
        enabled=True,
        base_url="http://127.0.0.1:8000",
        transport="streamable-http",
        api_key=SecretStr(""),
    )
    assert not settings.api_key.get_secret_value()

    with pytest.raises(ValidationError, match="shared API key"):
        MCPAdapterSettings(
            _env_file=None,
            enabled=True,
            base_url="http://127.0.0.1:8000",
            transport="streamable-http",
            api_key=SecretStr("one-account-key"),
        )


@pytest.mark.asyncio
async def test_client_uses_the_request_key_instead_of_a_configured_service_key() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json=_libraries_response().model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PublicV1Client(_settings(), http_client, api_key="person-a-key")
        await client.list_libraries()

    assert seen == ["Bearer person-a-key"]


@pytest.mark.asyncio
async def test_http_mcp_verifies_each_users_key_without_caching_authority() -> None:
    seen: list[str] = []
    revoked = {"vk_alice"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        token = request.headers["authorization"].removeprefix("Bearer ")
        if token in revoked:
            return httpx.Response(401)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        verifier = PublicAPIKeyVerifier(_settings(), http_client)
        assert await verifier.verify_token("vk_alice") is None
        bob = await verifier.verify_token("vk_bob")
        assert bob is not None and bob.token == "vk_bob"
        revoked.add("vk_bob")
        assert await verifier.verify_token("vk_bob") is None

    assert seen == ["Bearer vk_alice", "Bearer vk_bob", "Bearer vk_bob"]


@pytest.mark.asyncio
async def test_http_mcp_verifier_rejects_missing_malformed_and_unavailable_credentials() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="unexpected response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        verifier = PublicAPIKeyVerifier(_settings(), http_client)
        assert await verifier.verify_token("") is None
        assert await verifier.verify_token("a" * 257) is None
        assert await verifier.verify_token("vk_invalid") is None


@pytest.mark.asyncio
async def test_http_mcp_keeps_two_users_credentials_and_libraries_separate() -> None:
    seen: list[tuple[str, str]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        seen.append((request.url.path, token))
        if token not in {"vk_alice", "vk_bob"}:
            return httpx.Response(401)
        if request.url.path == "/me/permissions":
            return httpx.Response(200, json=[])
        if request.url.path == "/libraries/alice-library/import-file":
            if token != "vk_alice":
                return httpx.Response(403)
            return httpx.Response(201, json=_upload_response().model_dump(mode="json"))
        payload = _libraries_response().model_dump(mode="json")
        payload["libraries"][0]["slug"] = "alice-library" if token == "vk_alice" else "bob-library"
        return httpx.Response(200, json=payload)

    settings = _settings(transport="streamable-http", api_key=SecretStr(""))
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
        verifier = PublicAPIKeyVerifier(settings, upstream_client)

        def current_user_client() -> PublicV1Client:
            access = get_access_token()
            assert access is not None
            return PublicV1Client(settings, upstream_client, api_key=access.token)

        server = create_mcp_server(
            client_factory=current_user_client,
            token_verifier=verifier,
            auth=AuthSettings(
                issuer_url="https://knowledge.example.test",
                resource_server_url=None,
                required_scopes=["mcp"],
            ),
            upload_enabled=True,
        )
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8001",
            ) as caller:
                request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "list_libraries", "arguments": {}},
                }
                headers = {
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
                }
                missing = await caller.post("/mcp", json=request, headers=headers)
                alice = await caller.post(
                    "/mcp", json=request, headers={**headers, "Authorization": "Bearer vk_alice"}
                )
                bob = await caller.post(
                    "/mcp", json=request, headers={**headers, "Authorization": "Bearer vk_bob"}
                )
                invalid = await caller.post(
                    "/mcp", json=request, headers={**headers, "Authorization": "Bearer vk_invalid"}
                )
                upload_request = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "upload_file",
                        "arguments": {
                            "library_slug": "alice-library",
                            "filename": "notes.txt",
                            "content_base64": "eA==",
                        },
                    },
                }
                alice_upload = await caller.post(
                    "/mcp", json=upload_request,
                    headers={**headers, "Authorization": "Bearer vk_alice"},
                )
                bob_upload = await caller.post(
                    "/mcp", json=upload_request,
                    headers={**headers, "Authorization": "Bearer vk_bob"},
                )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert alice.status_code == bob.status_code == 200
    assert "alice-library" in alice.text and "bob-library" not in alice.text
    assert "bob-library" in bob.text and "alice-library" not in bob.text
    assert alice_upload.status_code == bob_upload.status_code == 200
    assert alice_upload.json()["result"]["isError"] is False
    assert bob_upload.json()["result"]["isError"] is True
    assert ("/libraries/alice-library/import-file", "vk_alice") in seen
    assert ("/libraries/alice-library/import-file", "vk_bob") in seen
    assert ("/api/v1/libraries", "vk_alice") in seen
    assert ("/api/v1/libraries", "vk_bob") in seen


def test_mcp_settings_reject_cleartext_remote_upstream_and_public_bind() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        _settings(base_url="http://knowledge.example.test")
    with pytest.raises(ValidationError, match="loopback"):
        _settings(transport="streamable-http", http_host="0.0.0.0", api_key=SecretStr(""))

    local = _settings(
        base_url="http://127.0.0.1:8000",
        transport="streamable-http",
        api_key=SecretStr(""),
    )
    assert local.http_host == "127.0.0.1"

    private = _settings(
        base_url="http://vector-kb-api-release:8200",
        transport="streamable-http",
        http_host="0.0.0.0",
        private_network=True,
        public_host="knowledge.example.test",
        api_key=SecretStr(""),
    )
    assert private.private_network is True

    with pytest.raises(ValidationError, match="public DNS host"):
        _settings(
            transport="streamable-http",
            http_host="0.0.0.0",
            private_network=True,
            api_key=SecretStr(""),
        )


@pytest.mark.asyncio
async def test_private_network_accepts_authenticated_proxy_host_only() -> None:
    settings = _settings(
        transport="streamable-http",
        http_host="0.0.0.0",
        private_network=True,
        public_host="knowledge.example.test",
        api_key=SecretStr(""),
    )

    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
        server = create_mcp_server(
            client_factory=lambda: PublicV1Client(settings, upstream_client, api_key="vk_alice"),
            token_verifier=PublicAPIKeyVerifier(settings, upstream_client),
            auth=AuthSettings(
                issuer_url="https://knowledge.example.test",
                resource_server_url=None,
                required_scopes=["mcp"],
            ),
        )
        configure_http_transport(server, settings)
        app = server.streamable_http_app()
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        headers = {
            "Authorization": "Bearer vk_alice",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://knowledge.example.test",
            ) as caller:
                accepted = await caller.post("/mcp", json=request, headers=headers)
                wrong_origin = await caller.post(
                    "/mcp", json=request, headers={**headers, "Origin": "https://evil.test"}
                )
                wrong_host = await caller.post(
                    "/mcp", json=request, headers={**headers, "Host": "evil.test"}
                )
    assert accepted.status_code == 200
    assert wrong_origin.status_code == 403
    assert wrong_host.status_code == 421


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
