"""Cleanup Worker（#7 §8）：消费 qdrant_cleanup_outbox，幂等执行 Qdrant 物理清理。

  python -m app.workers.cleanup            # 处理完 pending 即退出（cron 友好）
  python -m app.workers.cleanup --watch    # 长跑：空闲时 poll

独立于 embedding worker：故障依赖/扩缩容目标不同。FOR UPDATE SKIP LOCKED 抢锁；
失败指数退避（available_at += backoff）、超 max_attempts 标 failed（死信）；processing 超时重置。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import socket
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import async_session_factory
from app.models.cleanup_outbox import CleanupOutbox
from app.services import cleanup as cleanup_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] cleanup[%(process)d]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _backoff_seconds(attempt: int) -> float:
    base, cap = settings.cleanup_backoff_base_seconds, settings.cleanup_backoff_max_seconds
    delay = min(base * (2 ** max(0, attempt - 1)), cap)
    return delay + random.uniform(0, base)   # 轻微 jitter，避免惊群


async def _reset_stale(db: AsyncSession) -> int:
    """超时 processing 行回收。**已达 max_attempts 的直接标 failed**（否则 reset 成 pending 后
    _claim 因 attempt>=max 永不领取 → 永久卡住）；未达上限的才重置为 pending 重试。"""
    params = {"secs": str(settings.cleanup_stale_seconds), "max": settings.cleanup_worker_max_attempts}
    failed = await db.execute(
        text(
            """
            UPDATE qdrant_cleanup_outbox
            SET status='failed', worker_id=NULL, claimed_at=NULL, finished_at=NOW(),
                last_error=COALESCE(last_error,'stale processing at max attempts')
            WHERE status='processing' AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - (:secs || ' seconds')::interval
              AND attempt_count >= :max
            """
        ),
        params,
    )
    reset = await db.execute(
        text(
            """
            UPDATE qdrant_cleanup_outbox
            SET status='pending', worker_id=NULL, claimed_at=NULL
            WHERE status='processing' AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - (:secs || ' seconds')::interval
              AND attempt_count < :max
            """
        ),
        params,
    )
    await db.commit()
    return (failed.rowcount or 0) + (reset.rowcount or 0)


async def _claim(db: AsyncSession, worker_id: str, limit: int) -> list[CleanupOutbox]:
    raw = text(
        """
        WITH picked AS (
            SELECT id FROM qdrant_cleanup_outbox
            WHERE status='pending' AND available_at <= NOW()
              AND attempt_count < :max_attempts
            ORDER BY available_at
            FOR UPDATE SKIP LOCKED
            LIMIT :limit
        )
        UPDATE qdrant_cleanup_outbox o
        SET status='processing', worker_id=:wid, claimed_at=NOW(), attempt_count=o.attempt_count+1
        FROM picked WHERE o.id=picked.id
        RETURNING o.id
        """
    )
    result = await db.execute(
        raw, {"limit": limit, "wid": worker_id, "max_attempts": settings.cleanup_worker_max_attempts}
    )
    ids = [r[0] for r in result.all()]
    if not ids:
        await db.commit()
        return []
    rows = (await db.execute(select(CleanupOutbox).where(CleanupOutbox.id.in_(ids)))).scalars().all()
    await db.commit()
    return list(rows)


async def _process(db: AsyncSession, row: CleanupOutbox) -> None:
    # 先捕获标量：失败路径会 rollback 使 ORM 对象过期，之后再访问会触发异步 lazy load（MissingGreenlet）
    row_id, attempt, key = row.id, (row.attempt_count or 0), row.idempotency_key
    try:
        await cleanup_service.execute_event(row)   # 幂等：不存在 point/collection 视为成功
        await db.execute(
            update(CleanupOutbox).where(CleanupOutbox.id == row_id).values(
                status="done", finished_at=datetime.now(timezone.utc), last_error=None
            )
        )
        await db.commit()
        log.info("cleanup done: %s key=%s", row.event_type, key)
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        if attempt >= settings.cleanup_worker_max_attempts:
            await db.execute(update(CleanupOutbox).where(CleanupOutbox.id == row_id).values(
                status="failed", last_error=str(exc)[:1000], finished_at=datetime.now(timezone.utc)))
            log.error("cleanup FAILED (max attempts) key=%s: %s", key, exc)
        else:
            nxt = datetime.now(timezone.utc) + timedelta(seconds=_backoff_seconds(attempt))
            await db.execute(update(CleanupOutbox).where(CleanupOutbox.id == row_id).values(
                status="pending", worker_id=None, claimed_at=None,
                available_at=nxt, last_error=str(exc)[:1000]))
            log.warning("cleanup retry key=%s attempt=%s next=%s: %s", key, attempt, nxt, exc)
        await db.commit()


async def run(watch: bool) -> None:
    worker_id = _worker_id()
    log.info("starting cleanup worker id=%s watch=%s", worker_id, watch)
    # 运行状态心跳：独立后台任务，与主循环解耦——长任务 / 空闲 poll 期间照常打卡（docs/26）。
    from app.services import heartbeat
    hb_stop = asyncio.Event()
    hb_instance = heartbeat.make_instance_id()
    hb_task = asyncio.create_task(heartbeat.heartbeat_loop(
        "cleanup_worker", hb_instance,
        hostname=heartbeat.HOSTNAME, pid=heartbeat.PID, started_at=heartbeat.STARTED_AT,
        stop_event=hb_stop, metadata_provider=lambda: {"watch": watch},
    ))

    async def _stop_heartbeat() -> None:
        hb_stop.set()
        await heartbeat.beat("cleanup_worker", hb_instance, hostname=heartbeat.HOSTNAME,
                             pid=heartbeat.PID, started_at=heartbeat.STARTED_AT, status="stopping")
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

    while True:
        async with async_session_factory() as s:
            reset = await _reset_stale(s)
            if reset:
                log.warning("reset %s stale processing cleanup rows", reset)
            rows = await _claim(s, worker_id, settings.cleanup_worker_batch)
        if not rows:
            if not watch:
                log.info("no pending cleanup; exiting")
                await _stop_heartbeat()
                return
            await asyncio.sleep(settings.cleanup_worker_poll_seconds)
            continue
        log.info("claimed %s cleanup rows", len(rows))
        for row in rows:
            async with async_session_factory() as s:
                await _process(s, row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Qdrant cleanup outbox worker.")
    parser.add_argument("--watch", action="store_true", help="Long-running; poll when idle.")
    args = parser.parse_args()
    try:
        asyncio.run(run(watch=args.watch))
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
