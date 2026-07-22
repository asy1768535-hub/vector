from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.config import Settings, settings
from app.services.object_storage import build_object_storage_adapter
from app.services.object_storage_contracts import ObjectStorageAdapter, ObjectStorageError
from app.services.revision_cleanup import (
    RevisionCleanupClaim,
    RevisionCleanupError,
    claim_revision_cleanup,
    cleanup_batch_limit,
    cleanup_candidate_record_ids,
    cleanup_now,
    complete_revision_cleanup,
    fail_revision_cleanup,
    queue_eligible_revision_cleanups,
    require_cleanup_worker_id,
)
from app.services.revision_files import require_storage_adapter_identity


@dataclass(frozen=True, slots=True)
class RevisionCleanupBatchResult:
    queued_record_ids: tuple[uuid.UUID, ...] = ()
    cleaned_record_ids: tuple[uuid.UUID, ...] = ()
    released_record_ids: tuple[uuid.UUID, ...] = ()
    failed_record_ids: tuple[uuid.UUID, ...] = ()


async def execute_revision_cleanup_claim(
    claim: RevisionCleanupClaim,
    *,
    adapter_factory: Callable[[Settings], ObjectStorageAdapter] = build_object_storage_adapter,
    config: Settings = settings,
) -> str:
    if not claim.managed_snapshot:
        return "released"
    try:
        adapter = adapter_factory(config)
    except Exception:
        raise RevisionCleanupError(
            "storage_adapter_unavailable", "storage adapter is unavailable"
        ) from None
    require_storage_adapter_identity(adapter, claim.locator)
    try:
        await adapter.delete(
            claim.locator.object_key,
            claim.locator.object_version,
        )
    except ObjectStorageError as exc:
        if exc.code != "object_not_found":
            raise
    return "deleted"


async def run_revision_cleanup_batch(
    session_factory,
    *,
    worker_id: str,
    at: datetime | None = None,
    adapter_factory: Callable[[Settings], ObjectStorageAdapter] = build_object_storage_adapter,
    config: Settings = settings,
) -> RevisionCleanupBatchResult:
    if not config.revision_cleanup_enabled:
        return RevisionCleanupBatchResult()
    worker_id = require_cleanup_worker_id(worker_id)
    current_time = cleanup_now(at)
    limit = cleanup_batch_limit(None, config)
    async with session_factory() as db:
        async with db.begin():
            queued = await queue_eligible_revision_cleanups(
                db,
                at=current_time,
                batch_limit=limit,
                config=config,
            )
    async with session_factory() as db:
        candidate_ids = await cleanup_candidate_record_ids(
            db,
            at=current_time,
            limit=limit,
            config=config,
        )
        await db.rollback()

    cleaned: list[uuid.UUID] = []
    released: list[uuid.UUID] = []
    failed: list[uuid.UUID] = []
    for record_id in candidate_ids:
        async with session_factory() as db:
            async with db.begin():
                claim = await claim_revision_cleanup(
                    db,
                    record_id=record_id,
                    worker_id=worker_id,
                    at=current_time,
                    config=config,
                )
        if claim is None:
            continue
        try:
            outcome = await execute_revision_cleanup_claim(
                claim,
                adapter_factory=adapter_factory,
                config=config,
            )
        except (ObjectStorageError, RevisionCleanupError) as exc:
            async with session_factory() as db:
                async with db.begin():
                    await fail_revision_cleanup(
                        db,
                        claim=claim,
                        error_code=exc.code,
                        at=current_time,
                        config=config,
                    )
            failed.append(record_id)
            continue
        except Exception:
            async with session_factory() as db:
                async with db.begin():
                    await fail_revision_cleanup(
                        db,
                        claim=claim,
                        error_code="cleanup_failed",
                        at=current_time,
                        config=config,
                    )
            failed.append(record_id)
            continue
        async with session_factory() as db:
            async with db.begin():
                result = await complete_revision_cleanup(
                    db,
                    claim=claim,
                    at=current_time,
                    config=config,
                )
        if result.status != "cleaned":
            failed.append(record_id)
        elif outcome == "released":
            released.append(record_id)
        else:
            cleaned.append(record_id)
    return RevisionCleanupBatchResult(
        queued_record_ids=queued,
        cleaned_record_ids=tuple(cleaned),
        released_record_ids=tuple(released),
        failed_record_ids=tuple(failed),
    )
