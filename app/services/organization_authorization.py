from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import and_, select

from app.casbin import service as casbin_service
from app.casbin.enforcer import has_permission
from app.config import settings
from app.models.library import Library
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.models.user import User


Action = Literal["read", "insert", "delete", "admin"]
VALID_ACTIONS: tuple[Action, ...] = ("read", "insert", "delete", "admin")
_CREDENTIAL_SCOPE_ATTRIBUTE = "_organization_credential_scope"


class OrganizationAuthorizationError(RuntimeError):
    def __init__(self, code: str, message: str = "Organization access is forbidden") -> None:
        self.code = code[:64]
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class CredentialOrganizationScope:
    organization_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class OrganizationAccess:
    organization_id: uuid.UUID
    membership_id: uuid.UUID
    role: str
    library: Library
    action: Action


@dataclass(frozen=True, slots=True)
class OrganizationAdminContext:
    organization_id: uuid.UUID
    membership_id: uuid.UUID
    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PermissionProjection:
    organization_id: uuid.UUID
    library_slug: str
    library_name: str
    actions: tuple[str, ...]


def bind_credential_organization(user: User, organization_id: uuid.UUID) -> None:
    if not isinstance(organization_id, uuid.UUID):
        raise OrganizationAuthorizationError("credential_organization_invalid")
    setattr(
        user,
        _CREDENTIAL_SCOPE_ATTRIBUTE,
        CredentialOrganizationScope(organization_id=organization_id),
    )


def credential_organization_scope(user: User) -> CredentialOrganizationScope | None:
    value = getattr(user, _CREDENTIAL_SCOPE_ATTRIBUTE, None)
    return value if isinstance(value, CredentialOrganizationScope) else None


def _action_allowed(
    user: User,
    membership: OrganizationMembership,
    library: Library,
    action: Action,
) -> bool:
    if membership.role == "organization_admin" and action == "read":
        return True
    return has_permission(str(user.id), library.slug, action)


async def load_active_library(slug: str, db) -> Library | None:
    result = await db.execute(
        select(Library).where(Library.slug == slug, Library.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def resolve_loaded_library_access(
    db,
    *,
    user: User,
    library: Library,
    action: Action,
) -> OrganizationAccess:
    if action not in VALID_ACTIONS:
        raise OrganizationAuthorizationError("organization_action_invalid")
    scope = credential_organization_scope(user)
    if scope is not None and scope.organization_id != library.organization_id:
        raise OrganizationAuthorizationError("organization_forbidden")
    row = (
        await db.execute(
            select(OrganizationMembership, Organization)
            .join(
                Organization,
                Organization.id == OrganizationMembership.organization_id,
            )
            .where(
                OrganizationMembership.organization_id == library.organization_id,
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.status == "active",
                Organization.status == "active",
            )
        )
    ).first()
    if row is None:
        raise OrganizationAuthorizationError("organization_forbidden")
    membership, _organization = row
    if not _action_allowed(user, membership, library, action):
        raise OrganizationAuthorizationError("organization_forbidden")
    return OrganizationAccess(
        organization_id=library.organization_id,
        membership_id=membership.id,
        role=membership.role,
        library=library,
        action=action,
    )


async def resolve_loaded_library_management(
    db,
    *,
    user: User,
    library: Library,
) -> OrganizationAccess:
    scope = credential_organization_scope(user)
    if scope is not None and scope.organization_id != library.organization_id:
        raise OrganizationAuthorizationError("organization_forbidden")
    row = (
        await db.execute(
            select(OrganizationMembership, Organization)
            .join(
                Organization,
                Organization.id == OrganizationMembership.organization_id,
            )
            .where(
                OrganizationMembership.organization_id == library.organization_id,
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.status == "active",
                Organization.status == "active",
            )
        )
    ).first()
    if row is None:
        raise OrganizationAuthorizationError("organization_forbidden")
    membership, _organization = row
    if membership.role != "organization_admin" and not has_permission(
        str(user.id), library.slug, "admin"
    ):
        raise OrganizationAuthorizationError("organization_forbidden")
    return OrganizationAccess(
        organization_id=library.organization_id,
        membership_id=membership.id,
        role=membership.role,
        library=library,
        action="admin",
    )


async def authorize_library_management(
    db,
    *,
    user: User,
    library: Library,
) -> OrganizationAccess:
    if settings.organization_authorization_enabled:
        return await resolve_loaded_library_management(
            db,
            user=user,
            library=library,
        )
    if user.is_superuser or has_permission(str(user.id), library.slug, "admin"):
        return OrganizationAccess(
            organization_id=library.organization_id,
            membership_id=uuid.UUID(int=0),
            role="legacy",
            library=library,
            action="admin",
        )
    raise OrganizationAuthorizationError("organization_forbidden")


async def resolve_library_access(
    db,
    *,
    user: User,
    library_slug: str,
    action: Action,
) -> OrganizationAccess:
    row = (
        await db.execute(
            select(Library, OrganizationMembership, Organization)
            .join(Organization, Organization.id == Library.organization_id)
            .join(
                OrganizationMembership,
                and_(
                    OrganizationMembership.organization_id == Library.organization_id,
                    OrganizationMembership.user_id == user.id,
                ),
            )
            .where(
                Library.slug == library_slug,
                Library.deleted_at.is_(None),
                Organization.status == "active",
                OrganizationMembership.status == "active",
            )
        )
    ).first()
    if row is None:
        raise OrganizationAuthorizationError("organization_forbidden")
    library, membership, _organization = row
    scope = credential_organization_scope(user)
    if scope is not None and scope.organization_id != library.organization_id:
        raise OrganizationAuthorizationError("organization_forbidden")
    if action not in VALID_ACTIONS or not _action_allowed(user, membership, library, action):
        raise OrganizationAuthorizationError("organization_forbidden")
    return OrganizationAccess(
        organization_id=library.organization_id,
        membership_id=membership.id,
        role=membership.role,
        library=library,
        action=action,
    )


async def authorize_library(
    db,
    *,
    user: User,
    library_slug: str,
    action: Action,
) -> Library:
    if settings.organization_authorization_enabled:
        access = await resolve_library_access(
            db,
            user=user,
            library_slug=library_slug,
            action=action,
        )
        return access.library
    library = await load_active_library(library_slug, db)
    if library is None:
        raise OrganizationAuthorizationError("organization_forbidden")
    if user.is_superuser or has_permission(str(user.id), library.slug, action):
        return library
    raise OrganizationAuthorizationError("organization_forbidden")


async def resolve_organization_admin(
    db,
    *,
    organization_id: uuid.UUID,
    user: User,
) -> OrganizationAdminContext:
    row = (
        await db.execute(
            select(OrganizationMembership.id)
            .join(
                Organization,
                Organization.id == OrganizationMembership.organization_id,
            )
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.role == "organization_admin",
                OrganizationMembership.status == "active",
                Organization.status == "active",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise OrganizationAuthorizationError("organization_admin_forbidden")
    return OrganizationAdminContext(
        organization_id=organization_id,
        membership_id=row,
        user_id=user.id,
    )


async def resolve_organization_membership(
    db,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> OrganizationMembership:
    membership = (
        await db.execute(
            select(OrganizationMembership)
            .join(
                Organization,
                Organization.id == OrganizationMembership.organization_id,
            )
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.status == "active",
                Organization.status == "active",
            )
        )
    ).scalars().first()
    if membership is None:
        raise OrganizationAuthorizationError("organization_forbidden")
    return membership


async def list_accessible_libraries(
    db,
    *,
    user: User,
    action: Action = "read",
) -> tuple[Library, ...]:
    base = select(Library).where(Library.deleted_at.is_(None)).order_by(Library.name.asc())
    if not settings.organization_authorization_enabled:
        if user.is_superuser:
            return tuple((await db.execute(base)).scalars().all())
        permissions = casbin_service.list_user_permissions(str(user.id))
        slugs = [slug for slug, actions in permissions.items() if action in actions]
        if not slugs:
            return ()
        return tuple((await db.execute(base.where(Library.slug.in_(slugs)))).scalars().all())

    rows = (
        await db.execute(
            select(Library, OrganizationMembership)
            .join(Organization, Organization.id == Library.organization_id)
            .join(
                OrganizationMembership,
                and_(
                    OrganizationMembership.organization_id == Library.organization_id,
                    OrganizationMembership.user_id == user.id,
                ),
            )
            .where(
                Library.deleted_at.is_(None),
                Organization.status == "active",
                OrganizationMembership.status == "active",
            )
            .order_by(Library.name.asc())
        )
    ).all()
    scope = credential_organization_scope(user)
    return tuple(
        library
        for library, membership in rows
        if (scope is None or scope.organization_id == library.organization_id)
        and _action_allowed(user, membership, library, action)
    )


async def list_effective_permissions(
    db,
    *,
    user: User,
) -> tuple[PermissionProjection, ...]:
    if not settings.organization_authorization_enabled:
        if user.is_superuser:
            return ()
        permissions = casbin_service.list_user_permissions(str(user.id))
        if not permissions:
            return ()
        rows = await db.execute(
            select(Library.slug, Library.name, Library.organization_id).where(
                Library.slug.in_(list(permissions)),
                Library.deleted_at.is_(None),
            )
        )
        libraries = {slug: (name, organization_id) for slug, name, organization_id in rows.all()}
        return tuple(
            PermissionProjection(
                organization_id=libraries[slug][1],
                library_slug=slug,
                library_name=libraries[slug][0],
                actions=tuple(actions),
            )
            for slug, actions in permissions.items()
            if slug in libraries
        )

    explicit = casbin_service.list_user_permissions(str(user.id))
    rows = (
        await db.execute(
            select(Library, OrganizationMembership)
            .join(Organization, Organization.id == Library.organization_id)
            .join(
                OrganizationMembership,
                and_(
                    OrganizationMembership.organization_id == Library.organization_id,
                    OrganizationMembership.user_id == user.id,
                ),
            )
            .where(
                Library.deleted_at.is_(None),
                Organization.status == "active",
                OrganizationMembership.status == "active",
            )
            .order_by(Library.name.asc(), Library.id.asc())
        )
    ).all()
    scope = credential_organization_scope(user)
    projections: list[PermissionProjection] = []
    for library, membership in rows:
        if scope is not None and scope.organization_id != library.organization_id:
            continue
        actions = set(explicit.get(library.slug, ()))
        if membership.role == "organization_admin":
            actions.add("read")
        ordered = tuple(action for action in VALID_ACTIONS if action in actions)
        if ordered:
            projections.append(
                PermissionProjection(
                    organization_id=library.organization_id,
                    library_slug=library.slug,
                    library_name=library.name,
                    actions=ordered,
                )
            )
    return tuple(projections)
