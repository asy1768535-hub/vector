from __future__ import annotations

from app.mcp_adapter.client import PublicV1Client
from app.mcp_adapter.config import MCPAdapterSettings
from app.mcp_adapter.server import create_mcp_server


def main() -> None:
    settings = MCPAdapterSettings()
    if not settings.enabled:
        raise SystemExit(
            "MCP adapter is disabled; set MCP_ADAPTER_ENABLED=true to start it."
        )

    client = PublicV1Client(settings)
    server = create_mcp_server(client)
    server.settings.host = settings.http_host
    server.settings.port = settings.http_port
    server.run(transport=settings.transport)
