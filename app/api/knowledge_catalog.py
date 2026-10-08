from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps as deps_module
from app.auth.backend import current_cookie_user
from app.casbin.enforcer import has_permission
from app.config import settings
from app.db import get_db
from app.deps import current_active_user, require_lib
from app.models.library import Library
from app.models.user import User
from app.schemas.document_processing import (
    DocumentProcessingRead,
    DocumentProcessingRetryRequest,
)
from app.schemas.knowledge_catalog import (
    CatalogParsingCoverageRead,
    CatalogPdfCoverageReviewApplyRequest,
    CatalogPdfCoverageReviewMutationRead,
    CatalogPdfCoverageReviewRollbackRequest,
    CatalogDocumentDetailRead,
    CatalogDocumentPageRead,
    CatalogEvidenceDetailRead,
    CatalogFileAccessRead,
    CatalogUploaderOptionsRead,
)
from app.services.knowledge_catalog import (
    get_catalog_document_detail,
    get_catalog_evidence_detail,
    list_catalog_documents,
    list_catalog_uploader_options,
    prepare_catalog_file_access,
)
from app.services.knowledge_catalog_contracts import (
    CatalogDocumentQuery,
    KnowledgeCatalogError,
)
from app.services.pdf_coverage_reviews import (
    PdfCoverageReviewError,
    apply_pdf_coverage_review_audit,
    rollback_pdf_coverage_review_audit,
)
from app.services.document_processing_contracts import (
    DocumentProcessingError,
    ProcessingStage,
)
from app.services.document_processing_diagnostics import (
    get_document_processing_diagnostics,
    retry_document_processing_stage,
)
from app.services.object_storage import build_object_storage_adapter
from app.services.object_storage_contracts import ObjectStorageError
from app.services.revision_files import require_storage_adapter_identity
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    authorize_library_management,
)


router = APIRouter(
    prefix="/libraries/{slug}/catalog",
    tags=["knowledge-catalog"],
)


@dataclass(frozen=True, slots=True)
class CatalogManagementContext:
    user: User
    library: Library


def require_knowledge_catalog_enabled() -> None:
    if not settings.knowledge_catalog_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


async def require_catalog_management(
    slug: str,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> CatalogManagementContext:
    require_knowledge_catalog_enabled()
    library = await deps_module.load_active_library(slug, db)
    if library is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    try:
        await authorize_library_management(db, user=user, library=library)
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return CatalogManagementContext(user, library)


def _http_error(exc: KnowledgeCatalogError) -> HTTPException:
    if exc.code == "catalog_not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code == "catalog_invariant_failed":
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "catalog_unavailable",
        )
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)


def _processing_http_error(exc: DocumentProcessingError) -> HTTPException:
    if exc.code == "processing_document_not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code == "processing_invariant_failed":
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code)
    return HTTPException(status.HTTP_409_CONFLICT, exc.code)


def _coverage_review_http_error(exc: PdfCoverageReviewError) -> HTTPException:
    if exc.code == "coverage_review_document_not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code in {
        "coverage_review_stale_revision", "coverage_review_revision_unavailable",
        "coverage_review_source_mismatch", "coverage_review_source_binding_mismatch",
        "coverage_review_page_conflict", "coverage_review_page_not_unprocessed",
        "coverage_review_idempotency_conflict", "coverage_review_apply_event_not_found",
        "coverage_review_batch_invalid", "coverage_review_duplicate_page",
        "coverage_review_evidence_invalid",
    }:
        return HTTPException(status.HTTP_409_CONFLICT, exc.code)
    if exc.code in {"coverage_review_report_unavailable", "coverage_review_history_limit"}:
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)


async def _catalog_viewer_permissions(
    db: AsyncSession,
    *,
    user: User,
    library: Library,
) -> tuple[bool, bool]:
    can_manage = user.is_superuser
    if not can_manage:
        try:
            await authorize_library_management(db, user=user, library=library)
            can_manage = True
        except OrganizationAuthorizationError:
            pass
    can_delete_any = bool(
        user.is_superuser or has_permission(str(user.id), library.slug, "delete")
    )
    return can_manage, can_delete_any


@router.get(
    "/uploader-options",
    response_model=CatalogUploaderOptionsRead,
)
async def catalog_uploader_options(
    _: None = Depends(require_knowledge_catalog_enabled),
    user: User = Depends(current_active_user),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> CatalogUploaderOptionsRead:
    try:
        can_manage, _ = await _catalog_viewer_permissions(db, user=user, library=library)
        return await list_catalog_uploader_options(
            db,
            library=library,
            viewer=user,
            reveal_email=can_manage,
        )
    except KnowledgeCatalogError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


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
    uploader_id: uuid.UUID | None = Query(default=None),
    include_system_uploader: bool = Query(default=False),
    uploaded_from: date | None = Query(default=None),
    uploaded_to: date | None = Query(default=None),
    cursor: str | None = Query(default=None, min_length=1, max_length=2048),
    limit: int = Query(default=20, ge=1, le=50),
    _: None = Depends(require_knowledge_catalog_enabled),
    user: User = Depends(current_active_user),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> CatalogDocumentPageRead:
    try:
        can_manage, can_delete_any = await _catalog_viewer_permissions(
            db,
            user=user,
            library=library,
        )
        return await list_catalog_documents(
            db,
            library=library,
            query=CatalogDocumentQuery(
                title_query=title,
                document_status=document_status,
                classification_state=classification_state,
                label_id=label_id,
                uploader_id=uploader_id,
                include_system_uploader=include_system_uploader,
                uploaded_from=uploaded_from,
                uploaded_to=uploaded_to,
                limit=limit,
            ),
            cursor_value=cursor,
            include_uploader=True,
            reveal_uploader_email=can_manage,
            viewer_id=user.id,
            can_delete_any=can_delete_any,
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
    user: User = Depends(current_active_user),
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> CatalogDocumentDetailRead:
    try:
        can_manage, can_delete_any = await _catalog_viewer_permissions(
            db,
            user=user,
            library=library,
        )
        return await get_catalog_document_detail(
            db,
            library=library,
            document_id=document_id,
            include_uploader=True,
            reveal_uploader_email=can_manage,
            viewer_id=user.id,
            can_delete_any=can_delete_any,
        )
    except KnowledgeCatalogError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.get(
    "/documents/{document_id}/processing",
    response_model=DocumentProcessingRead,
)
async def catalog_document_processing(
    document_id: uuid.UUID,
    _: None = Depends(require_knowledge_catalog_enabled),
    context: CatalogManagementContext = Depends(require_catalog_management),
    db: AsyncSession = Depends(get_db),
) -> DocumentProcessingRead:
    try:
        return await get_document_processing_diagnostics(
            db,
            library=context.library,
            document_id=document_id,
        )
    except DocumentProcessingError as exc:
        await db.rollback()
        raise _processing_http_error(exc) from exc


@router.post(
    "/documents/{document_id}/processing/{stage}/retry",
    response_model=DocumentProcessingRead,
)
async def retry_catalog_document_processing(
    document_id: uuid.UUID,
    stage: ProcessingStage,
    body: DocumentProcessingRetryRequest,
    _: None = Depends(require_knowledge_catalog_enabled),
    context: CatalogManagementContext = Depends(require_catalog_management),
    db: AsyncSession = Depends(get_db),
) -> DocumentProcessingRead:
    try:
        result = await retry_document_processing_stage(
            db,
            library=context.library,
            document_id=document_id,
            stage=stage,
            source_job_id=body.source_job_id,
            observed_retry_generation=body.retry_generation,
            actor_user_id=context.user.id,
        )
        await db.commit()
        return result
    except DocumentProcessingError as exc:
        await db.rollback()
        raise _processing_http_error(exc) from exc


@router.post(
    "/documents/{document_id}/parsing-coverage/reviews",
    response_model=CatalogPdfCoverageReviewMutationRead,
)
async def apply_catalog_pdf_coverage_reviews(
    document_id: uuid.UUID,
    body: CatalogPdfCoverageReviewApplyRequest,
    _: None = Depends(require_knowledge_catalog_enabled),
    context: CatalogManagementContext = Depends(require_catalog_management),
    db: AsyncSession = Depends(get_db),
) -> CatalogPdfCoverageReviewMutationRead:
    try:
        changed, report = await apply_pdf_coverage_review_audit(
            db,
            library=context.library,
            document_id=document_id,
            revision_id=body.revision_id,
            source_sha256=body.source_sha256,
            idempotency_key=body.idempotency_key,
            reviews=[item.model_dump() for item in body.reviews],
            actor_user_id=context.user.id,
        )
        await db.commit()
        return CatalogPdfCoverageReviewMutationRead(
            operation_key=body.idempotency_key,
            changed=changed,
            parsing_coverage=CatalogParsingCoverageRead.model_validate(report),
        )
    except PdfCoverageReviewError as exc:
        await db.rollback()
        raise _coverage_review_http_error(exc) from exc


@router.post(
    "/documents/{document_id}/parsing-coverage/reviews/rollback",
    response_model=CatalogPdfCoverageReviewMutationRead,
)
async def rollback_catalog_pdf_coverage_reviews(
    document_id: uuid.UUID,
    body: CatalogPdfCoverageReviewRollbackRequest,
    _: None = Depends(require_knowledge_catalog_enabled),
    context: CatalogManagementContext = Depends(require_catalog_management),
    db: AsyncSession = Depends(get_db),
) -> CatalogPdfCoverageReviewMutationRead:
    try:
        changed, report = await rollback_pdf_coverage_review_audit(
            db,
            library=context.library,
            document_id=document_id,
            revision_id=body.revision_id,
            source_sha256=body.source_sha256,
            idempotency_key=body.idempotency_key,
            apply_idempotency_key=body.apply_idempotency_key,
            actor_user_id=context.user.id,
        )
        await db.commit()
        return CatalogPdfCoverageReviewMutationRead(
            operation_key=body.idempotency_key,
            changed=changed,
            parsing_coverage=CatalogParsingCoverageRead.model_validate(report),
        )
    except PdfCoverageReviewError as exc:
        await db.rollback()
        raise _coverage_review_http_error(exc) from exc


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
        adapter = build_object_storage_adapter(
            provider=prepared.access.locator.provider
        )
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
