from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPAdapterSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_ADAPTER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = False
    base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    api_key: SecretStr = SecretStr("")
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    transport: Literal["stdio", "streamable-http"] = "stdio"
    http_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    http_port: int = Field(default=8001, ge=1, le=65535)
    upload_enabled: bool = False
    max_upload_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1,
        le=50 * 1024 * 1024,
    )

    @model_validator(mode="after")
    def require_enabled_credentials(self) -> MCPAdapterSettings:
        if self.enabled and not self.api_key.get_secret_value().strip():
            raise ValueError("MCP adapter API key is required when enabled")
        host = (self.base_url.host or "").lower()
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if self.enabled and self.base_url.scheme != "https" and host not in loopback_hosts:
            raise ValueError("MCP adapter upstream must use HTTPS unless it is loopback")
        if (
            self.enabled
            and self.transport == "streamable-http"
            and self.http_host.lower() not in loopback_hosts
        ):
            raise ValueError("MCP Streamable HTTP must bind to a loopback host")
        return self
