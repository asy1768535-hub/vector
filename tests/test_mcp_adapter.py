from __future__ import annotations

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.mcp_adapter.client import VectorKnowledgeClient
from app.mcp_adapter.config import MCPAdapterSettings
from app.mcp_adapter.server import create_mcp_server


def _settings() -> MCPAdapterSettings:
    return MCPAdapterSettings(api_key="vk_test", base_url="http://vector.local")


@pytest.mark.asyncio
async def test_mcp_server_exposes_expected_tools():
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    client = VectorKnowledgeClient(_settings(), http_client=http_client)
    server = create_mcp_server(client)

    tools = {tool.name for tool in server._tool_manager.list_tools()}

    await http_client.aclose()

    assert tools == {"search_knowledge", "list_permissions", "upload_file"}


@pytest.mark.asyncio
async def test_upload_file_calls_import_endpoint_with_service_key():
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.read()
        return httpx.Response(
            201,
            json={
                "status": "success",
                "imported_count": 1,
                "documents": [
                    {
                        "document_id": "00000000-0000-0000-0000-000000000003",
                        "title": "notes.txt",
                        "chunk_count": 1,
                        "status": "pending",
                    }
                ],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = VectorKnowledgeClient(_settings(), http_client=http_client)
    server = create_mcp_server(client)

    result = await server._tool_manager.call_tool(
        "upload_file",
        {
            "knowledge_id": "legal",
            "filename": "notes.txt",
            "content_base64": "aGVsbG8=",
        },
    )

    await http_client.aclose()

    assert seen["method"] == "POST"
    assert seen["url"] == "http://vector.local/libraries/legal/import-file"
    assert seen["authorization"] == "Bearer vk_test"
    assert str(seen["content_type"]).startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="notes.txt"' in seen["body"]
    assert b"hello" in seen["body"]
    assert result.imported_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content_base64"),
    [
        ("notes.pdf", "aGVsbG8="),
        ("../notes.txt", "aGVsbG8="),
        ("notes.txt", "not-base64"),
        ("notes.txt", "aGVsbG8="),
    ],
)
async def test_upload_file_rejects_unsafe_input(filename, content_base64):
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal called
        called = True
        return httpx.Response(500)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = VectorKnowledgeClient(_settings(), http_client=http_client)
    server = create_mcp_server(client, max_upload_bytes=4)

    with pytest.raises(ToolError) as exc:
        await server._tool_manager.call_tool(
            "upload_file",
            {
                "knowledge_id": "legal",
                "filename": filename,
                "content_base64": content_base64,
            },
        )

    await http_client.aclose()

    assert "request_invalid" in str(exc.value)
    assert called is False


@pytest.mark.asyncio
async def test_upload_file_hides_permission_error_details():
    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(403, json={"detail": "private permission policy"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = VectorKnowledgeClient(_settings(), http_client=http_client)
    server = create_mcp_server(client)

    with pytest.raises(ToolError) as exc:
        await server._tool_manager.call_tool(
            "upload_file",
            {
                "knowledge_id": "legal",
                "filename": "notes.txt",
                "content_base64": "aGVsbG8=",
            },
        )

    await http_client.aclose()

    message = str(exc.value)
    assert "upstream_forbidden" in message
    assert "private permission policy" not in message


@pytest.mark.asyncio
async def test_search_knowledge_calls_retrieval_with_service_key():
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "records": [
                    {
                        "content": "matched chunk",
                        "score": 0.91,
                        "title": "Doc",
                        "metadata": {"document_id": "d1"},
                    }
                ]
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = VectorKnowledgeClient(_settings(), http_client=http_client)
    server = create_mcp_server(client)

    result = await server._tool_manager.call_tool(
        "search_knowledge",
        {
            "knowledge_id": "legal",
            "query": "contract risk",
            "top_k": 3,
            "score_threshold": 0.2,
        },
    )

    await http_client.aclose()

    assert seen["method"] == "POST"
    assert seen["url"] == "http://vector.local/retrieval"
    assert seen["authorization"] == "Bearer vk_test"
    assert b'"knowledge_id":"legal"' in seen["body"]
    assert b'"top_k":3' in seen["body"]
    assert result.records[0].content == "matched chunk"
    assert result.records[0].score == 0.91


@pytest.mark.asyncio
async def test_list_permissions_calls_current_user_permissions():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://vector.local/me/permissions"
        assert request.headers["authorization"] == "Bearer vk_test"
        return httpx.Response(200, json=[{"library_slug": "legal", "actions": ["read"]}])

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = VectorKnowledgeClient(_settings(), http_client=http_client)
    server = create_mcp_server(client)

    result = await server._tool_manager.call_tool("list_permissions", {})

    await http_client.aclose()

    assert result[0].library_slug == "legal"
    assert result[0].actions == ["read"]


@pytest.mark.asyncio
async def test_search_knowledge_hides_upstream_error_details():
    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(500, json={"detail": "secret internal traceback"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = VectorKnowledgeClient(_settings(), http_client=http_client)
    server = create_mcp_server(client)

    with pytest.raises(ToolError) as exc:
        await server._tool_manager.call_tool(
            "search_knowledge",
            {"knowledge_id": "legal", "query": "contract risk"},
        )

    await http_client.aclose()

    message = str(exc.value)
    assert "upstream_failed" in message
    assert "secret internal traceback" not in message


def test_config_requires_explicit_non_loopback_bind_opt_in():
    with pytest.raises(ValueError):
        MCPAdapterSettings(
            api_key="vk_test",
            http_host="0.0.0.0",
        )

    settings = MCPAdapterSettings(
        api_key="vk_test",
        http_host="0.0.0.0",
        allow_non_loopback_bind=True,
    )
    assert settings.http_host == "0.0.0.0"
