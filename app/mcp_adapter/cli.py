from __future__ import annotations

from app.mcp_adapter.client import VectorKnowledgeClient
from app.mcp_adapter.config import MCPAdapterSettings
from app.mcp_adapter.server import create_mcp_server


def main() -> None:
    settings = MCPAdapterSettings()
    client = VectorKnowledgeClient(settings)
    server = create_mcp_server(
        client,
        host=settings.http_host,
        port=settings.http_port,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
        max_upload_bytes=settings.max_upload_bytes,
    )
    server.run(transport=settings.transport)
