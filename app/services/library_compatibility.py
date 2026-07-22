from __future__ import annotations

from app.config import settings
from app.models.user import User
from app.services.library_compatibility_contracts import (
    CompatibilityAssessment,
    CompatibilityChannel,
    LibraryCompatibilityError,
    LibraryCompatibilityProfile,
    LibraryIncompatibility,
    REASON_ORDER,
    VALID_CHANNELS,
)
from app.services.library_embedding_compatibility import (
    build_embedding_profile,
)
from app.services.library_graph_compatibility import build_graph_profiles
from app.services.library_retrieval_compatibility import build_retrieval_profile
from app.services.organization_authorization import resolve_library_selection


def _ordered_reasons(values: set[str]) -> tuple[str, ...]:
    return tuple(reason for reason in REASON_ORDER if reason in values)


async def assess_library_compatibility(
    db,
    *,
    user: User,
    library_slugs: tuple[str, ...],
    channels: tuple[CompatibilityChannel, ...],
) -> CompatibilityAssessment:
    if (
        not 1 <= len(library_slugs) <= 20
        or len(set(library_slugs)) != len(library_slugs)
        or any(
            not isinstance(slug, str) or not slug.strip() or len(slug) > 80
            for slug in library_slugs
        )
        or not channels
        or len(set(channels)) != len(channels)
        or any(channel not in VALID_CHANNELS for channel in channels)
    ):
        raise LibraryCompatibilityError("compatibility_request_invalid")
    accesses = await resolve_library_selection(
        db,
        user=user,
        library_slugs=library_slugs,
        action="read",
    )
    libraries = tuple(access.library for access in accesses)
    graph_profiles = await build_graph_profiles(db, libraries) if "graph" in channels else {}
    profiles = tuple(
        LibraryCompatibilityProfile(
            library=library,
            embedding=build_embedding_profile(library),
            retrieval=build_retrieval_profile(library),
            graph=graph_profiles.get(library.id),
        )
        for library in libraries
    )
    reference = profiles[0]
    incompatibilities: list[LibraryIncompatibility] = []
    for profile in profiles:
        reasons: set[str] = set()
        if profile.library.organization_id != reference.library.organization_id:
            reasons.add("organization_mismatch")
        if "text" in channels:
            if profile.library.index_state != "ready":
                reasons.add("library_index_unready")
            if not profile.embedding.ready and profile.embedding.reason_code:
                reasons.add(profile.embedding.reason_code)
            if profile.embedding.fingerprint != reference.embedding.fingerprint:
                reasons.add("embedding_profile_mismatch")
            if profile.retrieval.fingerprint != reference.retrieval.fingerprint:
                reasons.add("retrieval_profile_mismatch")
        if "graph" in channels:
            if not settings.graph_retrieval_enabled:
                reasons.add("graph_channel_disabled")
            graph = profile.graph
            if graph is not None and not graph.ready and graph.reason_code:
                reasons.add(graph.reason_code)
            reference_graph = reference.graph
            if (
                graph is not None
                and reference_graph is not None
                and graph.fingerprint != reference_graph.fingerprint
            ):
                reasons.add("graph_profile_mismatch")
        ordered = _ordered_reasons(reasons)
        if ordered:
            incompatibilities.append(
                LibraryIncompatibility(profile.library.slug, ordered)
            )
    return CompatibilityAssessment(
        organization_id=reference.library.organization_id,
        channels=channels,
        profiles=profiles,
        compatible=not incompatibilities,
        incompatibilities=tuple(incompatibilities),
    )
