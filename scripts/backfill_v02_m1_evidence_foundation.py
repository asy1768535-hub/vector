from __future__ import annotations

import argparse
import asyncio
import uuid

from app.db import async_session_factory
from app.services.evidence_backfill import backfill_v02_m1_batch


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill v0.2 M1 evidence foundation rows.")
    parser.add_argument("--library-id", type=uuid.UUID, default=None)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


async def _run() -> None:
    args = _parse_args()
    while True:
        async with async_session_factory() as db:
            result = await backfill_v02_m1_batch(
                db,
                library_id=args.library_id,
                batch_size=args.batch_size,
            )
        print(
            f"processed={result.processed} failed={result.failed} exhausted={result.exhausted}",
            flush=True,
        )
        if args.once or result.exhausted:
            return


if __name__ == "__main__":
    asyncio.run(_run())
