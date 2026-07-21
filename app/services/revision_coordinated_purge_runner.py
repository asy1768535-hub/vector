from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings, settings
from app.services.graph_publication_activation import (
    GraphPublicationActivationError,
    activate_graph_publication,
)
from app.services.revision_coordinated_purge import (
    CoordinatedPurgeError,
    claim_coordinated_purge_operation,
    coordinated_purge_candidate_ids,
    fail_coordinated_purge_operation,
    finalize_coordinated_purge_activation,
)
from app.services.revision_cleanup import cleanup_now, require_cleanup_worker_id


@dataclass(frozen=True, slots=True)
class CoordinatedPurgeBatchResult:
    cleanup_pending_ids: tuple[uuid.UUID, ...] = ()
    failed_ids: tuple[uuid.UUID, ...] = ()


async def run_coordinated_purge_batch(
    session_factory,
    *,
    worker_id: str,
    at: datetime | None = None,
    config: Settings = settings,
) -> CoordinatedPurgeBatchResult:
    if not config.revision_coordinated_purge_enabled:
        return CoordinatedPurgeBatchResult()
    worker_id = require_cleanup_worker_id(worker_id)
    current_time = cleanup_now(at)
    limit = config.revision_coordinated_purge_batch_size
    async with session_factory() as db:
        candidate_ids = await coordinated_purge_candidate_ids(
            db,
            at=current_time,
            limit=limit,
            config=config,
        )
        await db.rollback()
    pending: list[uuid.UUID] = []
    failed: list[uuid.UUID] = []
    for operation_id in candidate_ids:
        async with session_factory() as db:
            async with db.begin():
                claim = await claim_coordinated_purge_operation(
                    db,
                    operation_id=operation_id,
                    worker_id=worker_id,
                    at=current_time,
                    config=config,
                )
        if claim is None:
            continue
        try:
            async with session_factory() as activation_db:
                await activate_graph_publication(
                    activation_db,
                    claim.replacement_publication_id,
                    expected_manifest_hash=claim.replacement_manifest_hash,
                    command_idempotency_key=(
                        f"coordinated-purge:{claim.operation_id}"
                    ),
                    config=config,
                )
            async with session_factory() as db:
                async with db.begin():
                    result = await finalize_coordinated_purge_activation(
                        db,
                        claim=claim,
                        at=current_time,
                        config=config,
                    )
            if result.status == "cleanup_pending":
                pending.append(operation_id)
        except (GraphPublicationActivationError, CoordinatedPurgeError) as exc:
            async with session_factory() as db:
                async with db.begin():
                    await fail_coordinated_purge_operation(
                        db,
                        claim=claim,
                        error_code=exc.code,
                        at=current_time,
                    )
            failed.append(operation_id)
        except Exception:
            async with session_factory() as db:
                async with db.begin():
                    await fail_coordinated_purge_operation(
                        db,
                        claim=claim,
                        error_code="coordinated_purge_failed",
                        at=current_time,
                    )
            failed.append(operation_id)
    return CoordinatedPurgeBatchResult(
        cleanup_pending_ids=tuple(pending),
        failed_ids=tuple(failed),
    )
