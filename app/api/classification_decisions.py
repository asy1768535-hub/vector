from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps as deps_module
from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.deps import require_lib
from app.models.library import Library
from app.models.user import User
from app.schemas.classification_decisions import (
    ClassificationDecisionRead,
    ClassificationProposalRead,
    ClassificationReviewPageRead,
    ClassificationReviewRequest,
    ClassificationRunRead,
    EffectiveClassificationRead,
    ManualClassificationRemoveRequest,
    ManualClassificationSetRequest,
)
from app.services.classification_decision_contracts import (
    ClassificationDecisionError,
    ManualClassificationSelection,
    RemoveManualClassificationCommand,
    ReviewClassificationRunCommand,
    SetManualClassificationCommand,
)
from app.services.classification_decisions import (
    ClassificationReviewItem,
    EffectiveClassification,
    get_effective_classification,
    list_classification_review_runs,
    remove_manual_classification,
    review_classification_run,
    set_manual_classification,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    authorize_library_management,
)


router = APIRouter(
    prefix="/libraries/{slug}/classifications",
    tags=["classification-decisions"],
)


@dataclass(frozen=True, slots=True)
class ClassificationManagementContext:
    user: User
    library: Library


def _require_runtime() -> None:
    if not settings.classification_decision_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


async def require_classification_management(
    slug: str,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ClassificationManagementContext:
    _require_runtime()
    library = await deps_module.load_active_library(slug, db)
    if library is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    try:
        await authorize_library_management(db, user=user, library=library)
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return ClassificationManagementContext(user, library)


def _http_error(exc: ClassificationDecisionError) -> HTTPException:
    if exc.code == "classification_admin_forbidden":
        return HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if exc.code in {
        "classification_library_not_found",
        "classification_document_not_found",
        "classification_run_not_found",
    }:
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code in {
        "classification_revision_stale",
        "classification_revision_unavailable",
        "classification_scope_not_found",
        "classification_taxonomy_state_changed",
        "classification_label_set_state_changed",
        "classification_run_state_changed",
        "classification_decision_state_changed",
        "classification_decision_conflict",
    }:
        return HTTPException(status.HTTP_409_CONFLICT, exc.code)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)


def _proposal_read(
    pair,
    *,
    taxonomy_version_id: uuid.UUID,
) -> ClassificationProposalRead:
    proposal, label = pair
    if label is not None and label.taxonomy_version_id != taxonomy_version_id:
        label = None
    return ClassificationProposalRead(
        id=proposal.id,
        label_id=proposal.label_id if label is not None else None,
        label_key=label.key if label is not None else None,
        label=label.label if label is not None else None,
        proposed_key=proposal.proposed_key,
        proposed_label=proposal.proposed_label,
        role=proposal.role,
        rank=proposal.rank,
        confidence_micros=proposal.confidence_micros,
        status=proposal.status,
        reason_codes=list(proposal.reason_codes),
        created_at=proposal.created_at,
    )


def _run_read(item: ClassificationReviewItem) -> ClassificationRunRead:
    run = item.run
    return ClassificationRunRead(
        id=run.id,
        library_id=run.library_id,
        document_id=run.document_id,
        document_revision_id=run.document_revision_id,
        taxonomy_version_id=run.taxonomy_version_id,
        generation_no=run.generation_no,
        retry_generation=run.retry_generation,
        status=run.status,
        reason_codes=list(run.reason_codes),
        policy_version=run.policy_version,
        min_confidence_micros=run.min_confidence_micros,
        min_margin_micros=run.min_margin_micros,
        classifier_version=run.classifier_version,
        model_provider=run.model_provider,
        model_name=run.model_name,
        prompt_version=run.prompt_version,
        error_code=run.error_code,
        proposals=[
            _proposal_read(pair, taxonomy_version_id=run.taxonomy_version_id)
            for pair in item.proposals
        ],
        created_at=run.created_at,
        finished_at=run.finished_at,
    )


def _effective_read(value: EffectiveClassification) -> EffectiveClassificationRead:
    decision_set = value.decision_set
    return EffectiveClassificationRead(
        library_id=value.document.library_id,
        document_id=value.document.id,
        document_revision_id=value.revision.id,
        state=value.state,
        decision_set_id=decision_set.id if decision_set is not None else None,
        taxonomy_version_id=(
            decision_set.taxonomy_version_id if decision_set is not None else None
        ),
        source=decision_set.source if decision_set is not None else None,
        generation_no=decision_set.generation_no if decision_set is not None else None,
        decisions=[
            ClassificationDecisionRead(
                id=decision.id,
                label_id=label.id,
                label_key=label.key,
                label=label.label,
                role=decision.role,
                ordinal=decision.ordinal,
                confidence_micros=decision.confidence_micros,
            )
            for decision, label in value.decisions
        ],
        latest_run_id=value.latest_run.id if value.latest_run is not None else None,
        latest_run_status=(
            value.latest_run.status if value.latest_run is not None else None
        ),
        latest_error_code=(
            value.latest_run.error_code if value.latest_run is not None else None
        ),
    )


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "classification_decision_conflict",
        ) from exc
    except Exception:
        await db.rollback()
        raise


@router.get("/reviews", response_model=ClassificationReviewPageRead)
async def classification_reviews(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: ClassificationManagementContext = Depends(
        require_classification_management
    ),
    db: AsyncSession = Depends(get_db),
) -> ClassificationReviewPageRead:
    try:
        page = await list_classification_review_runs(
            db,
            library_id=context.library.id,
            actor_user_id=context.user.id,
            limit=limit,
            offset=offset,
        )
    except ClassificationDecisionError as exc:
        raise _http_error(exc) from exc
    return ClassificationReviewPageRead(
        items=[_run_read(item) for item in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/runs/{run_id}/review",
    response_model=EffectiveClassificationRead,
)
async def review_run(
    run_id: uuid.UUID,
    body: ClassificationReviewRequest,
    context: ClassificationManagementContext = Depends(
        require_classification_management
    ),
    db: AsyncSession = Depends(get_db),
) -> EffectiveClassificationRead:
    selection = (
        ManualClassificationSelection(
            primary_label_id=body.primary_label_id,
            secondary_label_ids=tuple(body.secondary_label_ids),
        )
        if body.action == "change" and body.primary_label_id is not None
        else None
    )
    try:
        result = await review_classification_run(
            db,
            ReviewClassificationRunCommand(
                library_id=context.library.id,
                actor_user_id=context.user.id,
                run_id=run_id,
                expected_run_status=body.expected_run_status,
                expected_effective_decision_set_id=(
                    body.expected_effective_decision_set_id
                ),
                action=body.action,
                selection=selection,
            ),
        )
        await _commit(db)
        effective = await get_effective_classification(
            db,
            library_id=context.library.id,
            document_id=result.run.document_id,
        )
    except ClassificationDecisionError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except Exception:
        await db.rollback()
        raise
    return _effective_read(effective)


@router.get(
    "/documents/{document_id}",
    response_model=EffectiveClassificationRead,
)
async def effective_document_classification(
    document_id: uuid.UUID,
    library: Library = Depends(require_lib("read")),
    db: AsyncSession = Depends(get_db),
) -> EffectiveClassificationRead:
    _require_runtime()
    try:
        value = await get_effective_classification(
            db,
            library_id=library.id,
            document_id=document_id,
        )
    except ClassificationDecisionError as exc:
        raise _http_error(exc) from exc
    return _effective_read(value)


@router.put(
    "/documents/{document_id}",
    response_model=EffectiveClassificationRead,
)
async def set_document_classification(
    document_id: uuid.UUID,
    body: ManualClassificationSetRequest,
    context: ClassificationManagementContext = Depends(
        require_classification_management
    ),
    db: AsyncSession = Depends(get_db),
) -> EffectiveClassificationRead:
    try:
        await set_manual_classification(
            db,
            SetManualClassificationCommand(
                library_id=context.library.id,
                actor_user_id=context.user.id,
                document_id=document_id,
                expected_effective_decision_set_id=(
                    body.expected_effective_decision_set_id
                ),
                selection=ManualClassificationSelection(
                    primary_label_id=body.primary_label_id,
                    secondary_label_ids=tuple(body.secondary_label_ids),
                ),
            ),
        )
        await _commit(db)
        effective = await get_effective_classification(
            db,
            library_id=context.library.id,
            document_id=document_id,
        )
    except ClassificationDecisionError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except Exception:
        await db.rollback()
        raise
    return _effective_read(effective)


@router.delete(
    "/documents/{document_id}",
    response_model=EffectiveClassificationRead,
)
async def remove_document_classification(
    document_id: uuid.UUID,
    body: ManualClassificationRemoveRequest,
    context: ClassificationManagementContext = Depends(
        require_classification_management
    ),
    db: AsyncSession = Depends(get_db),
) -> EffectiveClassificationRead:
    try:
        await remove_manual_classification(
            db,
            RemoveManualClassificationCommand(
                library_id=context.library.id,
                actor_user_id=context.user.id,
                document_id=document_id,
                expected_effective_decision_set_id=(
                    body.expected_effective_decision_set_id
                ),
            ),
        )
        await _commit(db)
        effective = await get_effective_classification(
            db,
            library_id=context.library.id,
            document_id=document_id,
        )
    except ClassificationDecisionError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except Exception:
        await db.rollback()
        raise
    return _effective_read(effective)
