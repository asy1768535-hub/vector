from __future__ import annotations

from app.models.user import User
from app.services.library_compatibility import assess_library_compatibility
from app.services.library_compatibility_contracts import LibraryCompatibilityError
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    credential_organization_scope,
    list_accessible_libraries,
    resolve_library_selection,
    resolve_organization_membership,
)
from app.services.personal_library_scopes import (
    PersonalLibraryScopeError,
    read_named_scope_library_ids,
)
from app.services.graph_catalog_contracts import (
    GraphCatalogError,
    GraphCatalogResolvedScope,
    GraphCatalogScopeError,
    GraphCatalogScopeIncompatibility,
    GraphCatalogSelection,
)


async def resolve_graph_catalog_scope(
    db,
    *,
    user: User,
    selection: GraphCatalogSelection,
) -> GraphCatalogResolvedScope:
    if not isinstance(user, User) or not isinstance(selection, GraphCatalogSelection):
        raise GraphCatalogScopeError("graph_catalog_scope_invalid")
    credential_scope = credential_organization_scope(user)
    if (
        credential_scope is not None
        and credential_scope.organization_id != selection.organization_id
    ):
        raise GraphCatalogScopeError("graph_catalog_scope_forbidden")
    try:
        await resolve_organization_membership(
            db,
            organization_id=selection.organization_id,
            user_id=user.id,
        )
        if selection.library_slugs is not None:
            accesses = await resolve_library_selection(
                db,
                user=user,
                library_slugs=selection.library_slugs,
                action="read",
            )
            libraries = tuple(access.library for access in accesses)
        else:
            stored_ids = await read_named_scope_library_ids(
                db,
                user=user,
                organization_id=selection.organization_id,
                scope_id=selection.scope_id,
            )
            accessible = {
                library.id: library
                for library in await list_accessible_libraries(db, user=user, action="read")
                if library.organization_id == selection.organization_id
            }
            if not stored_ids or any(item not in accessible for item in stored_ids):
                raise GraphCatalogScopeError("graph_catalog_scope_forbidden")
            libraries = tuple(accessible[item] for item in stored_ids)
    except GraphCatalogScopeError:
        raise
    except (OrganizationAuthorizationError, PersonalLibraryScopeError) as exc:
        raise GraphCatalogScopeError("graph_catalog_scope_forbidden") from exc

    if (
        not libraries
        or any(library.organization_id != selection.organization_id for library in libraries)
    ):
        raise GraphCatalogScopeError("graph_catalog_scope_forbidden")
    if len(libraries) > 1:
        try:
            assessment = await assess_library_compatibility(
                db,
                user=user,
                library_slugs=tuple(library.slug for library in libraries),
                channels=("graph",),
            )
        except OrganizationAuthorizationError as exc:
            raise GraphCatalogScopeError("graph_catalog_scope_forbidden") from exc
        except LibraryCompatibilityError as exc:
            raise GraphCatalogError("graph_catalog_unavailable") from exc
        profile_library_ids = tuple(profile.library.id for profile in assessment.profiles)
        if (
            assessment.organization_id != selection.organization_id
            or profile_library_ids != tuple(library.id for library in libraries)
            or any(
                profile.library.organization_id != selection.organization_id
                for profile in assessment.profiles
            )
        ):
            raise GraphCatalogScopeError("graph_catalog_scope_forbidden")
        if not assessment.compatible:
            raise GraphCatalogScopeError(
                "graph_catalog_scope_incompatible",
                incompatibilities=tuple(
                    GraphCatalogScopeIncompatibility(
                        library_slug=item.library_slug,
                        reason_codes=tuple(item.reason_codes),
                    )
                    for item in assessment.incompatibilities
                ),
            )
    return GraphCatalogResolvedScope(
        organization_id=selection.organization_id,
        libraries=libraries,
    )
