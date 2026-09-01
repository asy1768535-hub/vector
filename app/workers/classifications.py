"""Document classification worker process entry point."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.config import settings, validate_classification_runtime_startup


log = logging.getLogger(__name__)


async def run(*, watch: bool) -> None:
    validate_classification_runtime_startup(settings)
    if not settings.classification_runtime_enabled:
        log.info("classification runtime is disabled; worker exiting")
        return

    from app.services import heartbeat
    from app.services.classification_worker import run_classification_worker

    instance_id = heartbeat.make_instance_id()
    metadata = {
        "watch": watch,
        "mode": "active_worker" if watch else "one_shot",
        "milestone": "v0.8",
    }
    if not watch:
        await heartbeat.beat(
            "classification_worker",
            instance_id,
            hostname=heartbeat.HOSTNAME,
            pid=heartbeat.PID,
            started_at=heartbeat.STARTED_AT,
            heartbeat_metadata=metadata,
        )
        try:
            await run_classification_worker(watch=False, metadata=metadata)
        finally:
            await heartbeat.beat(
                "classification_worker",
                instance_id,
                hostname=heartbeat.HOSTNAME,
                pid=heartbeat.PID,
                started_at=heartbeat.STARTED_AT,
                status="stopping",
                heartbeat_metadata=metadata,
            )
        return

    stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        heartbeat.heartbeat_loop(
            "classification_worker",
            instance_id,
            hostname=heartbeat.HOSTNAME,
            pid=heartbeat.PID,
            started_at=heartbeat.STARTED_AT,
            stop_event=stop_event,
            metadata_provider=lambda: metadata,
        )
    )
    try:
        await run_classification_worker(watch=True, metadata=metadata)
    finally:
        stop_event.set()
        await heartbeat.beat(
            "classification_worker",
            instance_id,
            hostname=heartbeat.HOSTNAME,
            pid=heartbeat.PID,
            started_at=heartbeat.STARTED_AT,
            status="stopping",
            heartbeat_metadata=metadata,
        )
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Document classification worker")
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(watch=args.watch))
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
