"""健康检查。轻量级：探活 DB 与 Qdrant，但不阻塞，超时即返回 degraded。"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from fastapi import APIRouter
from sqlalchemy import text

from app.config import settings
from app.db import async_session_factory
from app.services import selfcheck

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


async def _check_embedding() -> tuple[bool, Optional[str]]:
    """真实探活 embedding 服务（一次小 embed）。返回 (ok, error_or_None)。

    包一层 6s 超时，避免 embedding 服务挂起时 /health 跟着卡住
    （embed_one 自身的 read 超时长达 120s）。
    """
    try:
        ok, msg, _dim = await asyncio.wait_for(selfcheck.probe_embedding(), timeout=6.0)
        return ok, (None if ok else msg)
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding health check failed: %s", exc)
        return False, str(exc)[:300]


async def _check_rerank() -> tuple[str, Optional[str]]:
    """rerank 探活（仅 enabled+configured 时）。返回 (state, error)，state ∈ {off,ok,fail}。"""
    try:
        state, msg = await asyncio.wait_for(selfcheck.probe_rerank(), timeout=6.0)
        return state, (None if state in ("ok", "off") else msg)
    except Exception as exc:  # noqa: BLE001
        log.warning("rerank health check failed: %s", exc)
        return "fail", str(exc)[:300]


def _check_ocr() -> str:
    """OCR 引擎状态：ok=已装；missing=全局开了 OCR 但引擎没装（大声报，会静默不生效）；off=未启用。

    注意：这是全局视角。某个库单独开 ocr_enabled 而全局关时，这里仍显示 off——
    库级生效与否以该库 ocr_enabled 为准，引擎是否安装由本字段判断。
    """
    from app.services import ocr
    if ocr.is_available():
        return "ok"
    return "missing" if settings.ocr_enabled else "off"


@router.get("/health")
async def health() -> dict[str, object]:
    db_ok, qdrant_ok, emb, rr = await asyncio.gather(
        _check_db(), _check_qdrant(), _check_embedding(), _check_rerank()
    )
    emb_ok, emb_err = emb
    rr_state, rr_err = rr
    ocr_state = _check_ocr()
    out: dict[str, object] = {
        # rerank 仅在 enabled 且真的探测失败（fail）时拉低整体状态；off 不影响。
        # ocr 即使 missing 也不拉低整体（按库可选功能），只在字段里显式暴露。
        "status": "ok" if (db_ok and qdrant_ok and emb_ok and rr_state != "fail") else "degraded",
        "db": db_ok,
        "qdrant": qdrant_ok,
        "embedding": "ok" if emb_ok else "fail",
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "rerank": rr_state,
        "ocr": ocr_state,
    }
    if not emb_ok and emb_err:
        out["embedding_error"] = emb_err
    if rr_err:
        out["rerank_error"] = rr_err
    return out
