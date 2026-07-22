from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.models.user import User
from app.schemas.classification_taxonomies import (
    ClassificationLabelCreate,
    ClassificationLabelRead,
    ClassificationLabelUpdate,
    LibraryClassificationLabelsReplace,
    LibraryClassificationSelectionRead,
    TaxonomyActivate,
    TaxonomyCreate,
    TaxonomyDraftUpdate,
    TaxonomyRead,
    TaxonomyVersionCopy,
)
from app.services.classification_taxonomy_contracts import (
    ActivateTaxonomyCommand,
    ClassificationTaxonomyError,
    CopyTaxonomyVersionCommand,
    CreateClassificationLabelCommand,
    CreateTaxonomyCommand,
    ReplaceLibraryClassificationLabelsCommand,
    UpdateClassificationLabelCommand,
    UpdateTaxonomyDraftCommand,
)
from app.services.classification_taxonomies import (
    LibraryClassificationSelection,
    activate_taxonomy,
    copy_taxonomy_version,
    create_classification_label,
    create_taxonomy,
    get_library_classification_selection,
    list_classification_labels,
    list_taxonomies,
    replace_library_classification_labels,
    update_classification_label,
    update_taxonomy_draft,
)
from app.services.organization_authorization import (
    OrganizationAdminContext,
    OrganizationAuthorizationError,
    resolve_organization_admin,
)


router = APIRouter(tags=["classification-taxonomies"])


@dataclass(frozen=True, slots=True)
class ClassificationAdminRequestContext:
    user: User
    admin: OrganizationAdminContext


async def require_classification_admin(
    organization_id: uuid.UUID,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ClassificationAdminRequestContext:
    if not settings.classification_taxonomy_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    try:
        admin = await resolve_organization_admin(
            db,
            organization_id=organization_id,
            user=user,
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return ClassificationAdminRequestContext(user=user, admin=admin)


def _http_error(exc: ClassificationTaxonomyError) -> HTTPException:
    if exc.code == "classification_admin_forbidden":
        return HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if exc.code in {
        "classification_scope_not_found",
        "classification_taxonomy_not_found",
        "classification_label_not_found",
        "classification_library_not_found",
        "classification_active_taxonomy_not_found",
    }:
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code in {
        "classification_taxonomy_conflict",
        "classification_label_conflict",
        "classification_taxonomy_state_changed",
        "classification_label_state_changed",
        "classification_library_selection_state_changed",
        "classification_taxonomy_immutable",
        "classification_source_not_published",
        "classification_taxonomy_empty",
        "classification_parent_scope_invalid",
        "classification_parent_disabled",
        "classification_parent_cycle",
        "classification_label_limit_exceeded",
    }:
        return HTTPException(status.HTTP_409_CONFLICT, exc.code)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)


def _taxonomy_read(value) -> TaxonomyRead:
    return TaxonomyRead(
        id=value.id,
        organization_id=value.organization_id,
        version_key=value.version_key,
        version_no=value.version_no,
        status=value.status,
        parent_version_id=value.parent_version_id,
        description=value.description,
        created_by_user_id=value.created_by_user_id,
        activated_by_user_id=value.activated_by_user_id,
        activated_at=value.activated_at,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _label_read(value) -> ClassificationLabelRead:
    return ClassificationLabelRead(
        id=value.id,
        taxonomy_version_id=value.taxonomy_version_id,
        key=value.key,
        label=value.label,
        description=value.description,
        parent_label_id=value.parent_label_id,
        sort_order=value.sort_order,
        status=value.status,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _selection_read(value: LibraryClassificationSelection) -> LibraryClassificationSelectionRead:
    return LibraryClassificationSelectionRead(
        organization_id=value.library.organization_id,
        library_id=value.library.id,
        library_slug=value.library.slug,
        library_name=value.library.name,
        taxonomy=_taxonomy_read(value.taxonomy),
        labels=[_label_read(label) for label in value.labels],
    )


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "classification_taxonomy_conflict",
        ) from exc
    except Exception:
        await db.rollback()
        raise


async def _rollback_integrity(db: AsyncSession, exc: IntegrityError) -> None:
    await db.rollback()
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        "classification_taxonomy_conflict",
    ) from exc


@router.get(
    "/organizations/{organization_id}/classification-taxonomies",
    response_model=list[TaxonomyRead],
)
async def taxonomy_versions(
    organization_id: uuid.UUID,
    context: ClassificationAdminRequestContext = Depends(require_classification_admin),
    db: AsyncSession = Depends(get_db),
) -> list[TaxonomyRead]:
    try:
        rows = await list_taxonomies(
            db,
            organization_id=organization_id,
            actor_user_id=context.user.id,
        )
    except ClassificationTaxonomyError as exc:
        raise _http_error(exc) from exc
    return [_taxonomy_read(row) for row in rows]


@router.post(
    "/organizations/{organization_id}/classification-taxonomies",
    response_model=TaxonomyRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_taxonomy_version(
    organization_id: uuid.UUID,
    body: TaxonomyCreate,
    context: ClassificationAdminRequestContext = Depends(require_classification_admin),
    db: AsyncSession = Depends(get_db),
) -> TaxonomyRead:
    try:
        taxonomy = await create_taxonomy(
            db,
            CreateTaxonomyCommand(
                organization_id=organization_id,
                actor_user_id=context.user.id,
                version_key=body.version_key,
                version_no=body.version_no,
                description=body.description,
            ),
        )
        await _commit(db)
        await db.refresh(taxonomy)
    except ClassificationTaxonomyError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_integrity(db, exc)
    except Exception:
        await db.rollback()
        raise
    return _taxonomy_read(taxonomy)


@router.put(
    "/organizations/{organization_id}/classification-taxonomies/{taxonomy_id}",
    response_model=TaxonomyRead,
)
async def replace_taxonomy_draft(
    organization_id: uuid.UUID,
    taxonomy_id: uuid.UUID,
    body: TaxonomyDraftUpdate,
    context: ClassificationAdminRequestContext = Depends(require_classification_admin),
    db: AsyncSession = Depends(get_db),
) -> TaxonomyRead:
    try:
        taxonomy = await update_taxonomy_draft(
            db,
            UpdateTaxonomyDraftCommand(
                organization_id=organization_id,
                actor_user_id=context.user.id,
                taxonomy_id=taxonomy_id,
                expected_status=body.expected_status,
                expected_updated_at=body.expected_updated_at,
                description=body.description,
            ),
        )
        await _commit(db)
        await db.refresh(taxonomy)
    except ClassificationTaxonomyError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_integrity(db, exc)
    except Exception:
        await db.rollback()
        raise
    return _taxonomy_read(taxonomy)


@router.post(
    "/organizations/{organization_id}/classification-taxonomies/{taxonomy_id}/versions",
    response_model=TaxonomyRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_next_taxonomy_version(
    organization_id: uuid.UUID,
    taxonomy_id: uuid.UUID,
    body: TaxonomyVersionCopy,
    context: ClassificationAdminRequestContext = Depends(require_classification_admin),
    db: AsyncSession = Depends(get_db),
) -> TaxonomyRead:
    try:
        taxonomy = await copy_taxonomy_version(
            db,
            CopyTaxonomyVersionCommand(
                organization_id=organization_id,
                actor_user_id=context.user.id,
                source_taxonomy_id=taxonomy_id,
                expected_source_status=body.expected_source_status,
                expected_source_updated_at=body.expected_source_updated_at,
                description=body.description,
            ),
        )
        await _commit(db)
        await db.refresh(taxonomy)
    except ClassificationTaxonomyError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_integrity(db, exc)
    except Exception:
        await db.rollback()
        raise
    return _taxonomy_read(taxonomy)


@router.post(
    "/organizations/{organization_id}/classification-taxonomies/{taxonomy_id}/activate",
    response_model=TaxonomyRead,
)
async def activate_taxonomy_version(
    organization_id: uuid.UUID,
    taxonomy_id: uuid.UUID,
    body: TaxonomyActivate,
    context: ClassificationAdminRequestContext = Depends(require_classification_admin),
    db: AsyncSession = Depends(get_db),
) -> TaxonomyRead:
    try:
        taxonomy = await activate_taxonomy(
            db,
            ActivateTaxonomyCommand(
                organization_id=organization_id,
                actor_user_id=context.user.id,
                taxonomy_id=taxonomy_id,
                expected_status=body.expected_status,
                expected_updated_at=body.expected_updated_at,
            ),
        )
        await _commit(db)
        await db.refresh(taxonomy)
    except ClassificationTaxonomyError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_integrity(db, exc)
    except Exception:
        await db.rollback()
        raise
    return _taxonomy_read(taxonomy)


@router.get(
    "/organizations/{organization_id}/classification-taxonomies/{taxonomy_id}/labels",
    response_model=list[ClassificationLabelRead],
)
async def taxonomy_labels(
    organization_id: uuid.UUID,
    taxonomy_id: uuid.UUID,
    context: ClassificationAdminRequestContext = Depends(require_classification_admin),
    db: AsyncSession = Depends(get_db),
) -> list[ClassificationLabelRead]:
    try:
        rows = await list_classification_labels(
            db,
            organization_id=organization_id,
            actor_user_id=context.user.id,
            taxonomy_id=taxonomy_id,
        )
    except ClassificationTaxonomyError as exc:
        raise _http_error(exc) from exc
    return [_label_read(row) for row in rows]


@router.post(
    "/organizations/{organization_id}/classification-taxonomies/{taxonomy_id}/labels",
    response_model=ClassificationLabelRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_taxonomy_label(
    organization_id: uuid.UUID,
    taxonomy_id: uuid.UUID,
    body: ClassificationLabelCreate,
    context: ClassificationAdminRequestContext = Depends(require_classification_admin),
    db: AsyncSession = Depends(get_db),
) -> ClassificationLabelRead:
    try:
        label = await create_classification_label(
            db,
            CreateClassificationLabelCommand(
                organization_id=organization_id,
                actor_user_id=context.user.id,
                taxonomy_id=taxonomy_id,
                expected_taxonomy_updated_at=body.expected_taxonomy_updated_at,
                key=body.key,
                label=body.label,
                description=body.description,
                parent_label_id=body.parent_label_id,
                sort_order=body.sort_order,
                status=body.status,
            ),
        )
        await _commit(db)
        await db.refresh(label)
    except ClassificationTaxonomyError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_integrity(db, exc)
    except Exception:
        await db.rollback()
        raise
    return _label_read(label)


@router.put(
    "/organizations/{organization_id}/classification-taxonomies/{taxonomy_id}/labels/{label_id}",
    response_model=ClassificationLabelRead,
)
async def replace_taxonomy_label(
    organization_id: uuid.UUID,
    taxonomy_id: uuid.UUID,
    label_id: uuid.UUID,
    body: ClassificationLabelUpdate,
    context: ClassificationAdminRequestContext = Depends(require_classification_admin),
    db: AsyncSession = Depends(get_db),
) -> ClassificationLabelRead:
    try:
        label = await update_classification_label(
            db,
            UpdateClassificationLabelCommand(
                organization_id=organization_id,
                actor_user_id=context.user.id,
                taxonomy_id=taxonomy_id,
                label_id=label_id,
                expected_taxonomy_updated_at=body.expected_taxonomy_updated_at,
                expected_updated_at=body.expected_updated_at,
                key=body.key,
                label=body.label,
                description=body.description,
                parent_label_id=body.parent_label_id,
                sort_order=body.sort_order,
                status=body.status,
            ),
        )
        await _commit(db)
        await db.refresh(label)
    except ClassificationTaxonomyError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_integrity(db, exc)
    except Exception:
        await db.rollback()
        raise
    return _label_read(label)


@router.get(
    "/organizations/{organization_id}/libraries/{library_id}/classification-labels",
    response_model=LibraryClassificationSelectionRead,
)
async def library_classification_labels(
    organization_id: uuid.UUID,
    library_id: uuid.UUID,
    context: ClassificationAdminRequestContext = Depends(require_classification_admin),
    db: AsyncSession = Depends(get_db),
) -> LibraryClassificationSelectionRead:
    try:
        selection = await get_library_classification_selection(
            db,
            organization_id=organization_id,
            actor_user_id=context.user.id,
            library_id=library_id,
        )
    except ClassificationTaxonomyError as exc:
        raise _http_error(exc) from exc
    return _selection_read(selection)


@router.put(
    "/organizations/{organization_id}/libraries/{library_id}/classification-labels",
    response_model=LibraryClassificationSelectionRead,
)
async def replace_library_labels(
    organization_id: uuid.UUID,
    library_id: uuid.UUID,
    body: LibraryClassificationLabelsReplace,
    context: ClassificationAdminRequestContext = Depends(require_classification_admin),
    db: AsyncSession = Depends(get_db),
) -> LibraryClassificationSelectionRead:
    try:
        selection = await replace_library_classification_labels(
            db,
            ReplaceLibraryClassificationLabelsCommand(
                organization_id=organization_id,
                actor_user_id=context.user.id,
                library_id=library_id,
                taxonomy_id=body.taxonomy_version_id,
                expected_taxonomy_updated_at=body.expected_taxonomy_updated_at,
                expected_label_ids=tuple(body.expected_label_ids),
                label_ids=tuple(body.label_ids),
            ),
        )
        await _commit(db)
    except ClassificationTaxonomyError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_integrity(db, exc)
    except Exception:
        await db.rollback()
        raise
    return _selection_read(selection)
