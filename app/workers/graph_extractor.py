"""Graph extraction worker process entry point."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.config import settings, validate_graph_extraction_startup


log = logging.getLogger(__name__)


async def _run_active_worker(worker_service, metadata: dict) -> None:
    from sqlalchemy.exc import DBAPIError

    from app.db import get_engine

    while True:
        try:
            await worker_service.run_graph_extraction_worker_pool(
                watch=True,
                metadata=metadata,
            )
            return
        except (DBAPIError, OSError) as exc:
            metadata["database_reconnects"] = int(
                metadata.get("database_reconnects", 0)
            ) + 1
            log.warning(
                "transient database connection failure; restarting worker pool: %s",
                type(exc).__name__,
            )
            await get_engine().dispose()
            await asyncio.sleep(max(1.0, settings.graph_extraction_worker_poll_seconds))


async def run(*, watch: bool) -> None:
    validate_graph_extraction_startup(settings)
    if not settings.graph_extraction_enabled:
        log.info("graph extraction is disabled; heartbeat shell exiting")
        return

    from app.services import heartbeat

    instance_id = heartbeat.make_instance_id()
    metadata = (
        {"watch": True, "mode": "active_worker", "milestone": "M5"}
        if watch
        else {"watch": False, "mode": "heartbeat_only", "milestone": "M1"}
    )
    if not watch:
        await heartbeat.beat(
            "graph_extractor",
            instance_id,
            hostname=heartbeat.HOSTNAME,
            pid=heartbeat.PID,
            started_at=heartbeat.STARTED_AT,
            status="online",
            heartbeat_metadata=metadata,
        )
        await heartbeat.beat(
            "graph_extractor",
            instance_id,
            hostname=heartbeat.HOSTNAME,
            pid=heartbeat.PID,
            started_at=heartbeat.STARTED_AT,
            status="stopping",
            heartbeat_metadata=metadata,
        )
        return

    from app.services import graph_extraction_worker as worker_service

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        heartbeat.heartbeat_loop(
            "graph_extractor",
            instance_id,
            hostname=heartbeat.HOSTNAME,
            pid=heartbeat.PID,
            started_at=heartbeat.STARTED_AT,
            stop_event=stop_event,
            metadata_provider=lambda: metadata,
        )
    )
    try:
        await _run_active_worker(worker_service, metadata)
    finally:
        stop_event.set()
        await heartbeat.beat(
            "graph_extractor",
            instance_id,
            hostname=heartbeat.HOSTNAME,
            pid=heartbeat.PID,
            started_at=heartbeat.STARTED_AT,
            status="stopping",
            heartbeat_metadata=metadata,
        )
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Graph extraction worker. Use --watch for active Unit consumption."
    )
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(watch=args.watch))
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
