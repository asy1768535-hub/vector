"""进程心跳服务（运行状态监控，docs/26）。

三类进程（API / Embedding Worker / Cleanup Worker）各跑一个独立的 `heartbeat_loop`
asyncio 任务，每 ~15s upsert 一行；**与主循环迭代解耦**，长任务 / degraded sleep 期间照常打卡。

铁律：
  - 心跳用**独立短事务 / 独立 AsyncSession**，绝不复用业务处理事务。
  - 写失败只记 WARNING、绝不抛出，绝不影响业务事务或主任务。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import async_session_factory
from app.models.service_heartbeat import ServiceHeartbeat

log = logging.getLogger(__name__)

# 进程级身份：导入（≈进程启动）时各算一次，全程不变。
HOSTNAME = socket.gethostname()
PID = os.getpid()
STARTED_AT = datetime.now(timezone.utc)


def make_instance_id() -> str:
    """f"{hostname}-{pid}-{uuid8}"：随机短后缀保证进程重启（PID 可能复用）后是新行，不覆盖旧实例。"""
    return f"{HOSTNAME}-{PID}-{uuid.uuid4().hex[:8]}"


async def beat(
    service_type: str,
    instance_id: str,
    *,
    hostname: str,
    pid: int,
    started_at: datetime,
    status: str = "online",
    heartbeat_metadata: Optional[dict[str, Any]] = None,
) -> bool:
    """单次心跳 upsert（独立短事务）。成功 True；任何异常吞掉记 WARNING 返回 False，绝不抛。"""
    tbl = ServiceHeartbeat.__table__
    try:
        async with async_session_factory() as s:
            stmt = pg_insert(tbl).values(
                id=uuid.uuid4(),
                service_type=service_type,
                instance_id=instance_id,
                hostname=hostname,
                pid=pid,
                started_at=started_at,
                status=status,
                metadata=heartbeat_metadata,  # DB 列名 metadata
            ).on_conflict_do_update(
                index_elements=["service_type", "instance_id"],
                set_={
                    "last_seen_at": func.now(),
                    "status": status,
                    "pid": pid,
                    "metadata": heartbeat_metadata,
                },
            )
            await s.execute(stmt)
            await s.commit()
        return True
    except Exception as exc:  # noqa: BLE001  心跳是旁路：失败不得影响业务
        log.warning("heartbeat beat failed (service=%s instance=%s): %s", service_type, instance_id, exc)
        return False


async def prune(cutoff_seconds: Optional[int] = None) -> int:
    """删除超过 cutoff 未更新的过期心跳行（独立短事务）。异常自吞，返回删除行数（失败 0）。"""
    secs = cutoff_seconds if cutoff_seconds is not None else settings.heartbeat_prune_seconds
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=secs)
    try:
        async with async_session_factory() as s:
            res = await s.execute(delete(ServiceHeartbeat).where(ServiceHeartbeat.last_seen_at < cutoff))
            await s.commit()
            return res.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        log.warning("heartbeat prune failed: %s", exc)
        return 0


async def heartbeat_loop(
    service_type: str,
    instance_id: str,
    *,
    hostname: str,
    pid: int,
    started_at: datetime,
    stop_event: asyncio.Event,
    metadata_provider: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
    also_prune: bool = False,
) -> None:
    """独立后台任务：每 heartbeat_interval_seconds 打一次卡，直到 stop_event 置位。

    与主循环并发、互不阻塞——主循环跑长任务或 degraded sleep 时，本任务照常打卡。
    """
    interval = settings.heartbeat_interval_seconds
    while not stop_event.is_set():
        meta: Optional[dict[str, Any]] = None
        if metadata_provider is not None:
            try:
                meta = metadata_provider()
            except Exception:  # noqa: BLE001
                meta = None
        await beat(service_type, instance_id, hostname=hostname, pid=pid,
                   started_at=started_at, heartbeat_metadata=meta)
        if also_prune:
            await prune()
        # 可被 stop_event 立即唤醒的 sleep（优雅关闭不必等满一个 interval）
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def _age_seconds(now: datetime, last_seen: datetime) -> float:
    return (now - last_seen).total_seconds()


def derive_service_status(
    service_type: str,
    rows: list[ServiceHeartbeat],
    *,
    now: datetime,
    offline_seconds: int,
) -> dict[str, Any]:
    """把某 service_type 的若干心跳行聚合为状态（纯函数，便于单测）。

    - 排除 status='stopping'（优雅退出实例不计入 online/known）。
    - known_instances = 非 stopping 行数；online_instances = 其中龄 <= offline_seconds 的行数。
    - status：online==0 → offline；否则 online<known 或 任一在线实例 metadata.degraded → degraded；否则 online。
    - latest = last_seen_at 最新的非 stopping 实例（供展示）。
    """
    active = [r for r in rows if r.status != "stopping"]
    known = len(active)
    online_rows = [r for r in active if _age_seconds(now, r.last_seen_at) <= offline_seconds]
    online_n = len(online_rows)
    if online_n == 0:
        status = "offline"
    elif online_n < known or any((r.heartbeat_metadata or {}).get("degraded") for r in online_rows):
        status = "degraded"
    else:
        status = "online"
    latest_out: Optional[dict[str, Any]] = None
    if active:
        latest = max(active, key=lambda r: r.last_seen_at)
        latest_out = {
            "instance_id": latest.instance_id,
            "hostname": latest.hostname,
            "pid": latest.pid,
            "last_seen_at": latest.last_seen_at,
            "seconds_since_last_seen": int(_age_seconds(now, latest.last_seen_at)),
            "heartbeat_metadata": latest.heartbeat_metadata,
        }
    return {
        "service_type": service_type,
        "status": status,
        "online_instances": online_n,
        "known_instances": known,
        "latest": latest_out,
    }
