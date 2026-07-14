from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select, update

from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit


UnitTerminalStatus = Literal["succeeded", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class StaleUnitRecoveryResult:
    abandoned_attempt_count: int
    failed_unit_count: int
    requeued_unit_count: int

    @property
    def recovered_unit_count(self) -> int:
        return self.failed_unit_count + self.requeued_unit_count


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_positive_int(value: int, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")


async def claim_graph_extraction_unit(
    db,
    *,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
    now: datetime | None = None,
) -> GraphExtractionUnit | None:
    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 255:
        raise ValueError("worker_id must contain 1 to 255 characters")
    _require_positive_int(lease_seconds, label="lease_seconds")
    _require_positive_int(max_attempts, label="max_attempts")
    claimed_at = now or _utcnow()
    claim_token = uuid.uuid4()

    async with db.begin():
        result = await db.execute(
            select(GraphExtractionUnit)
            .join(
                GraphExtractionJob,
                GraphExtractionJob.id == GraphExtractionUnit.job_id,
            )
            .where(
                GraphExtractionUnit.status == "queued",
                GraphExtractionUnit.model_attempt_count < max_attempts,
                GraphExtractionJob.status.in_(("queued", "processing")),
            )
            .order_by(
                GraphExtractionJob.created_at.asc(),
                GraphExtractionUnit.ordinal.asc(),
                GraphExtractionUnit.id.asc(),
            )
            .with_for_update(skip_locked=True, of=GraphExtractionUnit)
            .limit(1)
        )
        unit = result.scalars().first()
        if unit is None:
            return None

        job = await db.get(GraphExtractionJob, unit.job_id, with_for_update=True)
        if job is None or job.status not in {"queued", "processing"}:
            return None
        unit.status = "processing"
        unit.worker_id = worker_id.strip()
        unit.claim_token = claim_token
        unit.claimed_at = claimed_at
        unit.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        unit.retryable = False
        unit.error_code = None
        unit.error_message = None
        unit.started_at = unit.started_at or claimed_at
        job.status = "processing"
        job.current_stage = "building_context"
        job.started_at = job.started_at or claimed_at
        job.error_code = None
        job.error_message = None
        await db.flush()
        return unit


async def renew_graph_extraction_unit_lease(
    db,
    *,
    unit_id: uuid.UUID,
    claim_token: uuid.UUID,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    _require_positive_int(lease_seconds, label="lease_seconds")
    renewed_at = now or _utcnow()
    async with db.begin():
        result = await db.execute(
            update(GraphExtractionUnit)
            .where(
                GraphExtractionUnit.id == unit_id,
                GraphExtractionUnit.status == "processing",
                GraphExtractionUnit.claim_token == claim_token,
                GraphExtractionUnit.lease_expires_at > renewed_at,
            )
            .values(
                lease_expires_at=renewed_at + timedelta(seconds=lease_seconds),
                updated_at=renewed_at,
            )
        )
        return result.rowcount == 1


async def lock_live_graph_extraction_claim(
    db,
    *,
    unit_id: uuid.UUID,
    claim_token: uuid.UUID,
    now: datetime | None = None,
) -> GraphExtractionUnit | None:
    checked_at = now or _utcnow()
    result = await db.execute(
        select(GraphExtractionUnit)
        .where(
            GraphExtractionUnit.id == unit_id,
            GraphExtractionUnit.status == "processing",
            GraphExtractionUnit.claim_token == claim_token,
            GraphExtractionUnit.lease_expires_at > checked_at,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def mark_claimed_unit_terminal(
    db,
    *,
    unit_id: uuid.UUID,
    claim_token: uuid.UUID,
    status: UnitTerminalStatus,
    retryable: bool = False,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
) -> bool:
    if status not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("status must be succeeded, failed, or cancelled")
    if status != "failed" and retryable:
        raise ValueError("only failed Units may be retryable")
    finished_at = now or _utcnow()
    async with db.begin():
        result = await db.execute(
            update(GraphExtractionUnit)
            .where(
                GraphExtractionUnit.id == unit_id,
                GraphExtractionUnit.status == "processing",
                GraphExtractionUnit.claim_token == claim_token,
                GraphExtractionUnit.lease_expires_at > finished_at,
            )
            .values(
                status=status,
                retryable=retryable,
                worker_id=None,
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
                error_code=error_code,
                error_message=(error_message[:1000] if error_message else None),
                finished_at=finished_at,
                updated_at=finished_at,
            )
        )
        return result.rowcount == 1


async def recover_stale_graph_extraction_units(
    db,
    *,
    max_attempts: int,
    now: datetime | None = None,
) -> StaleUnitRecoveryResult:
    _require_positive_int(max_attempts, label="max_attempts")
    recovered_at = now or _utcnow()
    stale_unit_ids = select(GraphExtractionUnit.id).where(
        GraphExtractionUnit.status == "processing",
        GraphExtractionUnit.lease_expires_at.is_not(None),
        GraphExtractionUnit.lease_expires_at <= recovered_at,
    )

    async with db.begin():
        abandoned = await db.execute(
            update(ExtractionRawOutputAttempt)
            .where(
                ExtractionRawOutputAttempt.extraction_unit_id.in_(stale_unit_ids),
                ExtractionRawOutputAttempt.request_status == "pending",
            )
            .values(
                request_status="abandoned",
                abandoned_at=recovered_at,
                abandoned_reason="lease_expired",
                updated_at=recovered_at,
            )
        )
        failed = await db.execute(
            update(GraphExtractionUnit)
            .where(
                GraphExtractionUnit.id.in_(stale_unit_ids),
                GraphExtractionUnit.model_attempt_count >= max_attempts,
            )
            .values(
                status="failed",
                retryable=False,
                worker_id=None,
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
                error_code="model_attempt_budget_exhausted",
                error_message=None,
                finished_at=recovered_at,
                updated_at=recovered_at,
            )
        )
        requeued = await db.execute(
            update(GraphExtractionUnit)
            .where(
                GraphExtractionUnit.id.in_(stale_unit_ids),
                GraphExtractionUnit.model_attempt_count < max_attempts,
            )
            .values(
                status="queued",
                retryable=True,
                worker_id=None,
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
                error_code="lease_expired",
                error_message=None,
                finished_at=None,
                updated_at=recovered_at,
            )
        )
    return StaleUnitRecoveryResult(
        abandoned_attempt_count=int(abandoned.rowcount or 0),
        failed_unit_count=int(failed.rowcount or 0),
        requeued_unit_count=int(requeued.rowcount or 0),
    )
