from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.db import async_session_factory
from app.services.evidence_locator_backfill import (
    backfill_evidence_locators,
    rollback_evidence_locators,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bounded EvidenceLocatorV1 backfill; dry-run unless --apply is set."
    )
    parser.add_argument("--library-id", type=uuid.UUID)
    parser.add_argument("--revision-id", type=uuid.UUID)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-revisions", type=int)
    parser.add_argument("--cursor")
    parser.add_argument("--run-id")
    parser.add_argument("--rollback-run-id")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


async def _run() -> None:
    args = _parse_args()
    async with async_session_factory() as db:
        if args.rollback_run_id:
            result = await rollback_evidence_locators(
                db,
                run_id=args.rollback_run_id,
                library_id=args.library_id,
                revision_id=args.revision_id,
                batch_size=args.batch_size,
                max_revisions=args.max_revisions,
                cursor=args.cursor,
                apply=args.apply,
            )
        else:
            result = await backfill_evidence_locators(
                db,
                library_id=args.library_id,
                revision_id=args.revision_id,
                batch_size=args.batch_size,
                max_revisions=args.max_revisions,
                cursor=args.cursor,
                apply=args.apply,
                run_id=args.run_id,
            )
    print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True, default=str))


if __name__ == "__main__":
    asyncio.run(_run())

