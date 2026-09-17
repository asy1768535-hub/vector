"""/me/permissions —— 当前用户在哪些库上有哪些动作。

前端登录后拉一次用于动态菜单/按钮权限；与后端 Casbin enforce 一致。
"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documents import _lock_writable
from app.auth.backend import current_active_user
from app.casbin import service as casbin_service
from app.config import settings
from app.db import get_db
from app.models.library import Library
from app.models.user import User
from app.schemas.admin import PermissionMatrixRow
from app.schemas.documents import (
    PersonalFileFolderDeleteResponse,
    PersonalFileFolderRead,
    PersonalFilePageRead,
    PersonalFileRead,
    PersonalImportTaskFilePageRead,
    PersonalImportTaskFolderRead,
    PersonalImportTaskPageRead,
    PersonalImportTaskRead,
    PersonalImportTaskSummaryRead,
    StoredFileDeleteRead,
    StoredFileDownloadRead,
    StoredFileFolderRead,
    StoredFilePageRead,
    StoredFileRead,
)
from app.services import import_uploads
from app.services.organization_authorization import (
    list_accessible_libraries,
    list_effective_permissions,
)

router = APIRouter(prefix="/me", tags=["me"])


async def _personal_task_libraries(
    db: AsyncSession,
    *,
    user: User,
) -> dict:
    libraries = await list_accessible_libraries(db, user=user, action="insert")
    return {library.id: library for library in libraries}


def _personal_task_read(
    job,
    *,
    library: Library,
    projection: dict,
) -> PersonalImportTaskRead:
    return PersonalImportTaskRead.model_validate(
        import_uploads.personal_task_projection(
            job,
            library_name=library.name,
            library_slug=library.slug,
            projection=projection,
        )
    )


def _raise_personal_task_error(exc: import_uploads.ImportUploadError) -> None:
    if exc.code == "personal_file_folder_not_found":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "folder_not_found", "message": "文件夹不存在或无权访问"},
        ) from exc
    if exc.code in {"job_not_found", "stored_file_not_found"}:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "task_not_found", "message": "任务不存在或无权访问"},
        ) from exc
    if exc.code in {"personal_file_path_invalid", "personal_file_page_invalid"}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_file_query", "message": "文件查询参数无效"},
        ) from exc
    if exc.code in {
        "personal_task_scope_invalid",
        "personal_task_limit_invalid",
        "personal_task_cursor_invalid",
    }:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_task_query", "message": "任务查询参数无效"},
        ) from exc
    messages = {
        "task_stale": "文件已更新或删除，请刷新任务状态",
        "attempt_budget_exhausted": "重试次数已用完，请重新上传文件",
        "job_not_retryable": "当前任务暂不可重试，请刷新后再试",
        "stored_file_delete_document_required": "该文件已生成知识资产，请使用现有删除功能",
        "stored_file_processing_active": "文件仍在处理中，暂时不能删除",
        "stored_file_download_unavailable": "文件下载暂不可用，请稍后重试",
    }
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        {
            "code": "task_retry_unavailable",
            "message": messages.get(exc.code, "当前任务暂不可重试，请刷新后再试"),
        },
    ) from exc


@router.get(
    "/permissions",
    response_model=list[PermissionMatrixRow],
    response_model_exclude_none=True,
)
async def my_permissions(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[PermissionMatrixRow]:
    if not settings.organization_authorization_enabled:
        if user.is_superuser:
            return []
        perms = casbin_service.list_user_permissions(str(user.id))
        if not perms:
            return []
        rows = await db.execute(
            select(Library.slug, Library.name).where(
                Library.slug.in_(list(perms.keys())), Library.deleted_at.is_(None)
            )
        )
        name_by_slug = {slug: name for slug, name in rows.all()}
        return [
            PermissionMatrixRow(
                library_slug=slug,
                actions=actions,
                library_name=name_by_slug[slug],
            )
            for slug, actions in perms.items()
            if slug in name_by_slug
        ]
    projections = await list_effective_permissions(db, user=user)
    return [
        PermissionMatrixRow(
            library_slug=row.library_slug,
            actions=list(row.actions),
            library_name=row.library_name,
            organization_id=row.organization_id,
        )
        for row in projections
    ]


@router.get("/files", response_model=PersonalFilePageRead)
async def list_my_files(
    library_slug: str = Query(min_length=1, max_length=128),
    path: str = Query(default="", max_length=2048),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PersonalFilePageRead:
    libraries_by_id = await _personal_task_libraries(db, user=user)
    library = next(
        (item for item in libraries_by_id.values() if item.slug == library_slug),
        None,
    )
    if library is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "library_not_found", "message": "知识库不存在或无权访问"},
        )
    try:
        result = await import_uploads.list_personal_files(
            db,
            user_id=user.id,
            library_id=library.id,
            path=path,
            page=page,
            page_size=page_size,
        )
    except import_uploads.ImportUploadError as exc:
        _raise_personal_task_error(exc)
    return PersonalFilePageRead(
        library_slug=library.slug,
        path=result.path,
        folders=[
            PersonalFileFolderRead(id=folder.id, name=folder.name, path=folder.path)
            for folder in result.folders
        ],
        files=[
            PersonalFileRead(
                document_id=document.id,
                file_name=import_uploads.personal_file_name(document),
                created_at=document.created_at,
            )
            for document in result.files
        ],
        folder_total=result.folder_total,
        file_total=result.file_total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get("/stored-files", response_model=StoredFilePageRead)
async def list_my_stored_files(
    library_slug: str = Query(min_length=1, max_length=128),
    path: str = Query(default="", max_length=2048),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> StoredFilePageRead:
    """Net-disk view of verified originals, including storage-only media."""

    libraries_by_id = await _personal_task_libraries(db, user=user)
    library = next(
        (item for item in libraries_by_id.values() if item.slug == library_slug),
        None,
    )
    if library is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "library_not_found", "message": "知识库不存在或无权访问"},
        )
    try:
        result = await import_uploads.list_stored_files(
            db,
            user_id=user.id,
            library_id=library.id,
            path=path,
            page=page,
            page_size=page_size,
        )
    except import_uploads.ImportUploadError as exc:
        _raise_personal_task_error(exc)
    return StoredFilePageRead(
        library_slug=library.slug,
        path=result.path,
        folders=[
            StoredFileFolderRead(
                name=folder.name,
                path=folder.path,
                file_total=folder.file_total,
            )
            for folder in result.folders
        ],
        files=[
            StoredFileRead(
                file_resource_id=file.file_resource_id,
                document_id=file.document_id,
                file_name=file.file_name,
                relative_path=file.relative_path,
                content_type=file.content_type,
                size_bytes=file.size_bytes,
                storage_status=file.storage_status,
                processing_status=file.processing_status,
                processing_stage=file.processing_stage,
                result_operation=file.result_operation,
                created_at=file.created_at,
            )
            for file in result.files
        ],
        folder_total=result.folder_total,
        file_total=result.file_total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get(
    "/stored-files/{file_resource_id}/download",
    response_model=StoredFileDownloadRead,
)
async def download_my_stored_file(
    file_resource_id: uuid.UUID,
    library_slug: str = Query(min_length=1, max_length=128),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> StoredFileDownloadRead:
    libraries_by_id = await _personal_task_libraries(db, user=user)
    library = next(
        (item for item in libraries_by_id.values() if item.slug == library_slug),
        None,
    )
    if library is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "library_not_found", "message": "知识库不存在或无权访问"},
        )
    try:
        url = await import_uploads.owned_stored_file_download_url(
            db,
            user_id=user.id,
            library_id=library.id,
            file_resource_id=file_resource_id,
        )
    except import_uploads.ImportUploadError as exc:
        _raise_personal_task_error(exc)
    return StoredFileDownloadRead(
        url=url,
        expires_in_seconds=settings.document_storage_signed_url_seconds,
    )


@router.delete(
    "/stored-files/{file_resource_id}",
    response_model=StoredFileDeleteRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_my_stored_file(
    file_resource_id: uuid.UUID,
    library_slug: str = Query(min_length=1, max_length=128),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> StoredFileDeleteRead:
    libraries_by_id = await _personal_task_libraries(db, user=user)
    library = next(
        (item for item in libraries_by_id.values() if item.slug == library_slug),
        None,
    )
    if library is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "library_not_found", "message": "知识库不存在或无权访问"},
        )
    try:
        await import_uploads.request_owned_stored_file_delete(
            db,
            user_id=user.id,
            library=library,
            file_resource_id=file_resource_id,
        )
        await db.commit()
    except import_uploads.ImportUploadError as exc:
        await db.rollback()
        _raise_personal_task_error(exc)
    return StoredFileDeleteRead(status="deleting")


@router.delete("/files/folder", response_model=PersonalFileFolderDeleteResponse)
async def delete_my_files_folder(
    library_slug: str = Query(min_length=1, max_length=128),
    path: str = Query(min_length=1, max_length=2048),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PersonalFileFolderDeleteResponse:
    libraries_by_id = await _personal_task_libraries(db, user=user)
    library = next(
        (item for item in libraries_by_id.values() if item.slug == library_slug),
        None,
    )
    if library is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "library_not_found", "message": "知识库不存在或无权访问"},
        )
    try:
        locked = await _lock_writable(db, library)
        deleted_count, folder_deleted = await import_uploads.delete_personal_file_folder(
            db,
            user_id=user.id,
            library=locked,
            path=path,
        )
        await db.commit()
    except import_uploads.ImportUploadError as exc:
        _raise_personal_task_error(exc)
    return PersonalFileFolderDeleteResponse(
        deleted_count=deleted_count,
        folder_deleted=folder_deleted,
    )


@router.get("/import-tasks", response_model=PersonalImportTaskPageRead)
async def list_my_import_tasks(
    scope: Literal["30d", "all"] = Query(default="30d"),
    limit: int = Query(default=20),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PersonalImportTaskPageRead:
    libraries_by_id = await _personal_task_libraries(db, user=user)
    try:
        page = await import_uploads.list_personal_import_tasks(
            db,
            user_id=user.id,
            library_ids=set(libraries_by_id),
            scope=scope,
            limit=limit,
            cursor_value=cursor,
        )
        projections = await import_uploads.personal_task_projections(db, page.jobs)
    except import_uploads.ImportUploadError as exc:
        _raise_personal_task_error(exc)
    return PersonalImportTaskPageRead(
        items=[
            _personal_task_read(
                job,
                library=libraries_by_id[job.library_id],
                projection=projection,
            )
            for job, projection in zip(page.jobs, projections)
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/import-task-files", response_model=PersonalImportTaskFilePageRead)
async def list_my_import_task_files(
    library_slug: str = Query(min_length=1, max_length=128),
    scope: Literal["30d", "all"] = Query(default="30d"),
    path: str = Query(default="", max_length=2048),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PersonalImportTaskFilePageRead:
    libraries_by_id = await _personal_task_libraries(db, user=user)
    library = next(
        (item for item in libraries_by_id.values() if item.slug == library_slug),
        None,
    )
    if library is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "library_not_found", "message": "知识库不存在或无权访问"},
        )
    try:
        result = await import_uploads.list_personal_import_task_files(
            db,
            user_id=user.id,
            library_id=library.id,
            scope=scope,
            path=path,
            page=page,
            page_size=page_size,
        )
        projections = await import_uploads.personal_task_projections(db, result.jobs)
    except import_uploads.ImportUploadError as exc:
        _raise_personal_task_error(exc)
    return PersonalImportTaskFilePageRead(
        library_slug=library.slug,
        path=result.path,
        folders=[
            PersonalImportTaskFolderRead(
                name=folder.name,
                path=folder.path,
                file_total=folder.file_total,
            )
            for folder in result.folders
        ],
        items=[
            _personal_task_read(job, library=library, projection=projection)
            for job, projection in zip(result.jobs, projections)
        ],
        folder_total=result.folder_total,
        file_total=result.file_total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get("/import-task-summary", response_model=PersonalImportTaskSummaryRead)
async def my_import_task_summary(
    scope: Literal["30d", "all"] = Query(default="30d"),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PersonalImportTaskSummaryRead:
    libraries_by_id = await _personal_task_libraries(db, user=user)
    try:
        summary = await import_uploads.personal_import_task_summary(
            db,
            user_id=user.id,
            library_ids=set(libraries_by_id),
            scope=scope,
        )
    except import_uploads.ImportUploadError as exc:
        _raise_personal_task_error(exc)
    return PersonalImportTaskSummaryRead(scope=scope, **summary)


@router.post("/import-tasks/{job_id}/retry", response_model=PersonalImportTaskRead)
async def retry_my_import_task(
    job_id: uuid.UUID,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PersonalImportTaskRead:
    libraries_by_id = await _personal_task_libraries(db, user=user)
    try:
        job = await import_uploads.get_personal_import_task(
            db,
            job_id=job_id,
            user_id=user.id,
            library_ids=set(libraries_by_id),
            for_update=True,
        )
        library = libraries_by_id.get(job.library_id)
        if library is None:
            raise import_uploads.ImportUploadError(
                "job_not_found",
                "import job not found",
                status_code=404,
            )
        await import_uploads.retry_personal_import_task(db, job=job)
        projection = (await import_uploads.personal_task_projections(db, [job]))[0]
        response = _personal_task_read(job, library=library, projection=projection)
        await db.commit()
        return response
    except import_uploads.ImportUploadError as exc:
        await db.rollback()
        _raise_personal_task_error(exc)
