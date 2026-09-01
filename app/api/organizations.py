from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi_users.exceptions import InvalidPasswordException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_cookie_user, current_superuser
from app.auth.user_manager import UserManager, get_user_manager
from app.config import settings
from app.db import get_db
from app.models.user import User
from app.schemas.organizations import (
    OrganizationMemberCreate,
    OrganizationMemberRead,
    OrganizationMemberUpdate,
    OrganizationPasswordReset,
    OrganizationPermissionGrant,
    OrganizationPermissionRead,
    OrganizationPermissionRevoke,
    OrganizationRolloutRead,
    OrganizationRolloutUpdate,
    UserOrganizationRead,
)
from app.schemas.users import UserCreate
from app.services.organization_accounts import (
    OrganizationAccountCreateCommand,
    OrganizationAccountError,
    OrganizationAccountResult,
    change_organization_account_membership,
    create_organization_account,
    list_organization_members,
    list_user_organizations,
    reset_organization_account_password,
)
from app.services.organization_authorization import (
    OrganizationAdminContext,
    OrganizationAuthorizationError,
    resolve_organization_admin,
)
from app.services.organization_permissions import (
    OrganizationPermissionError,
    PermissionMutationResult,
    grant_organization_permissions,
    list_organization_user_permissions,
    revoke_organization_permissions,
)
from app.services.organization_rollouts import (
    OrganizationRolloutError,
    list_rollouts,
    set_rollout,
)


router = APIRouter(tags=["organizations"])


def _require_runtime() -> None:
    if not settings.organization_authorization_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


async def require_organization_admin(
    organization_id: uuid.UUID,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> OrganizationAdminContext:
    _require_runtime()
    try:
        return await resolve_organization_admin(
            db,
            organization_id=organization_id,
            user=user,
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc


def _account_http_error(exc: OrganizationAccountError) -> HTTPException:
    if exc.code in {
        "organization_account_scope_not_found",
        "organization_account_membership_not_found",
        "organization_account_not_found",
    }:
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code in {
        "organization_account_conflict",
        "organization_account_shared",
        "organization_last_admin",
        "organization_account_self_admin_change",
        "organization_membership_state_changed",
    }:
        return HTTPException(status.HTTP_409_CONFLICT, exc.code)
    if exc.code == "organization_account_admin_forbidden":
        return HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return HTTPException(status.HTTP_400_BAD_REQUEST, exc.code)


def _permission_http_error(exc: OrganizationPermissionError) -> HTTPException:
    if exc.code == "organization_permission_compensation_failed":
        return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, exc.code)
    if exc.code in {
        "organization_permission_scope_not_found",
        "organization_permission_target_not_found",
        "organization_permission_library_not_found",
    }:
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code == "organization_permission_admin_forbidden":
        return HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return HTTPException(status.HTTP_400_BAD_REQUEST, exc.code)


def _rollout_http_error(exc: OrganizationRolloutError) -> HTTPException:
    if exc.code == "organization_not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code == "rollout_version_conflict":
        return HTTPException(status.HTTP_409_CONFLICT, exc.code)
    return HTTPException(status.HTTP_400_BAD_REQUEST, exc.code)


def _member_read(result: OrganizationAccountResult) -> OrganizationMemberRead:
    return OrganizationMemberRead(
        membership_id=result.membership.id,
        organization_id=result.membership.organization_id,
        user_id=result.user.id,
        email=result.user.email,
        username=result.user.username,
        display_name=result.user.display_name,
        role=result.membership.role,
        status=result.membership.status,
        is_active=result.user.is_active and result.user.deleted_at is None,
        disabled_at=result.membership.disabled_at,
        created_at=result.membership.created_at,
    )


@router.get("/me/organizations", response_model=list[UserOrganizationRead])
async def my_organizations(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> list[UserOrganizationRead]:
    _require_runtime()
    rows = await list_user_organizations(db, user_id=user.id, limit=limit)
    return [
        UserOrganizationRead(
            organization_id=row.organization.id,
            slug=row.organization.slug,
            name=row.organization.name,
            role=row.membership.role,
        )
        for row in rows
    ]


@router.get(
    "/organizations/{organization_id}/rollouts",
    response_model=list[OrganizationRolloutRead],
)
async def organization_rollouts(
    organization_id: uuid.UUID,
    _: OrganizationAdminContext = Depends(require_organization_admin),
    db: AsyncSession = Depends(get_db),
) -> list[OrganizationRolloutRead]:
    try:
        rows = await list_rollouts(db, organization_id)
    except OrganizationRolloutError as exc:
        raise _rollout_http_error(exc) from exc
    return [OrganizationRolloutRead.model_validate(row, from_attributes=True) for row in rows]


@router.put(
    "/organizations/{organization_id}/rollouts/{capability}",
    response_model=OrganizationRolloutRead,
)
async def update_organization_rollout(
    organization_id: uuid.UUID,
    capability: str,
    body: OrganizationRolloutUpdate,
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> OrganizationRolloutRead:
    _require_runtime()
    try:
        row = await set_rollout(
            db,
            organization_id=organization_id,
            capability=capability,
            enabled=body.enabled,
            expected_version=body.expected_version,
            actor_user_id=actor.id,
        )
        await db.commit()
    except OrganizationRolloutError as exc:
        await db.rollback()
        raise _rollout_http_error(exc) from exc
    except Exception:
        await db.rollback()
        raise
    return OrganizationRolloutRead.model_validate(row, from_attributes=True)


@router.get(
    "/organizations/{organization_id}/members",
    response_model=list[OrganizationMemberRead],
)
async def organization_members(
    organization_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: OrganizationAdminContext = Depends(require_organization_admin),
    db: AsyncSession = Depends(get_db),
) -> list[OrganizationMemberRead]:
    try:
        rows = await list_organization_members(
            db,
            organization_id=organization_id,
            actor_user_id=context.user_id,
            limit=limit,
            offset=offset,
        )
    except OrganizationAccountError as exc:
        raise _account_http_error(exc) from exc
    return [_member_read(row) for row in rows]


@router.post(
    "/organizations/{organization_id}/members",
    response_model=OrganizationMemberRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization_member(
    organization_id: uuid.UUID,
    body: OrganizationMemberCreate,
    context: OrganizationAdminContext = Depends(require_organization_admin),
    user_manager: UserManager = Depends(get_user_manager),
    db: AsyncSession = Depends(get_db),
) -> OrganizationMemberRead:
    validation_payload = UserCreate(
        email=body.email,
        password=body.password,
        is_active=True,
        is_superuser=False,
        is_verified=False,
        username=body.username,
        display_name=body.display_name,
    )
    try:
        await user_manager.validate_password(body.password, validation_payload)
    except InvalidPasswordException as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "password is invalid") from exc
    try:
        result = await create_organization_account(
            db,
            OrganizationAccountCreateCommand(
                organization_id=organization_id,
                actor_user_id=context.user_id,
                email=str(body.email),
                username=body.username,
                display_name=body.display_name,
                role=body.role,
            ),
            password_hash=user_manager.password_helper.hash(body.password),
        )
        await db.commit()
        await db.refresh(result.user)
        await db.refresh(result.membership)
    except OrganizationAccountError as exc:
        await db.rollback()
        raise _account_http_error(exc) from exc
    except Exception:
        await db.rollback()
        raise
    return _member_read(result)


@router.patch(
    "/organizations/{organization_id}/members/{membership_id}",
    response_model=OrganizationMemberRead,
)
async def update_organization_member(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    body: OrganizationMemberUpdate,
    context: OrganizationAdminContext = Depends(require_organization_admin),
    db: AsyncSession = Depends(get_db),
) -> OrganizationMemberRead:
    try:
        membership = await change_organization_account_membership(
            db,
            organization_id=organization_id,
            actor_user_id=context.user_id,
            membership_id=membership_id,
            expected_role=body.expected_role,
            expected_status=body.expected_status,
            role=body.role,
            status=body.status,
        )
        user = await db.get(User, membership.user_id)
        if user is None:
            raise OrganizationAccountError("organization_account_not_found")
        await db.commit()
        await db.refresh(membership)
    except OrganizationAccountError as exc:
        await db.rollback()
        raise _account_http_error(exc) from exc
    except Exception:
        await db.rollback()
        raise
    return _member_read(OrganizationAccountResult(user=user, membership=membership))


@router.post(
    "/organizations/{organization_id}/members/{user_id}/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def reset_organization_member_password(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    body: OrganizationPasswordReset,
    context: OrganizationAdminContext = Depends(require_organization_admin),
    user_manager: UserManager = Depends(get_user_manager),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await user_manager.validate_password(
            body.password,
            UserCreate(email="password-validation@example.com", password=body.password),
        )
    except InvalidPasswordException as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "password is invalid") from exc
    try:
        await reset_organization_account_password(
            db,
            organization_id=organization_id,
            actor_user_id=context.user_id,
            target_user_id=user_id,
            password_hash=user_manager.password_helper.hash(body.password),
        )
        await db.commit()
    except OrganizationAccountError as exc:
        await db.rollback()
        raise _account_http_error(exc) from exc
    except Exception:
        await db.rollback()
        raise
    return None


@router.get(
    "/organizations/{organization_id}/permissions",
    response_model=list[OrganizationPermissionRead],
)
async def organization_permissions(
    organization_id: uuid.UUID,
    user_id: uuid.UUID = Query(...),
    context: OrganizationAdminContext = Depends(require_organization_admin),
    db: AsyncSession = Depends(get_db),
) -> list[OrganizationPermissionRead]:
    try:
        rows = await list_organization_user_permissions(
            db,
            organization_id=organization_id,
            actor_user_id=context.user_id,
            target_user_id=user_id,
        )
    except OrganizationPermissionError as exc:
        raise _permission_http_error(exc) from exc
    return [
        OrganizationPermissionRead(
            organization_id=organization_id,
            user_id=user_id,
            library_slug=slug,
            actions=list(actions),
        )
        for slug, actions in rows
    ]


async def _commit_permission_mutation(
    db: AsyncSession,
    result: PermissionMutationResult,
) -> None:
    try:
        await db.commit()
    except Exception:
        try:
            result.compensate()
        finally:
            await db.rollback()
        raise


@router.put("/organizations/{organization_id}/permissions")
async def grant_permissions(
    organization_id: uuid.UUID,
    body: OrganizationPermissionGrant,
    context: OrganizationAdminContext = Depends(require_organization_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[str]]:
    try:
        result = await grant_organization_permissions(
            db,
            organization_id=organization_id,
            actor_user_id=context.user_id,
            target_user_id=body.user_id,
            library_slug=body.library_slug,
            actions=body.actions,
        )
        await _commit_permission_mutation(db, result)
    except OrganizationPermissionError as exc:
        await db.rollback()
        raise _permission_http_error(exc) from exc
    return {"added": list(result.added)}


@router.delete("/organizations/{organization_id}/permissions")
async def revoke_permissions(
    organization_id: uuid.UUID,
    body: OrganizationPermissionRevoke,
    context: OrganizationAdminContext = Depends(require_organization_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[str]]:
    try:
        result = await revoke_organization_permissions(
            db,
            organization_id=organization_id,
            actor_user_id=context.user_id,
            target_user_id=body.user_id,
            library_slug=body.library_slug,
            actions=body.actions,
        )
        await _commit_permission_mutation(db, result)
    except OrganizationPermissionError as exc:
        await db.rollback()
        raise _permission_http_error(exc) from exc
    return {"removed": list(result.removed)}
