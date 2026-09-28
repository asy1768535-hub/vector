"""Verify a caller's API key through the existing user-authenticated API."""
from __future__ import annotations

import asyncio
import hashlib

import httpx
from mcp.server.auth.provider import AccessToken

from app.mcp_adapter.config import MCPAdapterSettings


class PublicAPIKeyVerifier:
    def __init__(self, settings: MCPAdapterSettings, http_client: httpx.AsyncClient) -> None:
        self._url = f"{str(settings.base_url).rstrip('/').removesuffix('/api/v1')}/me/permissions"
        self._timeout = settings.timeout_seconds
        self._http_client = http_client

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 256 or token != token.strip():
            return None
        try:
            response = await self._http_client.get(
                self._url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except asyncio.CancelledError:
            raise
        except (httpx.TimeoutException, httpx.RequestError):
            return None
        if response.status_code != 200:
            return None
        try:
            permissions = response.json()
        except ValueError:
            return None
        if not isinstance(permissions, list):
            return None
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
        return AccessToken(
            token=token,
            client_id=f"api-key:{fingerprint}",
            scopes=["mcp"],
        )
