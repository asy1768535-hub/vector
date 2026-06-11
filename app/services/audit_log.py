"""管理员操作审计：写入 sys_audit_log。

high-frequency 接口（/retrieval、/me/api-keys）不应该走这个；
仅 admin 操作（建库/授权/撤权/禁用用户）必写。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog

log = logging.getLogger(__name__)


async def record(
    db: AsyncSession,
    actor_user_id: uuid.UUID | None,
    action: str,
    target: dict[str, Any] | None = None,
) -> None:
    entry = AuditLog(actor_user_id=actor_user_id, action=action, target=target)
    db.add(entry)
    await db.flush()
    log.info("audit: actor=%s action=%s target=%s", actor_user_id, action, target)
