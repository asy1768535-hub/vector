"""Knowledge artifact worker process entry point."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.config import settings, validate_knowledge_artifact_startup


log = logging.getLogger(__name__)


async def run(*, watch: bool) -> None:
    validate_knowledge_artifact_startup(settings)
    if not settings.knowledge_artifact_runtime_enabled:
        log.info("knowledge artifact runtime is disabled; worker exiting")
        return

    from app.services import heartbeat
    from app.services.knowledge_artifact_worker import run_knowledge_artifact_worker

    instance_id = heartbeat.make_instance_id()
    metadata = {
        "watch": watch,
        "mode": "active_worker" if watch else "one_shot",
        "milestone": "v0.8",
    }
    if not watch:
        await heartbeat.beat(
            "knowledge_artifact_worker",
            instance_id,
            hostname=heartbeat.HOSTNAME,
            pid=heartbeat.PID,
            started_at=heartbeat.STARTED_AT,
            status="online",
            heartbeat_metadata=metadata,
        )
        await run_knowledge_artifact_worker(watch=False, metadata=metadata)
        await heartbeat.beat(
            "knowledge_artifact_worker",
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
            "knowledge_artifact_worker",
            instance_id,
            hostname=heartbeat.HOSTNAME,
            pid=heartbeat.PID,
            started_at=heartbeat.STARTED_AT,
            stop_event=stop_event,
            metadata_provider=lambda: metadata,
        )
    )
    try:
        await run_knowledge_artifact_worker(watch=True, metadata=metadata)
    finally:
        stop_event.set()
        await heartbeat.beat(
            "knowledge_artifact_worker",
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
    parser = argparse.ArgumentParser(description="Knowledge artifact worker")
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(watch=args.watch))
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
