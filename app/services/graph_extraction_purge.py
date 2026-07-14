from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, or_, select, update

from app.config import settings
from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.graph_occurrences import GraphEntityOccurrence, GraphRelationOccurrence
from app.models.graph_review import GraphEntityMergeCandidate, GraphExtractionConflict


@dataclass(frozen=True, slots=True)
class GraphExtractionPurgeResult:
    job_count: int
    cancelled_job_count: int
    purged_row_count: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rowcount(result) -> int:
    return int(result.rowcount or 0)


async def _cancel_jobs_before_purge(
    db,
    *,
    job_ids: set[uuid.UUID],
    now: datetime,
) -> int:
    if not job_ids:
        return 0
    unit_ids = select(GraphExtractionUnit.id).where(
        GraphExtractionUnit.job_id.in_(job_ids)
    )
    await db.execute(
        update(ExtractionRawOutputAttempt)
        .where(
            ExtractionRawOutputAttempt.extraction_unit_id.in_(unit_ids),
            ExtractionRawOutputAttempt.request_status == "pending",
        )
        .values(
            request_status="abandoned",
            abandoned_at=now,
            abandoned_reason="unit_cancelled",
            updated_at=now,
        )
    )
    await db.execute(
        update(GraphExtractionUnit)
        .where(
            GraphExtractionUnit.job_id.in_(job_ids),
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
            finished_at=now,
            updated_at=now,
        )
    )
    cancelled = await db.execute(
        update(GraphExtractionJob)
        .where(
            GraphExtractionJob.id.in_(job_ids),
            GraphExtractionJob.status.in_(("queued", "processing")),
        )
        .values(
            status="cancelled",
            current_stage="finalizing",
            error_code="sensitive_payload_purged",
            error_message=None,
            finished_at=now,
            updated_at=now,
        )
    )
    await db.execute(
        update(GraphExtractionUnit)
        .where(GraphExtractionUnit.job_id.in_(job_ids))
        .values(error_message=None, updated_at=now)
    )
    return _rowcount(cancelled)


async def _purge_context_payloads(
    db,
    *,
    job_ids: set[uuid.UUID],
    now: datetime,
) -> int:
    if not job_ids:
        return 0
    result = await db.execute(
        update(ExtractionContextSnapshot)
        .where(
            ExtractionContextSnapshot.job_id.in_(job_ids),
            ExtractionContextSnapshot.purged_at.is_(None),
        )
        .values(
            context_json=None,
            context_text=None,
            document_metadata=None,
            chunk_title_path=None,
            block_title_path=None,
            effective_title_path=None,
            purged_at=now,
        )
    )
    return _rowcount(result)


async def _purge_attempt_payloads(
    db,
    *,
    job_ids: set[uuid.UUID],
    now: datetime,
) -> int:
    if not job_ids:
        return 0
    unit_ids = select(GraphExtractionUnit.id).where(
        GraphExtractionUnit.job_id.in_(job_ids)
    )
    result = await db.execute(
        update(ExtractionRawOutputAttempt)
        .where(
            ExtractionRawOutputAttempt.extraction_unit_id.in_(unit_ids),
            ExtractionRawOutputAttempt.purged_at.is_(None),
        )
        .values(
            raw_response=None,
            parsed_response=None,
            parse_error=None,
            purged_at=now,
            updated_at=now,
        )
    )
    return _rowcount(result)


async def _purge_candidate_payloads(
    db,
    *,
    job_ids: set[uuid.UUID],
    now: datetime,
) -> int:
    if not job_ids:
        return 0
    count = 0
    for model in (GraphEntityOccurrence, GraphRelationOccurrence):
        result = await db.execute(
            update(model)
            .where(model.job_id.in_(job_ids), model.purged_at.is_(None))
            .values(raw_payload=None, purged_at=now)
        )
        count += _rowcount(result)
    entity_candidates = await db.execute(
        update(GraphEntityCandidate)
        .where(
            GraphEntityCandidate.job_id.in_(job_ids),
            GraphEntityCandidate.purged_at.is_(None),
        )
        .values(
            canonical_name=None,
            normalized_name=None,
            proposed_aliases=None,
            proposed_properties=None,
            external_mapping_hints=None,
            review_reason=None,
            validation_errors=None,
            purged_at=now,
            updated_at=now,
        )
    )
    count += _rowcount(entity_candidates)
    relation_candidates = await db.execute(
        update(GraphRelationCandidate)
        .where(
            GraphRelationCandidate.job_id.in_(job_ids),
            GraphRelationCandidate.purged_at.is_(None),
        )
        .values(
            proposed_properties=None,
            review_reason=None,
            validation_errors=None,
            purged_at=now,
            updated_at=now,
        )
    )
    count += _rowcount(relation_candidates)
    for model in (GraphEntityCandidateEvidence, GraphRelationCandidateEvidence):
        result = await db.execute(
            update(model)
            .where(model.job_id.in_(job_ids), model.purged_at.is_(None))
            .values(
                quote_text=None,
                resolved_source_span=None,
                validation_error=None,
                candidate_matches=[],
                purged_at=now,
            )
        )
        count += _rowcount(result)
    for model in (GraphEntityMergeCandidate, GraphExtractionConflict):
        result = await db.execute(
            update(model)
            .where(model.job_id.in_(job_ids), model.purged_at.is_(None))
            .values(
                details=None,
                description=None,
                evidence=None,
                purged_at=now,
                updated_at=now,
            )
        )
        count += _rowcount(result)
    jobs = await db.execute(
        update(GraphExtractionJob)
        .where(
            GraphExtractionJob.id.in_(job_ids),
            GraphExtractionJob.sensitive_payload_purged_at.is_(None),
        )
        .values(
            sensitive_payload_purged_at=now,
            error_message=None,
            updated_at=now,
        )
    )
    count += _rowcount(jobs)
    return count


async def _purge_job_payloads(
    db,
    *,
    context_job_ids: set[uuid.UUID],
    attempt_job_ids: set[uuid.UUID],
    candidate_job_ids: set[uuid.UUID],
    now: datetime,
) -> GraphExtractionPurgeResult:
    all_job_ids = context_job_ids | attempt_job_ids | candidate_job_ids
    cancelled = await _cancel_jobs_before_purge(db, job_ids=all_job_ids, now=now)
    count = await _purge_context_payloads(db, job_ids=context_job_ids, now=now)
    count += await _purge_attempt_payloads(db, job_ids=attempt_job_ids, now=now)
    count += await _purge_candidate_payloads(db, job_ids=candidate_job_ids, now=now)
    return GraphExtractionPurgeResult(len(all_job_ids), cancelled, count)


async def purge_document_graph_extraction_payloads(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    now: datetime | None = None,
) -> GraphExtractionPurgeResult:
    purged_at = now or _utcnow()
    result = await db.execute(
        select(GraphExtractionJob.id).where(
            GraphExtractionJob.library_id == library_id,
            GraphExtractionJob.document_id == document_id,
        )
    )
    job_ids = set(result.scalars().all())
    return await _purge_job_payloads(
        db,
        context_job_ids=job_ids,
        attempt_job_ids=job_ids,
        candidate_job_ids=job_ids,
        now=purged_at,
    )


async def purge_revision_graph_extraction_payloads(
    db,
    *,
    library_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    now: datetime | None = None,
) -> GraphExtractionPurgeResult:
    purged_at = now or _utcnow()
    result = await db.execute(
        select(GraphExtractionJob.id).where(
            GraphExtractionJob.library_id == library_id,
            GraphExtractionJob.document_revision_id == document_revision_id,
        )
    )
    job_ids = set(result.scalars().all())
    return await _purge_job_payloads(
        db,
        context_job_ids=job_ids,
        attempt_job_ids=job_ids,
        candidate_job_ids=job_ids,
        now=purged_at,
    )


async def purge_expired_graph_extraction_payloads(
    db,
    *,
    batch_limit: int = 100,
    now: datetime | None = None,
) -> GraphExtractionPurgeResult:
    if (
        isinstance(batch_limit, bool)
        or not isinstance(batch_limit, int)
        or batch_limit < 1
        or batch_limit > 1000
    ):
        raise ValueError("purge batch_limit must be between 1 and 1000")
    purged_at = now or _utcnow()
    context_cutoff = purged_at - timedelta(
        days=settings.graph_extraction_context_retention_days
    )
    attempt_cutoff = purged_at - timedelta(
        days=settings.graph_extraction_raw_output_retention_days
    )
    candidate_cutoff = purged_at - timedelta(
        days=settings.graph_extraction_candidate_retention_days
    )
    due_context = exists(
        select(1).where(
            ExtractionContextSnapshot.job_id == GraphExtractionJob.id,
            ExtractionContextSnapshot.purged_at.is_(None),
            ExtractionContextSnapshot.created_at < context_cutoff,
        )
    )
    due_attempt = exists(
        select(1)
        .select_from(ExtractionRawOutputAttempt)
        .join(
            GraphExtractionUnit,
            GraphExtractionUnit.id
            == ExtractionRawOutputAttempt.extraction_unit_id,
        )
        .where(
            GraphExtractionUnit.job_id == GraphExtractionJob.id,
            ExtractionRawOutputAttempt.purged_at.is_(None),
            ExtractionRawOutputAttempt.created_at < attempt_cutoff,
        )
    )
    result = await db.execute(
        select(GraphExtractionJob)
        .where(
            or_(
                due_context,
                due_attempt,
                (
                    GraphExtractionJob.created_at < candidate_cutoff
                )
                & GraphExtractionJob.sensitive_payload_purged_at.is_(None),
            )
        )
        .order_by(GraphExtractionJob.created_at)
        .limit(batch_limit)
        .with_for_update(skip_locked=True)
    )
    jobs = list(result.scalars().all())
    context_ids = {
        job.id for job in jobs if job.created_at < context_cutoff
    }
    attempt_ids = {
        job.id for job in jobs if job.created_at < attempt_cutoff
    }
    candidate_ids = {
        job.id for job in jobs if job.created_at < candidate_cutoff
    }
    return await _purge_job_payloads(
        db,
        context_job_ids=context_ids,
        attempt_job_ids=attempt_ids,
        candidate_job_ids=candidate_ids,
        now=purged_at,
    )
