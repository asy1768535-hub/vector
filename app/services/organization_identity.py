from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.organization import (
    ORGANIZATION_PROFILES,
    ORGANIZATION_STATUSES,
    Organization,
)
from app.models.organization_membership import (
    ORGANIZATION_MEMBERSHIP_ROLES,
    ORGANIZATION_MEMBERSHIP_STATUSES,
    OrganizationMembership,
)
from app.models.user import User
from app.services import audit_log


_ORGANIZATION_SLUG = re.compile(r"^[a-z][a-z0-9_-]{1,62}[a-z0-9]$")


class OrganizationIdentityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code[:64]
        super().__init__(message[:255])


def _bounded_name(value: str) -> str:
    if not isinstance(value, str):
        raise OrganizationIdentityError(
            "organization_name_invalid", "Organization name is invalid"
        )
    normalized = value.strip()
    if not normalized or len(normalized) > 160:
        raise OrganizationIdentityError(
            "organization_name_invalid", "Organization name is invalid"
        )
    return normalized


def _normalized_slug(value: str) -> str:
    if not isinstance(value, str):
        raise OrganizationIdentityError(
            "organization_slug_invalid", "Organization slug is invalid"
        )
    normalized = value.strip().lower()
    if not _ORGANIZATION_SLUG.fullmatch(normalized):
        raise OrganizationIdentityError(
            "organization_slug_invalid", "Organization slug is invalid"
        )
    return normalized


def _require_choice(value: str, choices: tuple[str, ...], code: str) -> str:
    if value not in choices:
        raise OrganizationIdentityError(code, "Organization identity value is invalid")
    return value


def _require_uuid(value: uuid.UUID, code: str, message: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise OrganizationIdentityError(code, message)
    return value


def _require_optional_actor(value: uuid.UUID | None) -> uuid.UUID | None:
    if value is not None:
        _require_uuid(
            value,
            "organization_actor_invalid",
            "Organization actor is invalid",
        )
    return value


@dataclass(frozen=True, slots=True)
class OrganizationCreateCommand:
    slug: str
    name: str
    deployment_profile: str
    initial_admin_user_id: uuid.UUID
    actor_user_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "slug", _normalized_slug(self.slug))
        object.__setattr__(self, "name", _bounded_name(self.name))
        _require_choice(
            self.deployment_profile,
            ORGANIZATION_PROFILES,
            "organization_profile_invalid",
        )
        _require_uuid(
            self.initial_admin_user_id,
            "organization_admin_invalid",
            "Initial administrator is invalid",
        )
        _require_optional_actor(self.actor_user_id)


@dataclass(frozen=True, slots=True)
class OrganizationUpdateCommand:
    organization_id: uuid.UUID
    expected_status: str
    name: str | None = None
    deployment_profile: str | None = None
    status: str | None = None
    actor_user_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        _require_uuid(
            self.organization_id,
            "organization_id_invalid",
            "Organization identity is invalid",
        )
        _require_optional_actor(self.actor_user_id)
        _require_choice(
            self.expected_status,
            ORGANIZATION_STATUSES,
            "organization_status_invalid",
        )
        if self.name is not None:
            object.__setattr__(self, "name", _bounded_name(self.name))
        if self.deployment_profile is not None:
            _require_choice(
                self.deployment_profile,
                ORGANIZATION_PROFILES,
                "organization_profile_invalid",
            )
        if self.status is not None:
            _require_choice(
                self.status,
                ORGANIZATION_STATUSES,
                "organization_status_invalid",
            )
        if (
            self.name is None
            and self.deployment_profile is None
            and self.status is None
        ):
            raise OrganizationIdentityError(
                "organization_update_empty", "Organization update is empty"
            )


@dataclass(frozen=True, slots=True)
class MembershipCreateCommand:
    organization_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    actor_user_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        _require_uuid(
            self.organization_id,
            "organization_id_invalid",
            "Organization identity is invalid",
        )
        _require_uuid(
            self.user_id,
            "organization_user_invalid",
            "Organization user is invalid",
        )
        _require_optional_actor(self.actor_user_id)
        _require_choice(
            self.role,
            ORGANIZATION_MEMBERSHIP_ROLES,
            "organization_membership_role_invalid",
        )


@dataclass(frozen=True, slots=True)
class MembershipChangeCommand:
    membership_id: uuid.UUID
    expected_role: str
    expected_status: str
    role: str
    status: str
    actor_user_id: uuid.UUID | None = None
    organization_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        _require_uuid(
            self.membership_id,
            "organization_membership_id_invalid",
            "Organization membership identity is invalid",
        )
        _require_optional_actor(self.actor_user_id)
        if self.organization_id is not None:
            _require_uuid(
                self.organization_id,
                "organization_id_invalid",
                "Organization identity is invalid",
            )
        _require_choice(
            self.expected_role,
            ORGANIZATION_MEMBERSHIP_ROLES,
            "organization_membership_role_invalid",
        )
        _require_choice(
            self.role,
            ORGANIZATION_MEMBERSHIP_ROLES,
            "organization_membership_role_invalid",
        )
        _require_choice(
            self.expected_status,
            ORGANIZATION_MEMBERSHIP_STATUSES,
            "organization_membership_status_invalid",
        )
        _require_choice(
            self.status,
            ORGANIZATION_MEMBERSHIP_STATUSES,
            "organization_membership_status_invalid",
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _lock_organization(db, organization_id: uuid.UUID) -> Organization:
    organization = (
        await db.execute(
            select(Organization)
            .where(Organization.id == organization_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if organization is None:
        raise OrganizationIdentityError(
            "organization_not_found", "Organization was not found"
        )
    return organization


async def _lock_active_user(db, user_id: uuid.UUID) -> User:
    user = (
        await db.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if user is None or not user.is_active or user.deleted_at is not None:
        raise OrganizationIdentityError(
            "organization_user_unavailable", "Organization user is unavailable"
        )
    return user


async def get_organization(db, organization_id: uuid.UUID) -> Organization:
    organization = await db.get(Organization, organization_id)
    if organization is None:
        raise OrganizationIdentityError(
            "organization_not_found", "Organization was not found"
        )
    return organization


async def list_organizations(
    db,
    *,
    status: str | None = None,
    limit: int = 100,
) -> tuple[Organization, ...]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise OrganizationIdentityError(
            "organization_limit_invalid", "Organization list limit is invalid"
        )
    statement = select(Organization).order_by(Organization.slug).limit(limit)
    if status is not None:
        _require_choice(
            status, ORGANIZATION_STATUSES, "organization_status_invalid"
        )
        statement = statement.where(Organization.status == status)
    return tuple((await db.execute(statement)).scalars().all())


async def create_organization(
    db,
    command: OrganizationCreateCommand,
) -> Organization:
    existing = (
        await db.execute(
            select(Organization)
            .where(Organization.slug == command.slug)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if existing is not None:
        membership = (
            await db.execute(
                select(OrganizationMembership)
                .where(
                    OrganizationMembership.organization_id == existing.id,
                    OrganizationMembership.user_id
                    == command.initial_admin_user_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().first()
        if (
            existing.name == command.name
            and existing.deployment_profile == command.deployment_profile
            and existing.status == "active"
            and membership is not None
            and membership.role == "organization_admin"
            and membership.status == "active"
        ):
            return existing
        raise OrganizationIdentityError(
            "organization_conflict", "Organization identity conflicts"
        )

    admin = await _lock_active_user(db, command.initial_admin_user_id)
    organization = Organization(
        id=uuid.uuid4(),
        slug=command.slug,
        name=command.name,
        deployment_profile=command.deployment_profile,
        status="active",
        created_by_user_id=command.actor_user_id,
    )
    membership = OrganizationMembership(
        id=uuid.uuid4(),
        organization_id=organization.id,
        user_id=admin.id,
        role="organization_admin",
        status="active",
        created_by_user_id=command.actor_user_id,
    )
    db.add_all((organization, membership))
    try:
        await db.flush()
    except IntegrityError as exc:
        raise OrganizationIdentityError(
            "organization_conflict", "Organization identity conflicts"
        ) from exc
    await audit_log.record(
        db,
        command.actor_user_id,
        "organization.create",
        {
            "organization_id": str(organization.id),
            "organization_slug": organization.slug,
            "initial_admin_user_id": str(admin.id),
            "deployment_profile": organization.deployment_profile,
        },
    )
    return organization


async def update_organization(
    db,
    command: OrganizationUpdateCommand,
) -> Organization:
    organization = await _lock_organization(db, command.organization_id)
    if organization.status != command.expected_status:
        raise OrganizationIdentityError(
            "organization_state_changed", "Organization state changed"
        )
    changed: dict[str, str] = {}
    for field in ("name", "deployment_profile", "status"):
        value = getattr(command, field)
        if value is not None and getattr(organization, field) != value:
            setattr(organization, field, value)
            changed[field] = value
    if changed:
        audit_target: dict[str, str | bool] = {
            "organization_id": str(organization.id),
            "organization_slug": organization.slug,
        }
        if "name" in changed:
            audit_target["name_changed"] = True
        for field in ("deployment_profile", "status"):
            if field in changed:
                audit_target[field] = changed[field]
        await audit_log.record(
            db,
            command.actor_user_id,
            "organization.update",
            audit_target,
        )
    return organization


async def list_user_memberships(
    db,
    *,
    user_id: uuid.UUID,
    active_only: bool = True,
    limit: int = 100,
) -> tuple[OrganizationMembership, ...]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise OrganizationIdentityError(
            "organization_limit_invalid", "Membership list limit is invalid"
        )
    statement = (
        select(OrganizationMembership)
        .where(OrganizationMembership.user_id == user_id)
        .order_by(OrganizationMembership.organization_id)
        .limit(limit)
    )
    if active_only:
        statement = statement.where(OrganizationMembership.status == "active")
    return tuple((await db.execute(statement)).scalars().all())


async def add_organization_membership(
    db,
    command: MembershipCreateCommand,
) -> OrganizationMembership:
    organization = await _lock_organization(db, command.organization_id)
    if organization.status != "active":
        raise OrganizationIdentityError(
            "organization_suspended", "Organization is suspended"
        )
    user = await _lock_active_user(db, command.user_id)
    existing = (
        await db.execute(
            select(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == organization.id,
                OrganizationMembership.user_id == user.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if existing is not None:
        if existing.role == command.role and existing.status == "active":
            return existing
        raise OrganizationIdentityError(
            "organization_membership_conflict", "Membership identity conflicts"
        )
    membership = OrganizationMembership(
        id=uuid.uuid4(),
        organization_id=organization.id,
        user_id=user.id,
        role=command.role,
        status="active",
        created_by_user_id=command.actor_user_id,
    )
    db.add(membership)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise OrganizationIdentityError(
            "organization_membership_conflict", "Membership identity conflicts"
        ) from exc
    await audit_log.record(
        db,
        command.actor_user_id,
        "organization.membership_create",
        {
            "organization_id": str(organization.id),
            "membership_id": str(membership.id),
            "user_id": str(user.id),
            "role": membership.role,
            "status": membership.status,
        },
    )
    return membership


async def _lock_membership_scope(
    db,
    membership_id: uuid.UUID,
) -> tuple[Organization, OrganizationMembership]:
    initial = (
        await db.execute(
            select(
                OrganizationMembership.id,
                OrganizationMembership.organization_id,
            ).where(OrganizationMembership.id == membership_id)
        )
    ).first()
    if initial is None:
        raise OrganizationIdentityError(
            "organization_membership_not_found", "Membership was not found"
        )
    organization = await _lock_organization(db, initial.organization_id)
    membership = (
        await db.execute(
            select(OrganizationMembership)
            .where(
                OrganizationMembership.id == membership_id,
                OrganizationMembership.organization_id == organization.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if membership is None:
        raise OrganizationIdentityError(
            "organization_membership_not_found", "Membership was not found"
        )
    return organization, membership


async def _protect_final_active_admin(
    db,
    organization: Organization,
    membership: OrganizationMembership,
    *,
    role: str,
    status: str,
) -> None:
    removes_admin = (
        membership.role == "organization_admin"
        and membership.status == "active"
        and (role != "organization_admin" or status != "active")
    )
    if not removes_admin:
        return
    admin_ids = tuple(
        (
            await db.execute(
                select(OrganizationMembership.id)
                .where(
                    OrganizationMembership.organization_id == organization.id,
                    OrganizationMembership.role == "organization_admin",
                    OrganizationMembership.status == "active",
                )
                .order_by(OrganizationMembership.id)
                .with_for_update()
            )
        ).scalars().all()
    )
    if len(admin_ids) <= 1:
        raise OrganizationIdentityError(
            "organization_last_admin",
            "The final active Organization administrator cannot be removed",
        )


async def change_organization_membership(
    db,
    command: MembershipChangeCommand,
    *,
    at: datetime | None = None,
) -> OrganizationMembership:
    organization, membership = await _lock_membership_scope(
        db, command.membership_id
    )
    if (
        command.organization_id is not None
        and organization.id != command.organization_id
    ):
        raise OrganizationIdentityError(
            "organization_membership_not_found", "Membership was not found"
        )
    if organization.status != "active":
        raise OrganizationIdentityError(
            "organization_suspended", "Organization is suspended"
        )
    if (
        membership.role != command.expected_role
        or membership.status != command.expected_status
    ):
        raise OrganizationIdentityError(
            "organization_membership_state_changed", "Membership state changed"
        )
    await _protect_final_active_admin(
        db,
        organization,
        membership,
        role=command.role,
        status=command.status,
    )
    if membership.role == command.role and membership.status == command.status:
        return membership
    membership.role = command.role
    membership.status = command.status
    membership.disabled_at = (
        (at or _now()) if command.status == "disabled" else None
    )
    await audit_log.record(
        db,
        command.actor_user_id,
        "organization.membership_update",
        {
            "organization_id": str(organization.id),
            "membership_id": str(membership.id),
            "user_id": str(membership.user_id),
            "role": membership.role,
            "status": membership.status,
        },
    )
    return membership
