from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps as deps_module
from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.deps import require_lib
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.user import User
from app.schemas.graph_extraction_jobs import (
    GraphExtractionCandidateList,
    GraphExtractionCandidateRead,
    GraphExtractionCreate,
    GraphExtractionJobList,
    GraphExtractionJobRead,
    GraphExtractionRerun,
    GraphExtractionUnitList,
    GraphExtractionUnitRead,
)
from app.services import graph_extraction_jobs
from app.services.graph_extraction_jobs import GraphExtractionJobError
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    authorize_library_management,
)


router = APIRouter(
    prefix="/libraries/{slug}/v04/graph-extractions",
    tags=["v0.4-graph-extraction"],
)


async def _require_graph_rerun_library(
    slug: str,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> Library:
    library = await deps_module.load_active_library(slug, db)
    if library is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if not settings.organization_authorization_enabled:
        if user.is_superuser or deps_module.has_permission(str(user.id), slug, "insert"):
            return library
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    try:
        await authorize_library_management(db, user=user, library=library)
        return library
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin_required") from exc


def _raise_job_error(exc: GraphExtractionJobError) -> None:
    if exc.code == "job_not_found":
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.code) from exc
    raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc


async def _document_scope(
    db: AsyncSession,
    *,
    library: Library,
    document_id: uuid.UUID,
) -> tuple[Document, DocumentRevision]:
    document = await db.get(Document, document_id)
    if (
        document is None
        or document.library_id != library.id
        or document.deleted_at is not None
        or document.current_revision_id is None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document_not_found")
    revision = await db.get(DocumentRevision, document.current_revision_id)
    if (
        revision is None
        or revision.library_id != library.id
        or revision.document_id != document.id
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "current_revision_missing")
    return document, revision


@router.post("/", response_model=GraphExtractionJobRead, status_code=status.HTTP_201_CREATED)
async def create_graph_extraction(
    body: GraphExtractionCreate,
    library: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GraphExtractionJobRead:
    document, revision = await _document_scope(
        db,
        library=library,
        document_id=body.document_id,
    )
    try:
        job = await graph_extraction_jobs.create_graph_extraction_job(
            db,
            library=library,
            document=document,
            revision=revision,
            trigger_type="manual",
            execution_mode="production",
            requested_by=user,
            idempotency_key=body.idempotency_key,
        )
        await db.commit()
        return GraphExtractionJobRead.model_validate(job)
    except GraphExtractionJobError as exc:
        _raise_job_error(exc)


@router.get("/", response_model=GraphExtractionJobList)
async def list_graph_extractions(
    job_status: str | None = Query(default=None, alias="status", max_length=32),
    document_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> GraphExtractionJobList:
    filters = [GraphExtractionJob.library_id == library.id]
    if job_status is not None:
        filters.append(GraphExtractionJob.status == job_status)
    if document_id is not None:
        filters.append(GraphExtractionJob.document_id == document_id)
    total = int(
        (
            await db.execute(
                select(func.count(GraphExtractionJob.id)).where(*filters)
            )
        ).scalar_one()
    )
    result = await db.execute(
        select(GraphExtractionJob)
        .where(*filters)
        .order_by(GraphExtractionJob.created_at.desc(), GraphExtractionJob.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return GraphExtractionJobList(
        items=[GraphExtractionJobRead.model_validate(row) for row in result.scalars()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=GraphExtractionJobRead)
async def get_graph_extraction(
    job_id: uuid.UUID,
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> GraphExtractionJobRead:
    try:
        job = await graph_extraction_jobs.get_graph_extraction_job(
            db,
            library=library,
            job_id=job_id,
        )
        return GraphExtractionJobRead.model_validate(job)
    except GraphExtractionJobError as exc:
        _raise_job_error(exc)


@router.get("/{job_id}/units", response_model=GraphExtractionUnitList)
async def list_graph_extraction_units(
    job_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> GraphExtractionUnitList:
    try:
        job = await graph_extraction_jobs.get_graph_extraction_job(
            db,
            library=library,
            job_id=job_id,
        )
    except GraphExtractionJobError as exc:
        _raise_job_error(exc)
    total = int(
        (
            await db.execute(
                select(func.count(GraphExtractionUnit.id)).where(
                    GraphExtractionUnit.job_id == job.id
                )
            )
        ).scalar_one()
    )
    unit_result = await db.execute(
        select(GraphExtractionUnit)
        .where(GraphExtractionUnit.job_id == job.id)
        .order_by(GraphExtractionUnit.ordinal)
        .limit(limit)
        .offset(offset)
    )
    items = []
    for unit in unit_result.scalars().all():
        attempt_result = await db.execute(
            select(ExtractionRawOutputAttempt)
            .where(ExtractionRawOutputAttempt.extraction_unit_id == unit.id)
            .order_by(ExtractionRawOutputAttempt.attempt_no.desc())
            .limit(1)
        )
        attempt = attempt_result.scalars().first()
        items.append(
            GraphExtractionUnitRead(
                id=unit.id,
                ordinal=unit.ordinal,
                center_chunk_id=unit.center_chunk_id,
                center_evidence_id=unit.center_evidence_id,
                status=unit.status,
                model_attempt_count=unit.model_attempt_count,
                retryable=unit.retryable,
                error_code=unit.error_code,
                latest_attempt_status=(attempt.request_status if attempt else None),
                latest_parse_status=(attempt.parse_status if attempt else None),
                created_at=unit.created_at,
                started_at=unit.started_at,
                finished_at=unit.finished_at,
            )
        )
    return GraphExtractionUnitList(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


def _candidate_read(row, candidate_type: str) -> GraphExtractionCandidateRead:
    is_entity = candidate_type == "entity"
    return GraphExtractionCandidateRead(
        candidate_type=candidate_type,
        id=row.id,
        candidate_key=row.candidate_key,
        ontology_type_key=(
            row.entity_type_key if is_entity else row.relation_type_key
        ),
        status=row.status,
        final_confidence=row.final_confidence,
        review_reason=row.review_reason,
        matched_formal_id=(
            row.matched_entity_id if is_entity else row.matched_relation_id
        ),
        materialized_formal_id=(
            row.materialized_entity_id if is_entity else row.materialized_relation_id
        ),
        created_at=row.created_at,
    )


@router.get("/{job_id}/candidates", response_model=GraphExtractionCandidateList)
async def list_graph_extraction_candidates(
    job_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> GraphExtractionCandidateList:
    try:
        job = await graph_extraction_jobs.get_graph_extraction_job(
            db,
            library=library,
            job_id=job_id,
        )
    except GraphExtractionJobError as exc:
        _raise_job_error(exc)
    entity_total = int(
        (
            await db.execute(
                select(func.count(GraphEntityCandidate.id)).where(
                    GraphEntityCandidate.job_id == job.id
                )
            )
        ).scalar_one()
    )
    relation_total = int(
        (
            await db.execute(
                select(func.count(GraphRelationCandidate.id)).where(
                    GraphRelationCandidate.job_id == job.id
                )
            )
        ).scalar_one()
    )
    fetch_limit = offset + limit
    entity_result = await db.execute(
        select(GraphEntityCandidate)
        .where(GraphEntityCandidate.job_id == job.id)
        .order_by(GraphEntityCandidate.created_at, GraphEntityCandidate.candidate_key)
        .limit(fetch_limit)
    )
    relation_result = await db.execute(
        select(GraphRelationCandidate)
        .where(GraphRelationCandidate.job_id == job.id)
        .order_by(GraphRelationCandidate.created_at, GraphRelationCandidate.candidate_key)
        .limit(fetch_limit)
    )
    rows = [
        *[(row, "entity") for row in entity_result.scalars().all()],
        *[(row, "relation") for row in relation_result.scalars().all()],
    ]
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(
        key=lambda item: (
            item[0].created_at or minimum,
            item[1],
            item[0].candidate_key,
        )
    )
    return GraphExtractionCandidateList(
        items=[_candidate_read(row, kind) for row, kind in rows[offset:fetch_limit]],
        total=entity_total + relation_total,
        limit=limit,
        offset=offset,
    )


@router.post("/{job_id}/retry", response_model=GraphExtractionJobRead)
async def retry_graph_extraction(
    job_id: uuid.UUID,
    library: Library = Depends(require_lib("insert")),
    db: AsyncSession = Depends(get_db),
) -> GraphExtractionJobRead:
    try:
        job = await graph_extraction_jobs.retry_graph_extraction_job(
            db,
            library=library,
            job_id=job_id,
        )
        await db.commit()
        return GraphExtractionJobRead.model_validate(job)
    except GraphExtractionJobError as exc:
        _raise_job_error(exc)


@router.post("/{job_id}/rerun", response_model=GraphExtractionJobRead)
async def rerun_graph_extraction(
    job_id: uuid.UUID,
    body: GraphExtractionRerun,
    library: Library = Depends(_require_graph_rerun_library),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GraphExtractionJobRead:
    if not settings.organization_authorization_enabled and not user.is_superuser:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin_required")
    try:
        source = await graph_extraction_jobs.get_graph_extraction_job(
            db,
            library=library,
            job_id=job_id,
        )
        document, revision = await _document_scope(
            db,
            library=library,
            document_id=source.document_id,
        )
        job = await graph_extraction_jobs.create_graph_extraction_job(
            db,
            library=library,
            document=document,
            revision=revision,
            trigger_type="full_rerun",
            execution_mode="production",
            requested_by=user,
            idempotency_key=body.client_idempotency_key,
            rerun_of_job_id=source.id,
        )
        await db.commit()
        return GraphExtractionJobRead.model_validate(job)
    except GraphExtractionJobError as exc:
        _raise_job_error(exc)


@router.post("/{job_id}/cancel", response_model=GraphExtractionJobRead)
async def cancel_graph_extraction(
    job_id: uuid.UUID,
    library: Library = Depends(require_lib("insert")),
    db: AsyncSession = Depends(get_db),
) -> GraphExtractionJobRead:
    try:
        job = await graph_extraction_jobs.cancel_graph_extraction_job(
            db,
            library=library,
            job_id=job_id,
        )
        await db.commit()
        return GraphExtractionJobRead.model_validate(job)
    except GraphExtractionJobError as exc:
        _raise_job_error(exc)
