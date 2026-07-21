from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit


def normalize_allowed_security_levels(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("graph extraction security levels must be an array")

    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("each graph extraction security level must be a string")
        item = item.strip()
        if not item:
            raise ValueError("graph extraction security level must not be empty")
        normalized.append(item)
    return sorted(set(normalized))


SafetyCancellationReason = Literal[
    "library_opt_out",
    "security_allowlist_changed",
]


async def cancel_library_jobs_for_safety_change(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    job_error_code: SafetyCancellationReason,
    now: datetime | None = None,
) -> int:
    cancelled_at = now or datetime.now(timezone.utc)

    target_job_ids = select(GraphExtractionJob.id).where(
        GraphExtractionJob.library_id == library_id,
        GraphExtractionJob.status.in_(("queued", "processing")),
    )
    target_unit_ids = select(GraphExtractionUnit.id).where(
        GraphExtractionUnit.job_id.in_(target_job_ids)
    )

    await session.execute(
        update(ExtractionRawOutputAttempt)
        .where(
            ExtractionRawOutputAttempt.extraction_unit_id.in_(target_unit_ids),
            ExtractionRawOutputAttempt.request_status == "pending",
        )
        .values(
            request_status="abandoned",
            abandoned_at=cancelled_at,
            abandoned_reason="unit_cancelled",
            updated_at=cancelled_at,
        )
    )
    await session.execute(
        update(GraphExtractionUnit)
        .where(
            GraphExtractionUnit.job_id.in_(target_job_ids),
            GraphExtractionUnit.status.in_(("queued", "processing")),
        )
        .values(
            status="cancelled",
            retryable=False,
            worker_id=None,
            claim_token=None,
            claimed_at=None,
            lease_expires_at=None,
            error_code="unit_cancelled",
            error_message=None,
            finished_at=cancelled_at,
            updated_at=cancelled_at,
        )
    )
    result = await session.execute(
        update(GraphExtractionJob)
        .where(
            GraphExtractionJob.library_id == library_id,
            GraphExtractionJob.status.in_(("queued", "processing")),
        )
        .values(
            status="cancelled",
            error_code=job_error_code,
            error_message=None,
            finished_at=cancelled_at,
            updated_at=cancelled_at,
        )
    )
    return int(result.rowcount or 0)
