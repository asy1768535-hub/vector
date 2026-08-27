from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class MCPAdapterSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_ADAPTER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8100")
    api_key: SecretStr = SecretStr("")
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    transport: Literal["streamable-http", "stdio"] = "streamable-http"
    http_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    http_port: int = Field(default=8001, ge=1, le=65535)
    allow_non_loopback_bind: bool = False
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    allowed_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_runtime(self) -> MCPAdapterSettings:
        if not self.api_key.get_secret_value().strip():
            raise ValueError("MCP_ADAPTER_API_KEY is required")

        if (
            self.transport == "streamable-http"
            and self.http_host.lower() not in _LOOPBACK_HOSTS
            and not self.allow_non_loopback_bind
        ):
            raise ValueError(
                "non-loopback MCP bind requires MCP_ADAPTER_ALLOW_NON_LOOPBACK_BIND=true"
            )

        if self.transport == "streamable-http" and not self.allowed_hosts:
            raise ValueError("MCP_ADAPTER_ALLOWED_HOSTS must not be empty")

        return self
