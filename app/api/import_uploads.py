from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user
from app.db import get_db
from app.deps import require_lib
from app.models.document_import_job import DocumentImportJob
from app.models.library import Library
from app.models.user import User
from app.schemas.documents import (
    ImportConfigurationRead,
    ImportJobRead,
    ImportSessionCreate,
    ImportSessionRead,
)
from app.services import import_uploads


router = APIRouter(prefix="/libraries/{slug}", tags=["document-imports"])


def _raise_upload_error(exc: import_uploads.ImportUploadError) -> None:
    raise HTTPException(
        exc.status_code,
        {"code": exc.code, "message": str(exc)},
    ) from exc


@router.get("/import-configuration", response_model=ImportConfigurationRead)
async def get_import_configuration(
    _: Library = Depends(require_lib("insert")),
) -> dict:
    return import_uploads.import_configuration()


@router.post(
    "/import-sessions",
    response_model=ImportSessionRead,
    status_code=201,
)
async def create_import_session(
    body: ImportSessionCreate,
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        if body.graph_extraction_requested:
            from app.services.graph_extraction_triggers import (
                graph_extraction_upload_configuration,
            )

            graph = await graph_extraction_upload_configuration(db, lib)
            graph_allowed = graph["available"] or (
                graph.get("schema_mode") == "explore" and graph.get("exploration_available")
            )
            if not graph_allowed:
                raise import_uploads.ImportUploadError(
                    "graph_extraction_unavailable",
                    "graph extraction is unavailable",
                    status_code=409,
                )
            if body.security_level not in graph["allowed_security_levels"]:
                raise import_uploads.ImportUploadError(
                    "graph_extraction_security_level_not_allowed",
                    "security level is not allowed for graph extraction",
                    status_code=409,
                )
        job = await import_uploads.create_session(
            db,
            library=lib,
            user=user,
            payload=body,
        )
        await db.commit()
        await db.refresh(job)
        return import_uploads.session_projection(job)
    except import_uploads.ImportUploadError as exc:
        await db.rollback()
        _raise_upload_error(exc)


@router.put("/import-sessions/{job_id}/content", status_code=204)
async def append_import_content(
    job_id: uuid.UUID,
    request: Request,
    response: Response,
    upload_offset: int = Header(alias="Upload-Offset", ge=0),
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        job = await import_uploads.get_owned_job(
            db,
            library_id=lib.id,
            job_id=job_id,
            user=user,
            for_update=True,
        )
        next_offset = await import_uploads.append_content(
            db,
            job=job,
            expected_offset=upload_offset,
            body=request.stream(),
        )
        await db.commit()
        response.headers["Upload-Offset"] = str(next_offset)
    except import_uploads.ImportUploadError as exc:
        await db.rollback()
        _raise_upload_error(exc)


@router.post(
    "/import-sessions/{job_id}/complete",
    response_model=ImportJobRead,
    status_code=202,
)
async def complete_import_session(
    job_id: uuid.UUID,
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        job = await import_uploads.get_owned_job(
            db,
            library_id=lib.id,
            job_id=job_id,
            user=user,
            for_update=True,
        )
        await import_uploads.complete_upload(db, job=job)
        await db.commit()
        await db.refresh(job)
        return await import_uploads.job_projection(db, job)
    except import_uploads.ImportUploadError as exc:
        await db.rollback()
        _raise_upload_error(exc)


@router.get("/import-jobs", response_model=list[ImportJobRead])
async def list_import_jobs(
    batch_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = (
        select(DocumentImportJob)
        .where(DocumentImportJob.library_id == lib.id)
        .order_by(DocumentImportJob.created_at.desc())
        .limit(limit)
    )
    if not user.is_superuser:
        stmt = stmt.where(DocumentImportJob.requested_by_user_id == user.id)
    if batch_id is not None:
        stmt = stmt.where(DocumentImportJob.batch_id == batch_id)
    jobs = list((await db.execute(stmt)).scalars().all())
    return [await import_uploads.job_projection(db, job) for job in jobs]


@router.get("/import-jobs/{job_id}", response_model=ImportJobRead)
async def get_import_job(
    job_id: uuid.UUID,
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        job = await import_uploads.get_owned_job(
            db,
            library_id=lib.id,
            job_id=job_id,
            user=user,
        )
        return await import_uploads.job_projection(db, job)
    except import_uploads.ImportUploadError as exc:
        _raise_upload_error(exc)


@router.post("/import-jobs/{job_id}/retry", response_model=ImportJobRead)
async def retry_import_job(
    job_id: uuid.UUID,
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        job = await import_uploads.get_owned_job(
            db,
            library_id=lib.id,
            job_id=job_id,
            user=user,
            for_update=True,
        )
        await import_uploads.retry_job(db, job=job)
        await db.commit()
        await db.refresh(job)
        return await import_uploads.job_projection(db, job)
    except import_uploads.ImportUploadError as exc:
        await db.rollback()
        _raise_upload_error(exc)


@router.delete("/import-sessions/{job_id}", status_code=204)
async def cancel_import_session(
    job_id: uuid.UUID,
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        job = await import_uploads.get_owned_job(
            db,
            library_id=lib.id,
            job_id=job_id,
            user=user,
            for_update=True,
        )
        await import_uploads.cancel_upload(db, job=job)
        await db.commit()
    except import_uploads.ImportUploadError as exc:
        await db.rollback()
        _raise_upload_error(exc)
