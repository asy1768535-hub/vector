from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.models.user import User
from app.schemas.personal_library_scopes import (
    NamedScopeCreate,
    NamedScopeDelete,
    NamedScopeReplace,
    RemovedScopeItemRead,
    ResolvedScopeRead,
    ScopeLibraryRead,
    ScopeMetadataRead,
    ScopeSelection,
)
from app.services.organization_authorization import OrganizationAuthorizationError
from app.services.personal_library_scopes import (
    PersonalLibraryScopeError,
    ResolvedScope,
    create_named_scope,
    delete_named_scope,
    list_named_scopes,
    replace_named_scope,
    resolve_last_used_scope,
    resolve_named_scope,
    upsert_last_used_scope,
)


router = APIRouter(tags=["personal-library-scopes"])


def _require_runtime() -> None:
    if not settings.personal_library_scopes_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


def _http_error(exc: PersonalLibraryScopeError) -> HTTPException:
    if exc.code == "personal_scope_forbidden":
        return HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if exc.code == "personal_scope_not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code in {
        "personal_scope_incompatible",
        "personal_scope_limit_reached",
        "personal_scope_name_conflict",
        "personal_scope_state_changed",
    }:
        return HTTPException(status.HTTP_409_CONFLICT, exc.code)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)


def _metadata(scope, item_count: int) -> ScopeMetadataRead:
    return ScopeMetadataRead(
        scope_id=scope.id,
        organization_id=scope.organization_id,
        scope_kind=scope.scope_kind,
        name=scope.name,
        item_count=item_count,
        created_at=scope.created_at,
        updated_at=scope.updated_at,
    )


def _resolved(value: ResolvedScope) -> ResolvedScopeRead:
    return ResolvedScopeRead(
        scope_id=value.scope.id,
        organization_id=value.scope.organization_id,
        scope_kind=value.scope.scope_kind,
        name=value.scope.name,
        stored_item_count=len(value.libraries) + len(value.removed),
        libraries=[
            ScopeLibraryRead(
                library_id=library.id,
                library_slug=library.slug,
                library_name=library.name,
            )
            for library in value.libraries
        ],
        removed=[
            RemovedScopeItemRead(
                library_id=item.library_id,
                reason_codes=list(item.reason_codes),
            )
            for item in value.removed
        ],
        created_at=value.scope.created_at,
        updated_at=value.scope.updated_at,
    )


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "personal_scope_conflict") from exc
    except Exception:
        await db.rollback()
        raise


async def _rollback_conflict(db: AsyncSession, exc: IntegrityError) -> None:
    await db.rollback()
    raise HTTPException(status.HTTP_409_CONFLICT, "personal_scope_conflict") from exc


@router.get("/me/library-scopes", response_model=list[ScopeMetadataRead])
async def named_scopes(
    organization_id: uuid.UUID = Query(...),
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> list[ScopeMetadataRead]:
    _require_runtime()
    try:
        rows = await list_named_scopes(db, user=user, organization_id=organization_id)
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return [_metadata(row.scope, row.item_count) for row in rows]


@router.post(
    "/me/library-scopes",
    response_model=ScopeMetadataRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_scope(
    body: NamedScopeCreate,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ScopeMetadataRead:
    _require_runtime()
    try:
        scope = await create_named_scope(
            db,
            user=user,
            organization_id=body.organization_id,
            name=body.name,
            library_slugs=tuple(body.library_slugs),
        )
        await _commit(db)
        await db.refresh(scope)
    except OrganizationAuthorizationError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    except PersonalLibraryScopeError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_conflict(db, exc)
    except Exception:
        await db.rollback()
        raise
    return _metadata(scope, len(body.library_slugs))


@router.put("/me/library-scopes/last-used", response_model=ScopeMetadataRead)
async def upsert_last_used(
    body: ScopeSelection,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ScopeMetadataRead:
    _require_runtime()
    try:
        scope = await upsert_last_used_scope(
            db,
            user=user,
            organization_id=body.organization_id,
            library_slugs=tuple(body.library_slugs),
        )
        await _commit(db)
        await db.refresh(scope)
    except OrganizationAuthorizationError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    except PersonalLibraryScopeError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_conflict(db, exc)
    except Exception:
        await db.rollback()
        raise
    return _metadata(scope, len(body.library_slugs))


@router.get("/me/library-scopes/last-used/resolve", response_model=ResolvedScopeRead)
async def resolve_last_used(
    organization_id: uuid.UUID = Query(...),
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ResolvedScopeRead:
    _require_runtime()
    try:
        value = await resolve_last_used_scope(
            db,
            user=user,
            organization_id=organization_id,
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    except PersonalLibraryScopeError as exc:
        raise _http_error(exc) from exc
    return _resolved(value)


@router.put("/me/library-scopes/{scope_id}", response_model=ScopeMetadataRead)
async def replace_scope(
    scope_id: uuid.UUID,
    body: NamedScopeReplace,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ScopeMetadataRead:
    _require_runtime()
    try:
        scope = await replace_named_scope(
            db,
            user=user,
            scope_id=scope_id,
            organization_id=body.organization_id,
            expected_updated_at=body.expected_updated_at,
            name=body.name,
            library_slugs=tuple(body.library_slugs),
        )
        await _commit(db)
        await db.refresh(scope)
    except OrganizationAuthorizationError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    except PersonalLibraryScopeError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_conflict(db, exc)
    except Exception:
        await db.rollback()
        raise
    return _metadata(scope, len(body.library_slugs))


@router.delete("/me/library-scopes/{scope_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scope(
    scope_id: uuid.UUID,
    body: NamedScopeDelete,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_runtime()
    try:
        await delete_named_scope(
            db,
            user=user,
            scope_id=scope_id,
            organization_id=body.organization_id,
            expected_updated_at=body.expected_updated_at,
        )
        await _commit(db)
    except OrganizationAuthorizationError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    except PersonalLibraryScopeError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        await _rollback_conflict(db, exc)
    except Exception:
        await db.rollback()
        raise
    return None


@router.get("/me/library-scopes/{scope_id}/resolve", response_model=ResolvedScopeRead)
async def resolve_scope(
    scope_id: uuid.UUID,
    organization_id: uuid.UUID = Query(...),
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ResolvedScopeRead:
    _require_runtime()
    try:
        value = await resolve_named_scope(
            db,
            user=user,
            organization_id=organization_id,
            scope_id=scope_id,
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    except PersonalLibraryScopeError as exc:
        raise _http_error(exc) from exc
    return _resolved(value)
