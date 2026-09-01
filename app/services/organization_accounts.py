from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.models.user import User
from app.services import audit_log
from app.services.organization_identity import (
    MembershipChangeCommand,
    OrganizationIdentityError,
    change_organization_membership,
)


class OrganizationAccountError(RuntimeError):
    def __init__(self, code: str, message: str = "Organization account operation failed") -> None:
        self.code = code[:64]
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class OrganizationAccountCreateCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    email: str
    username: str | None
    display_name: str | None
    role: str

    def __post_init__(self) -> None:
        if not isinstance(self.organization_id, uuid.UUID) or not isinstance(
            self.actor_user_id, uuid.UUID
        ):
            raise OrganizationAccountError("organization_account_identity_invalid")
        email = self.email.strip().lower() if isinstance(self.email, str) else ""
        if not email or len(email) > 320:
            raise OrganizationAccountError("organization_account_email_invalid")
        object.__setattr__(self, "email", email)
        if self.role not in {"organization_admin", "member"}:
            raise OrganizationAccountError("organization_account_role_invalid")


@dataclass(frozen=True, slots=True)
class OrganizationAccountResult:
    user: User
    membership: OrganizationMembership


@dataclass(frozen=True, slots=True)
class UserOrganizationMembership:
    organization: Organization
    membership: OrganizationMembership


async def _lock_active_organization(db, organization_id: uuid.UUID) -> Organization:
    organization = (
        await db.execute(
            select(Organization)
            .where(Organization.id == organization_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if organization is None or organization.status != "active":
        raise OrganizationAccountError("organization_account_scope_not_found")
    return organization


async def _lock_admin_membership(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> OrganizationMembership:
    membership = (
        await db.execute(
            select(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == actor_user_id,
                OrganizationMembership.role == "organization_admin",
                OrganizationMembership.status == "active",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if membership is None:
        raise OrganizationAccountError("organization_account_admin_forbidden")
    return membership


async def list_user_organizations(
    db,
    *,
    user_id: uuid.UUID,
    limit: int = 100,
) -> tuple[UserOrganizationMembership, ...]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise OrganizationAccountError("organization_account_limit_invalid")
    rows = (
        await db.execute(
            select(Organization, OrganizationMembership)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id == Organization.id,
            )
            .where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.status == "active",
                Organization.status == "active",
            )
            .order_by(Organization.slug)
            .limit(limit)
        )
    ).all()
    return tuple(
        UserOrganizationMembership(organization=organization, membership=membership)
        for organization, membership in rows
    )


async def list_organization_members(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
) -> tuple[OrganizationAccountResult, ...]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 500
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise OrganizationAccountError("organization_account_limit_invalid")
    await _lock_active_organization(db, organization_id)
    await _lock_admin_membership(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
    )
    rows = (
        await db.execute(
            select(OrganizationMembership, User)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(OrganizationMembership.organization_id == organization_id)
            .order_by(User.created_at.desc(), User.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return tuple(
        OrganizationAccountResult(user=user, membership=membership)
        for membership, user in rows
    )


async def create_organization_account(
    db,
    command: OrganizationAccountCreateCommand,
    *,
    password_hash: str,
) -> OrganizationAccountResult:
    if not isinstance(password_hash, str) or not password_hash:
        raise OrganizationAccountError("organization_account_password_invalid")
    await _lock_active_organization(db, command.organization_id)
    await _lock_admin_membership(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
    )
    existing = (
        await db.execute(select(User.id).where(func.lower(User.email) == command.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise OrganizationAccountError("organization_account_conflict")
    user = User(
        id=uuid.uuid4(),
        email=command.email,
        hashed_password=password_hash,
        is_active=True,
        is_superuser=False,
        is_verified=False,
        username=command.username,
        display_name=command.display_name,
    )
    membership = OrganizationMembership(
        id=uuid.uuid4(),
        organization_id=command.organization_id,
        user_id=user.id,
        role=command.role,
        status="active",
        created_by_user_id=command.actor_user_id,
    )
    db.add_all((user, membership))
    try:
        await db.flush()
    except IntegrityError as exc:
        raise OrganizationAccountError("organization_account_conflict") from exc
    await audit_log.record(
        db,
        command.actor_user_id,
        "organization.account_create",
        {
            "organization_id": str(command.organization_id),
            "user_id": str(user.id),
            "membership_id": str(membership.id),
            "role": membership.role,
        },
    )
    return OrganizationAccountResult(user=user, membership=membership)


async def change_organization_account_membership(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    membership_id: uuid.UUID,
    expected_role: str,
    expected_status: str,
    role: str,
    status: str,
) -> OrganizationMembership:
    await _lock_active_organization(db, organization_id)
    await _lock_admin_membership(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
    )
    target = (
        await db.execute(
            select(OrganizationMembership.user_id).where(
                OrganizationMembership.id == membership_id,
                OrganizationMembership.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise OrganizationAccountError("organization_account_membership_not_found")
    if target == actor_user_id and (
        role != "organization_admin" or status != "active"
    ):
        raise OrganizationAccountError("organization_account_self_admin_change")
    try:
        return await change_organization_membership(
            db,
            MembershipChangeCommand(
                membership_id=membership_id,
                organization_id=organization_id,
                expected_role=expected_role,
                expected_status=expected_status,
                role=role,
                status=status,
                actor_user_id=actor_user_id,
            ),
        )
    except OrganizationIdentityError as exc:
        raise OrganizationAccountError(exc.code, str(exc)) from exc


async def reset_organization_account_password(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    password_hash: str,
) -> User:
    if not isinstance(password_hash, str) or not password_hash:
        raise OrganizationAccountError("organization_account_password_invalid")
    await _lock_active_organization(db, organization_id)
    await _lock_admin_membership(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
    )
    target_membership = (
        await db.execute(
            select(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == target_user_id,
                OrganizationMembership.status == "active",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if target_membership is None:
        raise OrganizationAccountError("organization_account_not_found")
    active_membership_ids = tuple(
        (
            await db.execute(
                select(OrganizationMembership.id)
                .where(
                    OrganizationMembership.user_id == target_user_id,
                    OrganizationMembership.status == "active",
                )
                .order_by(OrganizationMembership.organization_id)
                .with_for_update()
            )
        ).scalars().all()
    )
    if len(active_membership_ids) != 1:
        raise OrganizationAccountError("organization_account_shared")
    user = (
        await db.execute(
            select(User)
            .where(
                User.id == target_user_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if user is None:
        raise OrganizationAccountError("organization_account_not_found")
    user.hashed_password = password_hash
    await audit_log.record(
        db,
        actor_user_id,
        "organization.account_password_reset",
        {
            "organization_id": str(organization_id),
            "user_id": str(user.id),
            "membership_id": str(target_membership.id),
        },
    )
    return user
