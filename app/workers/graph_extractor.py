"""M1 graph extractor process shell: heartbeat only, no Unit consumption."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.config import settings, validate_graph_extraction_startup


log = logging.getLogger(__name__)


async def run(*, watch: bool) -> None:
    validate_graph_extraction_startup(settings)
    if not settings.graph_extraction_enabled:
        log.info("graph extraction is disabled; heartbeat shell exiting")
        return

    from app.services import heartbeat

    instance_id = heartbeat.make_instance_id()
    metadata = {
        "watch": watch,
        "mode": "heartbeat_only",
        "milestone": "M1",
    }
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
        while True:
            await asyncio.sleep(settings.graph_extraction_worker_poll_seconds)
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
        description="M1 graph extractor heartbeat shell; does not consume Units."
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
