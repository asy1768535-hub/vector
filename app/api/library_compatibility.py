from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user, current_cookie_user
from app.config import settings
from app.db import get_db
from app.models.user import User
from app.schemas.library_compatibility import (
    EmbeddingProfileVerificationRead,
    LibraryCompatibilityAssessmentRead,
    LibraryCompatibilityCheckRequest,
    LibraryCompatibilityProfileRead,
    LibraryIncompatibilityRead,
)
from app.services.library_compatibility import (
    assess_library_compatibility,
)
from app.services.library_compatibility_contracts import (
    COMPATIBILITY_CONTRACT_VERSION,
    LibraryCompatibilityError,
)
from app.services.library_embedding_compatibility import verify_library_embedding_profile
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    authorize_library_management,
    load_active_library,
)


router = APIRouter(tags=["library-compatibility"])


def _require_runtime() -> None:
    if not settings.cross_library_compatibility_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


@router.post(
    "/admin/libraries/{slug}/verify-embedding-profile",
    response_model=EmbeddingProfileVerificationRead,
)
async def verify_embedding_profile(
    slug: str,
    actor: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> EmbeddingProfileVerificationRead:
    _require_runtime()
    library = await load_active_library(slug, db)
    if library is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    try:
        await authorize_library_management(db, user=actor, library=library)
        snapshot = await verify_library_embedding_profile(
            db,
            library=library,
            actor_user_id=actor.id,
        )
        await db.commit()
        await db.refresh(library)
    except OrganizationAuthorizationError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    except LibraryCompatibilityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.code) from exc
    except Exception:
        await db.rollback()
        raise
    return EmbeddingProfileVerificationRead(
        library_id=library.id,
        library_slug=library.slug,
        contract_version=snapshot.contract_version,
        model=snapshot.model,
        dimension=snapshot.dimension,
        probe_fingerprint=snapshot.probe_fingerprint,
        verified_at=snapshot.verified_at,
    )


@router.post(
    "/me/library-compatibility/check",
    response_model=LibraryCompatibilityAssessmentRead,
)
async def check_library_compatibility(
    body: LibraryCompatibilityCheckRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> LibraryCompatibilityAssessmentRead:
    _require_runtime()
    try:
        assessment = await assess_library_compatibility(
            db,
            user=user,
            library_slugs=tuple(body.library_slugs),
            channels=tuple(body.channels),
        )
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    except LibraryCompatibilityError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code) from exc
    return LibraryCompatibilityAssessmentRead(
        contract_version=COMPATIBILITY_CONTRACT_VERSION,
        organization_id=assessment.organization_id,
        reference_library_slug=assessment.profiles[0].library.slug,
        channels=list(assessment.channels),
        compatible=assessment.compatible,
        libraries=[
            LibraryCompatibilityProfileRead(
                library_id=profile.library.id,
                library_slug=profile.library.slug,
                library_name=profile.library.name,
                index_state=profile.library.index_state,
                embedding_ready=profile.embedding.ready,
                retrieval_ready=profile.retrieval.ready,
                graph_ready=(
                    profile.graph.ready if profile.graph is not None else None
                ),
                embedding_profile_sha256=profile.embedding.fingerprint,
                retrieval_profile_sha256=profile.retrieval.fingerprint or "",
                graph_profile_sha256=(
                    profile.graph.fingerprint if profile.graph is not None else None
                ),
            )
            for profile in assessment.profiles
        ],
        incompatibilities=[
            LibraryIncompatibilityRead(
                library_slug=item.library_slug,
                reason_codes=list(item.reason_codes),
            )
            for item in assessment.incompatibilities
        ],
    )
