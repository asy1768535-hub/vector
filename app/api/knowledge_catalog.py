from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import require_lib
from app.models.library import Library
from app.schemas.knowledge_catalog import (
    CatalogDocumentDetailRead,
    CatalogDocumentPageRead,
    CatalogEvidenceDetailRead,
    CatalogFileAccessRead,
)
from app.services.knowledge_catalog import (
    get_catalog_document_detail,
    get_catalog_evidence_detail,
    list_catalog_documents,
    prepare_catalog_file_access,
)
from app.services.knowledge_catalog_contracts import (
    CatalogDocumentQuery,
    KnowledgeCatalogError,
)
from app.services.object_storage import build_object_storage_adapter
from app.services.object_storage_contracts import ObjectStorageError
from app.services.revision_files import require_storage_adapter_identity


router = APIRouter(
    prefix="/libraries/{slug}/catalog",
    tags=["knowledge-catalog"],
)


def require_knowledge_catalog_enabled() -> None:
    if not settings.knowledge_catalog_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


def _http_error(exc: KnowledgeCatalogError) -> HTTPException:
    if exc.code == "catalog_not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code == "catalog_invariant_failed":
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
        )
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)


@router.get(
    "/documents",
    response_model=CatalogDocumentPageRead,
)
async def catalog_documents(
    title: str | None = Query(default=None, min_length=1, max_length=160),
    document_status: str | None = Query(
        default=None,
        alias="status",
        pattern="^(pending|processing|ready|failed)$",
    ),
    classification_state: str | None = Query(
        default=None,
        pattern="^(unclassified|pending_review|classified|failed)$",
    ),
    label_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None, min_length=1, max_length=2048),
    limit: int = Query(default=50, ge=1, le=100),
    _: None = Depends(require_knowledge_catalog_enabled),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> CatalogDocumentPageRead:
    try:
        return await list_catalog_documents(
            db,
            library=library,
            query=CatalogDocumentQuery(
                title_query=title,
                document_status=document_status,
                classification_state=classification_state,
                label_id=label_id,
                limit=limit,
            ),
            cursor_value=cursor,
        )
    except KnowledgeCatalogError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.get(
    "/documents/{document_id}",
    response_model=CatalogDocumentDetailRead,
)
async def catalog_document_detail(
    document_id: uuid.UUID,
    _: None = Depends(require_knowledge_catalog_enabled),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> CatalogDocumentDetailRead:
    try:
        return await get_catalog_document_detail(
            db,
            library=library,
            document_id=document_id,
        )
    except KnowledgeCatalogError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.get(
    "/evidence/{evidence_id}",
    response_model=CatalogEvidenceDetailRead,
)
async def catalog_evidence_detail(
    evidence_id: uuid.UUID,
    _: None = Depends(require_knowledge_catalog_enabled),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> CatalogEvidenceDetailRead:
    try:
        return await get_catalog_evidence_detail(
            db,
            library=library,
            evidence_id=evidence_id,
        )
    except KnowledgeCatalogError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.get(
    "/files/{revision_file_id}/access",
    response_model=CatalogFileAccessRead,
)
async def catalog_file_access(
    slug: str,
    revision_file_id: uuid.UUID,
    _: None = Depends(require_knowledge_catalog_enabled),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> CatalogFileAccessRead:
    if not settings.revision_file_storage_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    try:
        prepared = await prepare_catalog_file_access(
            db,
            library=library,
            revision_file_id=revision_file_id,
        )
        await db.rollback()
        adapter = build_object_storage_adapter()
        require_storage_adapter_identity(adapter, prepared.access.locator)
        expires_seconds = settings.document_storage_signed_url_seconds
        signed_url = await adapter.download_url(
            prepared.access.locator.object_key,
            prepared.access.locator.object_version,
            expires_seconds,
        )
    except KnowledgeCatalogError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except ObjectStorageError as exc:
        await db.rollback()
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code in {"object_not_found", "revision_file_unavailable"}
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(code, "stored document file is unavailable") from None
    if signed_url is None:
        access_mode = "proxy"
        url = f"/libraries/{quote(slug, safe='')}/documents/{prepared.document_id}/file"
        expires_at = None
    else:
        if not isinstance(signed_url, str) or not signed_url.strip() or len(signed_url) > 4096:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "stored document file is unavailable",
            )
        access_mode = "signed_url"
        url = signed_url
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)
    return CatalogFileAccessRead(
        revision_file_id=prepared.revision_file_id,
        document_id=prepared.document_id,
        document_revision_id=prepared.document_revision_id,
        file_name=prepared.access.file_name,
        content_type=prepared.access.content_type,
        size_bytes=prepared.access.size_bytes,
        sha256=prepared.access.sha256,
        access_mode=access_mode,
        url=url,
        expires_at=expires_at,
    )
