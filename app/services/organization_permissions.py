from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.casbin import service as casbin_service
from app.models.library import Library
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.services import audit_log
from app.services.organization_authorization import VALID_ACTIONS


log = logging.getLogger(__name__)


class OrganizationPermissionError(RuntimeError):
    def __init__(self, code: str, message: str = "Organization permission operation failed") -> None:
        self.code = code[:64]
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class PermissionMutationResult:
    user_id: uuid.UUID
    library_slug: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def compensate(self) -> None:
        try:
            if self.added:
                casbin_service.revoke(str(self.user_id), self.library_slug, self.added)
            if self.removed:
                casbin_service.grant(str(self.user_id), self.library_slug, self.removed)
        except Exception as exc:
            log.error(
                "organization permission compensation failed: "
                "user_id=%s library_slug=%s added=%s removed=%s",
                self.user_id,
                self.library_slug,
                self.added,
                self.removed,
            )
            raise OrganizationPermissionError(
                "organization_permission_compensation_failed"
            ) from exc


async def _lock_scope(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    library_slug: str,
) -> Library:
    organization = (
        await db.execute(
            select(Organization)
            .where(Organization.id == organization_id, Organization.status == "active")
            .with_for_update()
        )
    ).scalars().first()
    if organization is None:
        raise OrganizationPermissionError("organization_permission_scope_not_found")
    memberships = tuple(
        (
            await db.execute(
                select(OrganizationMembership)
                .where(
                    OrganizationMembership.organization_id == organization_id,
                    OrganizationMembership.user_id.in_((actor_user_id, target_user_id)),
                    OrganizationMembership.status == "active",
                )
                .order_by(OrganizationMembership.user_id)
                .with_for_update()
            )
        ).scalars().all()
    )
    by_user = {membership.user_id: membership for membership in memberships}
    actor = by_user.get(actor_user_id)
    if actor is None or actor.role != "organization_admin":
        raise OrganizationPermissionError("organization_permission_admin_forbidden")
    if target_user_id not in by_user:
        raise OrganizationPermissionError("organization_permission_target_not_found")
    library = (
        await db.execute(
            select(Library)
            .where(
                Library.organization_id == organization_id,
                Library.slug == library_slug,
                Library.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalars().first()
    if library is None:
        raise OrganizationPermissionError("organization_permission_library_not_found")
    return library


def _validated_actions(actions: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    values = tuple(actions)
    if not values or len(set(values)) != len(values) or any(
        action not in VALID_ACTIONS for action in values
    ):
        raise OrganizationPermissionError("organization_permission_actions_invalid")
    return values


async def grant_organization_permissions(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    library_slug: str,
    actions: tuple[str, ...] | list[str],
) -> PermissionMutationResult:
    values = _validated_actions(actions)
    library = await _lock_scope(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        library_slug=library_slug,
    )
    added_rows = casbin_service.grant(str(target_user_id), library.slug, values)
    result = PermissionMutationResult(
        user_id=target_user_id,
        library_slug=library.slug,
        added=tuple(row[2] for row in added_rows),
    )
    try:
        await audit_log.record(
            db,
            actor_user_id,
            "organization.permission_grant",
            {
                "organization_id": str(organization_id),
                "user_id": str(target_user_id),
                "library_id": str(library.id),
                "actions": list(values),
                "added": len(result.added),
            },
        )
    except Exception:
        result.compensate()
        raise
    return result


async def grant_platform_library_permissions(
    db,
    *,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    library: Library,
    actions: tuple[str, ...] | list[str],
) -> PermissionMutationResult:
    """Grant library actions and establish the target's organization membership."""
    values = _validated_actions(actions)
    organization = (
        await db.execute(
            select(Organization)
            .where(
                Organization.id == library.organization_id,
                Organization.status == "active",
            )
            .with_for_update()
        )
    ).scalars().first()
    if organization is None:
        raise OrganizationPermissionError("organization_permission_scope_not_found")

    membership = (
        await db.execute(
            select(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == library.organization_id,
                OrganizationMembership.user_id == target_user_id,
            )
            .with_for_update()
        )
    ).scalars().first()
    membership_change = "unchanged"
    if membership is None:
        membership = OrganizationMembership(
            organization_id=library.organization_id,
            user_id=target_user_id,
            role="member",
            status="active",
            created_by_user_id=actor_user_id,
        )
        db.add(membership)
        await db.flush()
        membership_change = "created"
    elif membership.status != "active":
        membership.status = "active"
        membership.disabled_at = None
        membership_change = "reactivated"

    added_rows = casbin_service.grant(str(target_user_id), library.slug, values)
    result = PermissionMutationResult(
        user_id=target_user_id,
        library_slug=library.slug,
        added=tuple(row[2] for row in added_rows),
    )
    try:
        await audit_log.record(
            db,
            actor_user_id,
            "platform.permission_grant",
            {
                "organization_id": str(library.organization_id),
                "user_id": str(target_user_id),
                "library_id": str(library.id),
                "actions": list(values),
                "added": len(result.added),
                "membership_change": membership_change,
            },
        )
    except Exception:
        result.compensate()
        raise
    return result


async def revoke_organization_permissions(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    library_slug: str,
    actions: tuple[str, ...] | list[str] | None,
) -> PermissionMutationResult:
    values = None if actions is None else _validated_actions(actions)
    library = await _lock_scope(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        library_slug=library_slug,
    )
    current = casbin_service.list_user_permissions(str(target_user_id)).get(library.slug, [])
    selected = tuple(
        action
        for action in VALID_ACTIONS
        if action in current and (values is None or action in values)
    )
    casbin_service.revoke(str(target_user_id), library.slug, selected)
    result = PermissionMutationResult(
        user_id=target_user_id,
        library_slug=library.slug,
        removed=selected,
    )
    try:
        await audit_log.record(
            db,
            actor_user_id,
            "organization.permission_revoke",
            {
                "organization_id": str(organization_id),
                "user_id": str(target_user_id),
                "library_id": str(library.id),
                "actions": list(selected),
                "removed": len(selected),
            },
        )
    except Exception:
        result.compensate()
        raise
    return result


async def list_organization_user_permissions(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    organization = (
        await db.execute(
            select(Organization.id).where(
                Organization.id == organization_id,
                Organization.status == "active",
            )
        )
    ).scalar_one_or_none()
    if organization is None:
        raise OrganizationPermissionError("organization_permission_scope_not_found")
    rows = (
        await db.execute(
            select(OrganizationMembership.user_id, OrganizationMembership.role).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id.in_((actor_user_id, target_user_id)),
                OrganizationMembership.status == "active",
            )
        )
    ).all()
    memberships = {user_id: role for user_id, role in rows}
    if memberships.get(actor_user_id) != "organization_admin":
        raise OrganizationPermissionError("organization_permission_admin_forbidden")
    if target_user_id not in memberships:
        raise OrganizationPermissionError("organization_permission_target_not_found")
    explicit = casbin_service.list_user_permissions(str(target_user_id))
    libraries = (
        await db.execute(
            select(Library.slug).where(
                Library.organization_id == organization_id,
                Library.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return tuple(
        (
            slug,
            tuple(action for action in VALID_ACTIONS if action in explicit.get(slug, ())),
        )
        for slug in sorted(libraries)
        if explicit.get(slug)
    )
