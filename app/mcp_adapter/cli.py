from __future__ import annotations

import asyncio

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp_adapter.auth import PublicAPIKeyVerifier
from app.mcp_adapter.client import PublicV1Client
from app.mcp_adapter.config import MCPAdapterSettings
from app.mcp_adapter.server import create_mcp_server


def configure_http_transport(server, settings: MCPAdapterSettings) -> None:
    server.settings.host = settings.http_host
    server.settings.port = settings.http_port
    if settings.private_network:
        public_host = settings.public_host
        server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_host, f"{public_host}:443"],
            allowed_origins=[f"https://{public_host}"],
        )


def main() -> None:
    settings = MCPAdapterSettings()
    if not settings.enabled:
        raise SystemExit(
            "MCP adapter is disabled; set MCP_ADAPTER_ENABLED=true to start it."
        )

    if settings.transport == "stdio":
        client = PublicV1Client(settings)
        server = create_mcp_server(
            client,
            upload_enabled=settings.upload_enabled,
            max_upload_bytes=settings.max_upload_bytes,
        )
        server.run(transport="stdio")
        return

    http_client = httpx.AsyncClient()
    verifier = PublicAPIKeyVerifier(settings, http_client)

    def current_user_client() -> PublicV1Client:
        access = get_access_token()
        if access is None:
            raise RuntimeError("MCP request is missing authenticated identity")
        return PublicV1Client(settings, http_client, api_key=access.token)

    upstream_url = str(settings.base_url).rstrip("/").removesuffix("/api/v1")
    server = create_mcp_server(
        client_factory=current_user_client,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=upstream_url,
            resource_server_url=None,
            required_scopes=["mcp"],
        ),
        upload_enabled=settings.upload_enabled,
        max_upload_bytes=settings.max_upload_bytes,
    )
    configure_http_transport(server, settings)
    try:
        server.run(transport="streamable-http")
    finally:
        asyncio.run(http_client.aclose())
