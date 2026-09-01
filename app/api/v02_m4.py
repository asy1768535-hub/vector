from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps as deps_module
from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.deps import require_lib
from app.models.library import Library
from app.models.user import User
from app.schemas.v02_m4 import (
    BlockRead,
    ChunkRead,
    ChunkSourceRead,
    DocumentFolderUpdate,
    EvidenceRead,
    FolderCreate,
    FolderRead,
    FolderUpdate,
    RevisionRead,
    SyncBatchError,
    SyncBatchRequest,
    SyncBatchResponse,
    SyncDocumentDeleteRequest,
    SyncDocumentResult,
    SyncDocumentUpsertRequest,
    SyncSourceCreate,
    SyncSourceRead,
    SyncSourceUpdate,
)
from app.services import folders as folders_service
from app.services import evidence_read as evidence_read_service
from app.services import sync_documents as sync_documents_service
from app.services import sync_sources as sync_sources_service
from app.services.metadata_guard import MetadataValidationError
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    resolve_loaded_library_access,
)


router = APIRouter(prefix="/libraries/{slug}", tags=["v0.2-m4"])


def _raise_api_error(exc: Exception) -> None:
    if isinstance(exc, MetadataValidationError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if isinstance(exc, LookupError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    raise exc


def _require_sync_source_api_enabled() -> None:
    if not settings.enable_sync_source_api:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


async def _require_batch_delete_permission(
    body: SyncBatchRequest,
    user: User,
    lib: Library,
    db: AsyncSession,
) -> None:
    if not any(item.action == "delete" for item in body.items):
        return
    if settings.organization_authorization_enabled:
        try:
            await resolve_loaded_library_access(
                db,
                user=user,
                library=lib,
                action="delete",
            )
            return
        except OrganizationAuthorizationError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    if user.is_superuser:
        return
    if deps_module.has_permission(str(user.id), lib.slug, "delete"):
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")


@router.post("/folders", response_model=FolderRead, status_code=status.HTTP_201_CREATED)
async def create_folder(
    body: FolderCreate,
    lib: Library = Depends(require_lib("insert")),
    db: AsyncSession = Depends(get_db),
) -> FolderRead:
    try:
        folder = await folders_service.create_folder(
            db, lib, name=body.name, parent_id=body.parent_id, sort_order=body.sort_order
        )
        await db.commit()
        return folder
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/folders", response_model=list[FolderRead])
async def list_folders(
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> list[FolderRead]:
    return await folders_service.list_folders(db, lib)


@router.get("/folders/{folder_id}", response_model=FolderRead)
async def get_folder(
    folder_id: uuid.UUID,
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> FolderRead:
    try:
        return await folders_service.get_active_folder(db, lib, folder_id)
    except Exception as exc:
        _raise_api_error(exc)


@router.patch("/folders/{folder_id}", response_model=FolderRead)
async def update_folder(
    folder_id: uuid.UUID,
    body: FolderUpdate,
    lib: Library = Depends(require_lib("insert")),
    db: AsyncSession = Depends(get_db),
) -> FolderRead:
    try:
        folder = await folders_service.update_folder(db, lib, folder_id, body)
        await db.commit()
        return folder
    except Exception as exc:
        _raise_api_error(exc)


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    folder_id: uuid.UUID,
    lib: Library = Depends(require_lib("delete")),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await folders_service.delete_folder(db, lib, folder_id)
        await db.commit()
        return None
    except Exception as exc:
        _raise_api_error(exc)


@router.patch("/documents/{document_id}/folder")
async def move_document_folder(
    document_id: uuid.UUID,
    body: DocumentFolderUpdate,
    lib: Library = Depends(require_lib("insert")),
    db: AsyncSession = Depends(get_db),
):
    try:
        doc = await folders_service.move_document_to_folder(db, lib, document_id, body.folder_id)
        await db.commit()
        return {
            "document_id": str(doc.id),
            "folder_id": str(doc.folder_id) if doc.folder_id is not None else None,
            "current_revision": doc.current_revision,
            "current_revision_id": str(doc.current_revision_id) if doc.current_revision_id is not None else None,
            "latest_revision_id": str(doc.latest_revision_id) if doc.latest_revision_id is not None else None,
        }
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/sync-sources", response_model=SyncSourceRead, status_code=status.HTTP_201_CREATED)
async def create_sync_source(
    body: SyncSourceCreate,
    lib: Library = Depends(require_lib("insert")),
    db: AsyncSession = Depends(get_db),
) -> SyncSourceRead:
    _require_sync_source_api_enabled()
    try:
        source = await sync_sources_service.create_sync_source(db, lib, body)
        await db.commit()
        return source
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/sync-sources", response_model=list[SyncSourceRead])
async def list_sync_sources(
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> list[SyncSourceRead]:
    _require_sync_source_api_enabled()
    return await sync_sources_service.list_sync_sources(db, lib)


@router.get("/sync-sources/{source_key}", response_model=SyncSourceRead)
async def get_sync_source(
    source_key: str,
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> SyncSourceRead:
    _require_sync_source_api_enabled()
    try:
        return await sync_sources_service.require_sync_source(db, lib, source_key)
    except Exception as exc:
        _raise_api_error(exc)


@router.patch("/sync-sources/{source_key}", response_model=SyncSourceRead)
async def update_sync_source(
    source_key: str,
    body: SyncSourceUpdate,
    lib: Library = Depends(require_lib("insert")),
    db: AsyncSession = Depends(get_db),
) -> SyncSourceRead:
    _require_sync_source_api_enabled()
    try:
        source = await sync_sources_service.update_sync_source(db, lib, source_key, body)
        await db.commit()
        return source
    except Exception as exc:
        _raise_api_error(exc)


@router.delete("/sync-sources/{source_key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sync_source(
    source_key: str,
    lib: Library = Depends(require_lib("delete")),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_sync_source_api_enabled()
    try:
        await sync_sources_service.delete_sync_source(db, lib, source_key)
        await db.commit()
        return None
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/sync-sources/{source_key}/documents:upsert", response_model=SyncDocumentResult)
async def upsert_sync_document(
    source_key: str,
    body: SyncDocumentUpsertRequest,
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SyncDocumentResult:
    _require_sync_source_api_enabled()
    try:
        result = await sync_documents_service.upsert_sync_document(
            db, lib, source_key, body, created_by=user.id
        )
        await db.commit()
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/sync-sources/{source_key}/documents:delete", response_model=SyncDocumentResult)
async def delete_sync_document(
    source_key: str,
    body: SyncDocumentDeleteRequest,
    lib: Library = Depends(require_lib("delete")),
    db: AsyncSession = Depends(get_db),
) -> SyncDocumentResult:
    _require_sync_source_api_enabled()
    try:
        result = await sync_documents_service.delete_sync_document(db, lib, source_key, body)
        await db.commit()
        return result
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/sync-sources/{source_key}:sync", response_model=SyncBatchResponse)
async def sync_source_batch(
    source_key: str,
    body: SyncBatchRequest,
    lib: Library = Depends(require_lib("insert")),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SyncBatchResponse:
    _require_sync_source_api_enabled()
    if len(body.items) > settings.sync_batch_max_items:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "batch item limit exceeded")
    await _require_batch_delete_permission(body, user, lib, db)

    results: list[SyncDocumentResult] = []
    errors: list[SyncBatchError] = []
    for index, item in enumerate(body.items):
        try:
            if item.action == "upsert":
                if item.text is None or item.text == "":
                    raise ValueError("upsert item text is required")
                if len(item.text) > settings.sync_document_text_max_chars:
                    raise ValueError("upsert item text is too large")
                payload_data = item.model_dump(exclude_unset=True)
                payload_data.pop("action", None)
                result = await sync_documents_service.upsert_sync_document(
                    db,
                    lib,
                    source_key,
                    SyncDocumentUpsertRequest(**payload_data),
                    created_by=user.id,
                )
            else:
                result = await sync_documents_service.delete_sync_document(
                    db,
                    lib,
                    source_key,
                    SyncDocumentDeleteRequest(
                        external_id=item.external_id,
                        request_id=item.request_id,
                        idempotency_key=item.idempotency_key,
                        source_event_id=item.source_event_id,
                    ),
                )
            await db.commit()
            results.append(result)
        except Exception as exc:
            await db.rollback()
            errors.append(
                SyncBatchError(
                    index=index,
                    external_id=item.external_id,
                    action=item.action,
                    error=str(exc),
                )
            )

    response_status = "success" if not errors else "failed" if not results else "partial"
    return SyncBatchResponse(
        status=response_status,
        succeeded_count=len(results),
        failed_count=len(errors),
        results=results,
        errors=errors,
    )


@router.get("/documents/{document_id}/revisions", response_model=list[RevisionRead])
async def list_document_revisions(
    document_id: uuid.UUID,
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> list[RevisionRead]:
    try:
        return await evidence_read_service.list_document_revisions(db, lib, document_id)
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/documents/{document_id}/revisions/{revision_id}", response_model=RevisionRead)
async def get_document_revision(
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> RevisionRead:
    try:
        return await evidence_read_service.get_document_revision(db, lib, document_id, revision_id)
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/documents/{document_id}/revisions/{revision_id}/blocks", response_model=list[BlockRead])
async def list_revision_blocks(
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    block_kind: str | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> list[BlockRead]:
    try:
        return await evidence_read_service.list_revision_blocks(
            db,
            lib,
            document_id,
            revision_id,
            limit=limit,
            offset=offset,
            block_kind=block_kind,
            page=page,
        )
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/documents/{document_id}/revisions/{revision_id}/chunks", response_model=list[ChunkRead])
async def list_revision_chunks(
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> list[ChunkRead]:
    try:
        return await evidence_read_service.list_revision_chunks(
            db, lib, document_id, revision_id, limit=limit, offset=offset
        )
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/evidence/{evidence_id}", response_model=EvidenceRead)
async def get_evidence(
    evidence_id: uuid.UUID,
    revision_id: uuid.UUID | None = Query(default=None),
    window: int = Query(default=300, ge=0, le=3000),
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> EvidenceRead:
    try:
        return await evidence_read_service.get_evidence_detail(
            db, lib, evidence_id, revision_id=revision_id, window=window
        )
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/chunks/{chunk_id}/source", response_model=ChunkSourceRead)
async def get_chunk_source(
    chunk_id: uuid.UUID,
    revision_id: uuid.UUID | None = Query(default=None),
    window: int = Query(default=300, ge=0, le=3000),
    lib: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> ChunkSourceRead:
    try:
        return await evidence_read_service.get_chunk_source(
            db, lib, chunk_id, revision_id=revision_id, window=window
        )
    except Exception as exc:
        _raise_api_error(exc)
