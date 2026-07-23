from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from app.models.library import Library
from app.models.user import User
from app.models.user_library_scope import UserLibraryScope, UserLibraryScopeItem
from app.services.library_compatibility import assess_library_compatibility
from app.services.organization_authorization import (
    list_accessible_libraries,
    resolve_organization_membership,
)


MAX_NAMED_SCOPES = 20
MAX_SCOPE_ITEMS = 20


class PersonalLibraryScopeError(RuntimeError):
    def __init__(self, code: str, message: str = "Personal Library scope operation failed") -> None:
        self.code = code[:64]
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class ScopeMetadata:
    scope: UserLibraryScope
    item_count: int


@dataclass(frozen=True, slots=True)
class RemovedScopeItem:
    library_id: uuid.UUID
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    scope: UserLibraryScope
    libraries: tuple[Library, ...]
    removed: tuple[RemovedScopeItem, ...]


def normalize_scope_name(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise PersonalLibraryScopeError("personal_scope_name_invalid")
    display = " ".join(unicodedata.normalize("NFKC", value).split())
    normalized = display.casefold()
    if not display or len(display) > 80 or len(normalized) > 80:
        raise PersonalLibraryScopeError("personal_scope_name_invalid")
    return display, normalized


def _validate_slugs(slugs: tuple[str, ...]) -> None:
    if (
        not isinstance(slugs, tuple)
        or not 1 <= len(slugs) <= MAX_SCOPE_ITEMS
        or len(set(slugs)) != len(slugs)
        or any(not isinstance(slug, str) or not slug or len(slug) > 80 for slug in slugs)
    ):
        raise PersonalLibraryScopeError("personal_scope_selection_invalid")


async def _validate_selection(db, user: User, organization_id: uuid.UUID, slugs: tuple[str, ...]):
    _validate_slugs(slugs)
    assessment = await assess_library_compatibility(
        db,
        user=user,
        library_slugs=slugs,
        channels=("text",),
    )
    if assessment.organization_id != organization_id or any(
        profile.library.organization_id != organization_id for profile in assessment.profiles
    ):
        raise PersonalLibraryScopeError("personal_scope_forbidden")
    if not assessment.compatible:
        raise PersonalLibraryScopeError("personal_scope_incompatible")
    return tuple(profile.library for profile in assessment.profiles)


async def _require_membership(db, organization_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await resolve_organization_membership(
        db,
        organization_id=organization_id,
        user_id=user_id,
    )


async def _lock_user(db, user_id: uuid.UUID) -> None:
    locked = (
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if locked is None:
        raise PersonalLibraryScopeError("personal_scope_forbidden")


def _items(scope_id: uuid.UUID, libraries: tuple[Library, ...]) -> list[UserLibraryScopeItem]:
    return [
        UserLibraryScopeItem(scope_id=scope_id, library_id=library.id, ordinal=ordinal)
        for ordinal, library in enumerate(libraries)
    ]


async def _replace_items(db, scope: UserLibraryScope, libraries: tuple[Library, ...]) -> None:
    await db.execute(delete(UserLibraryScopeItem).where(UserLibraryScopeItem.scope_id == scope.id))
    db.add_all(_items(scope.id, libraries))
    scope.updated_at = datetime.now(timezone.utc)


async def _load_owned_scope(
    db,
    *,
    scope_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str | None = None,
    lock: bool = False,
) -> UserLibraryScope:
    statement = select(UserLibraryScope).where(
        UserLibraryScope.id == scope_id,
        UserLibraryScope.organization_id == organization_id,
        UserLibraryScope.user_id == user_id,
    )
    if kind is not None:
        statement = statement.where(UserLibraryScope.scope_kind == kind)
    if lock:
        statement = statement.with_for_update()
    scope = (await db.execute(statement)).scalars().first()
    if scope is None:
        raise PersonalLibraryScopeError("personal_scope_not_found")
    return scope


async def list_named_scopes(db, *, user: User, organization_id: uuid.UUID) -> tuple[ScopeMetadata, ...]:
    await _require_membership(db, organization_id, user.id)
    rows = (
        await db.execute(
            select(UserLibraryScope, func.count(UserLibraryScopeItem.id))
            .outerjoin(UserLibraryScopeItem, UserLibraryScopeItem.scope_id == UserLibraryScope.id)
            .where(
                UserLibraryScope.organization_id == organization_id,
                UserLibraryScope.user_id == user.id,
                UserLibraryScope.scope_kind == "named",
            )
            .group_by(UserLibraryScope.id)
            .order_by(UserLibraryScope.updated_at.desc(), UserLibraryScope.id)
            .limit(MAX_NAMED_SCOPES)
        )
    ).all()
    return tuple(ScopeMetadata(scope, int(count)) for scope, count in rows)


async def create_named_scope(
    db,
    *,
    user: User,
    organization_id: uuid.UUID,
    name: str,
    library_slugs: tuple[str, ...],
) -> UserLibraryScope:
    await _require_membership(db, organization_id, user.id)
    await _lock_user(db, user.id)
    display, normalized = normalize_scope_name(name)
    count = (
        await db.execute(
            select(func.count(UserLibraryScope.id)).where(
                UserLibraryScope.organization_id == organization_id,
                UserLibraryScope.user_id == user.id,
                UserLibraryScope.scope_kind == "named",
            )
        )
    ).scalar_one()
    if count >= MAX_NAMED_SCOPES:
        raise PersonalLibraryScopeError("personal_scope_limit_reached")
    existing = (
        await db.execute(
            select(UserLibraryScope.id).where(
                UserLibraryScope.organization_id == organization_id,
                UserLibraryScope.user_id == user.id,
                UserLibraryScope.scope_kind == "named",
                UserLibraryScope.normalized_name == normalized,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise PersonalLibraryScopeError("personal_scope_name_conflict")
    libraries = await _validate_selection(db, user, organization_id, library_slugs)
    scope = UserLibraryScope(
        organization_id=organization_id,
        user_id=user.id,
        scope_kind="named",
        name=display,
        normalized_name=normalized,
    )
    db.add(scope)
    await db.flush()
    db.add_all(_items(scope.id, libraries))
    return scope


async def replace_named_scope(
    db,
    *,
    user: User,
    scope_id: uuid.UUID,
    organization_id: uuid.UUID,
    expected_updated_at: datetime,
    name: str,
    library_slugs: tuple[str, ...],
) -> UserLibraryScope:
    await _require_membership(db, organization_id, user.id)
    scope = await _load_owned_scope(
        db,
        scope_id=scope_id,
        organization_id=organization_id,
        user_id=user.id,
        kind="named",
        lock=True,
    )
    if scope.updated_at != expected_updated_at:
        raise PersonalLibraryScopeError("personal_scope_state_changed")
    display, normalized = normalize_scope_name(name)
    conflict = (
        await db.execute(
            select(UserLibraryScope.id).where(
                UserLibraryScope.organization_id == organization_id,
                UserLibraryScope.user_id == user.id,
                UserLibraryScope.scope_kind == "named",
                UserLibraryScope.normalized_name == normalized,
                UserLibraryScope.id != scope.id,
            )
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise PersonalLibraryScopeError("personal_scope_name_conflict")
    libraries = await _validate_selection(db, user, organization_id, library_slugs)
    scope.name, scope.normalized_name = display, normalized
    await _replace_items(db, scope, libraries)
    return scope


async def delete_named_scope(
    db,
    *,
    user: User,
    scope_id: uuid.UUID,
    organization_id: uuid.UUID,
    expected_updated_at: datetime,
) -> None:
    await _require_membership(db, organization_id, user.id)
    scope = await _load_owned_scope(
        db,
        scope_id=scope_id,
        organization_id=organization_id,
        user_id=user.id,
        kind="named",
        lock=True,
    )
    if scope.updated_at != expected_updated_at:
        raise PersonalLibraryScopeError("personal_scope_state_changed")
    await db.delete(scope)


async def upsert_last_used_scope(
    db,
    *,
    user: User,
    organization_id: uuid.UUID,
    library_slugs: tuple[str, ...],
) -> UserLibraryScope:
    await _require_membership(db, organization_id, user.id)
    await _lock_user(db, user.id)
    libraries = await _validate_selection(db, user, organization_id, library_slugs)
    scope = (
        await db.execute(
            select(UserLibraryScope)
            .where(
                UserLibraryScope.organization_id == organization_id,
                UserLibraryScope.user_id == user.id,
                UserLibraryScope.scope_kind == "last_used",
            )
            .with_for_update()
        )
    ).scalars().first()
    if scope is None:
        scope = UserLibraryScope(
            organization_id=organization_id,
            user_id=user.id,
            scope_kind="last_used",
        )
        db.add(scope)
        await db.flush()
        db.add_all(_items(scope.id, libraries))
        return scope
    current = (
        await db.execute(
            select(UserLibraryScopeItem.library_id)
            .where(UserLibraryScopeItem.scope_id == scope.id)
            .order_by(UserLibraryScopeItem.ordinal)
        )
    ).scalars().all()
    desired = [library.id for library in libraries]
    if list(current) != desired:
        await _replace_items(db, scope, libraries)
    return scope


async def _resolve_scope(
    db, *, user: User, scope: UserLibraryScope, membership_checked: bool = False
) -> ResolvedScope:
    if not membership_checked:
        await _require_membership(db, scope.organization_id, user.id)
    stored_ids = tuple(
        (
            await db.execute(
                select(UserLibraryScopeItem.library_id)
                .where(UserLibraryScopeItem.scope_id == scope.id)
                .order_by(UserLibraryScopeItem.ordinal)
            )
        ).scalars().all()
    )
    accessible = {
        library.id: library
        for library in await list_accessible_libraries(db, user=user, action="read")
        if library.organization_id == scope.organization_id
    }
    readable = tuple(accessible[item_id] for item_id in stored_ids if item_id in accessible)
    removed = [
        RemovedScopeItem(item_id, ("unavailable_or_forbidden",))
        for item_id in stored_ids
        if item_id not in accessible
    ]
    if not readable:
        return ResolvedScope(scope, (), tuple(removed))
    first = await assess_library_compatibility(
        db,
        user=user,
        library_slugs=tuple(library.slug for library in readable),
        channels=("text",),
    )
    reasons = {item.library_slug: item.reason_codes for item in first.incompatibilities}
    survivors = tuple(library for library in readable if library.slug not in reasons)
    removed.extend(
        RemovedScopeItem(library.id, reasons[library.slug])
        for library in readable
        if library.slug in reasons
    )
    if not first.compatible and survivors:
        second = await assess_library_compatibility(
            db,
            user=user,
            library_slugs=tuple(library.slug for library in survivors),
            channels=("text",),
        )
        if not second.compatible:
            removed.extend(
                RemovedScopeItem(library.id, ("compatibility_revalidation_failed",))
                for library in survivors
            )
            survivors = ()
    position = {library_id: ordinal for ordinal, library_id in enumerate(stored_ids)}
    removed.sort(key=lambda item: position[item.library_id])
    return ResolvedScope(scope, survivors, tuple(removed))


async def resolve_named_scope(
    db, *, user: User, organization_id: uuid.UUID, scope_id: uuid.UUID
) -> ResolvedScope:
    await _require_membership(db, organization_id, user.id)
    scope = await _load_owned_scope(
        db,
        scope_id=scope_id,
        organization_id=organization_id,
        user_id=user.id,
        kind="named",
    )
    return await _resolve_scope(db, user=user, scope=scope, membership_checked=True)


async def read_named_scope_library_ids(
    db,
    *,
    user: User,
    organization_id: uuid.UUID,
    scope_id: uuid.UUID,
) -> tuple[uuid.UUID, ...]:
    """Return the stored ordered selection without applying restore/removal policy."""
    await _require_membership(db, organization_id, user.id)
    scope = await _load_owned_scope(
        db,
        scope_id=scope_id,
        organization_id=organization_id,
        user_id=user.id,
        kind="named",
    )
    return tuple(
        (
            await db.execute(
                select(UserLibraryScopeItem.library_id)
                .where(UserLibraryScopeItem.scope_id == scope.id)
                .order_by(UserLibraryScopeItem.ordinal)
            )
        ).scalars().all()
    )


async def resolve_last_used_scope(
    db, *, user: User, organization_id: uuid.UUID
) -> ResolvedScope:
    await _require_membership(db, organization_id, user.id)
    scope = (
        await db.execute(
            select(UserLibraryScope).where(
                UserLibraryScope.organization_id == organization_id,
                UserLibraryScope.user_id == user.id,
                UserLibraryScope.scope_kind == "last_used",
            )
        )
    ).scalars().first()
    if scope is None:
        raise PersonalLibraryScopeError("personal_scope_not_found")
    return await _resolve_scope(db, user=user, scope=scope, membership_checked=True)
