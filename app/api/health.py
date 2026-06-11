"""健康检查。轻量级：探活 DB 与 Qdrant，但不阻塞，超时即返回 degraded。"""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter
from sqlalchemy import text

from app.config import settings
from app.db import async_session_factory

log = logging.getLogger(__name__)
router = APIRouter()


async def _check_db() -> bool:
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("DB health check failed: %s", exc)
        return False


async def _check_qdrant() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{settings.qdrant_url.rstrip('/')}/healthz")
            return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("Qdrant health check failed: %s", exc)
        return False


@router.get("/health")
async def health() -> dict[str, object]:
    db_ok, qdrant_ok = await asyncio.gather(_check_db(), _check_qdrant())
    return {
        "status": "ok" if (db_ok and qdrant_ok) else "degraded",
        "db": db_ok,
        "qdrant": qdrant_ok,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
    }
