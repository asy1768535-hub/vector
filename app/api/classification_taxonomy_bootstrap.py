from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.classification_taxonomies import (
    ClassificationAdminRequestContext,
    require_classification_admin,
)
from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.models.classification_taxonomy_bootstrap import (
    TaxonomyBootstrapRun,
    TaxonomyBootstrapSource,
)
from app.models.user import User
from app.schemas.taxonomy_bootstrap import (
    TaxonomyBootstrapImport,
    TaxonomyBootstrapLabelInput,
    TaxonomyBootstrapLlmRequest,
    TaxonomyBootstrapRunRead,
    TaxonomyBootstrapSourceRead,
    TaxonomyBootstrapTemplateRead,
    TaxonomyBootstrapWarningRead,
    TaxonomyTemplateApply,
)
from app.services.classification_provider import (
    ClassificationProviderError,
    OpenAICompatibleClassificationProvider,
)
from app.services.classification_taxonomy_bootstrap import (
    TaxonomyBootstrapRunDetail,
    apply_builtin_taxonomy_template,
    get_taxonomy_bootstrap_run_detail,
    import_taxonomy_bootstrap,
    list_taxonomy_bootstrap_runs,
    prepare_llm_taxonomy_bootstrap,
    publish_llm_taxonomy_bootstrap,
    record_llm_taxonomy_bootstrap_failure,
)
from app.services.classification_taxonomy_bootstrap_contracts import (
    ApplyBuiltinTemplateCommand,
    DraftLabelInput,
    ImportTaxonomyBootstrapCommand,
    PrepareLlmTaxonomyBootstrapCommand,
    TaxonomyBootstrapError,
    parse_llm_taxonomy_bootstrap_output,
)
from app.services.classification_taxonomy_templates import (
    list_taxonomy_bootstrap_templates,
)


router = APIRouter(tags=["classification-taxonomy-bootstrap"])


def require_taxonomy_bootstrap_enabled() -> None:
    if not settings.classification_taxonomy_bootstrap_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


async def require_taxonomy_bootstrap_admin(
    organization_id: uuid.UUID,
    _: None = Depends(require_taxonomy_bootstrap_enabled),
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> ClassificationAdminRequestContext:
    return await require_classification_admin(
        organization_id=organization_id,
        user=user,
        db=db,
    )


def _http_error(exc: TaxonomyBootstrapError) -> HTTPException:
    if exc.code in {
        "bootstrap_template_not_found",
        "bootstrap_run_not_found",
        "bootstrap_sample_not_found",
    }:
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code in {
        "bootstrap_conflict",
        "bootstrap_idempotency_conflict",
        "bootstrap_taxonomy_already_initialized",
        "bootstrap_attempt_in_progress",
        "bootstrap_attempt_state_changed",
        "bootstrap_attempt_expired",
        "bootstrap_sample_revision_stale",
        "bootstrap_library_external_llm_disabled",
        "bootstrap_library_model_disabled",
        "bootstrap_security_level_denied",
        "bootstrap_model_state_changed",
    }:
        return HTTPException(status.HTTP_409_CONFLICT, exc.code)
    if exc.code == "bootstrap_llm_disabled":
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code.startswith("provider_"):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, exc.code)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "bootstrap_conflict",
        ) from exc
    except Exception:
        await db.rollback()
        raise


def _draft_input(value: TaxonomyBootstrapLabelInput) -> DraftLabelInput:
    return DraftLabelInput(
        key=value.key,
        label=value.label,
        description=value.description,
        parent_key=value.parent_key,
        sort_order=value.sort_order,
        status=value.status,
    )


def _warning_items(value: object) -> list[TaxonomyBootstrapWarningRead]:
    if not isinstance(value, list):
        return []
    warnings: list[TaxonomyBootstrapWarningRead] = []
    for item in value[:50]:
        if not isinstance(item, dict) or set(item) != {
            "code",
            "left_key",
            "right_key",
        }:
            continue
        try:
            warnings.append(TaxonomyBootstrapWarningRead.model_validate(item))
        except (TypeError, ValueError):
            continue
    return warnings


def _source_read(value: TaxonomyBootstrapSource) -> TaxonomyBootstrapSourceRead:
    return TaxonomyBootstrapSourceRead(
        ordinal=value.ordinal,
        library_id=value.library_id,
        document_id=value.document_id,
        document_revision_id=value.document_revision_id,
        revision_content_hash=value.revision_content_hash,
        security_level=value.security_level,
    )


def _run_read(
    run: TaxonomyBootstrapRun,
    sources: tuple[TaxonomyBootstrapSource, ...] = (),
) -> TaxonomyBootstrapRunRead:
    return TaxonomyBootstrapRunRead(
        id=run.id,
        organization_id=run.organization_id,
        request_id=run.request_id,
        source_type=run.source_type,
        source_key=run.source_key,
        source_version=run.source_version,
        source_hash=run.source_hash,
        status=run.status,
        warning_items=_warning_items(run.warning_items),
        output_taxonomy_id=run.output_taxonomy_id,
        model_provider=run.model_provider,
        model_name=run.model_name,
        prompt_version=run.prompt_version,
        error_code=run.error_code,
        created_by_user_id=run.created_by_user_id,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        sources=[_source_read(source) for source in sources],
    )


async def _detail_after_commit(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    run_id: uuid.UUID,
) -> TaxonomyBootstrapRunRead:
    detail = await get_taxonomy_bootstrap_run_detail(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        run_id=run_id,
    )
    await _commit(db)
    return _run_read(detail.run, detail.sources)


@router.get(
    "/organizations/{organization_id}/classification-taxonomy-bootstrap/templates",
    response_model=list[TaxonomyBootstrapTemplateRead],
)
async def taxonomy_bootstrap_templates(
    organization_id: uuid.UUID,
    _: ClassificationAdminRequestContext = Depends(require_taxonomy_bootstrap_admin),
) -> list[TaxonomyBootstrapTemplateRead]:
    return [
        TaxonomyBootstrapTemplateRead(
            key=template.key,
            version=template.version,
            description=template.description,
            template_hash=template.template_hash,
            label_count=len(template.draft.labels),
        )
        for template in list_taxonomy_bootstrap_templates()
    ]


@router.post(
    "/organizations/{organization_id}/classification-taxonomy-bootstrap/templates/{template_key}",
    response_model=TaxonomyBootstrapRunRead,
    status_code=status.HTTP_201_CREATED,
)
async def apply_taxonomy_bootstrap_template(
    organization_id: uuid.UUID,
    template_key: str,
    body: TaxonomyTemplateApply,
    context: ClassificationAdminRequestContext = Depends(require_taxonomy_bootstrap_admin),
    db: AsyncSession = Depends(get_db),
) -> TaxonomyBootstrapRunRead:
    try:
        result = await apply_builtin_taxonomy_template(
            db,
            ApplyBuiltinTemplateCommand(
                organization_id=organization_id,
                actor_user_id=context.user.id,
                request_id=body.request_id,
                template_key=template_key,
                version_key=body.version_key,
                description=body.description,
            ),
        )
        await _commit(db)
        return _run_read(result.run)
    except TaxonomyBootstrapError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/organizations/{organization_id}/classification-taxonomy-bootstrap/imports",
    response_model=TaxonomyBootstrapRunRead,
    status_code=status.HTTP_201_CREATED,
)
async def import_taxonomy_bootstrap_draft(
    organization_id: uuid.UUID,
    body: TaxonomyBootstrapImport,
    context: ClassificationAdminRequestContext = Depends(require_taxonomy_bootstrap_admin),
    db: AsyncSession = Depends(get_db),
) -> TaxonomyBootstrapRunRead:
    try:
        result = await import_taxonomy_bootstrap(
            db,
            ImportTaxonomyBootstrapCommand(
                organization_id=organization_id,
                actor_user_id=context.user.id,
                request_id=body.request_id,
                source_name=body.source_name,
                source_version=body.source_version,
                version_key=body.version_key,
                description=body.description,
                labels=tuple(_draft_input(label) for label in body.labels),
            ),
        )
        await _commit(db)
        return _run_read(result.run)
    except TaxonomyBootstrapError as exc:
        await db.rollback()
        raise _http_error(exc) from exc


async def _record_llm_failure(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    attempt_token: uuid.UUID,
    error_code: str,
) -> None:
    await db.rollback()
    await record_llm_taxonomy_bootstrap_failure(
        db,
        run_id=run_id,
        attempt_token=attempt_token,
        error_code=error_code,
    )
    await _commit(db)


@router.post(
    "/organizations/{organization_id}/classification-taxonomy-bootstrap/llm-proposals",
    response_model=TaxonomyBootstrapRunRead,
    status_code=status.HTTP_201_CREATED,
)
async def propose_taxonomy_bootstrap_draft(
    organization_id: uuid.UUID,
    body: TaxonomyBootstrapLlmRequest,
    context: ClassificationAdminRequestContext = Depends(require_taxonomy_bootstrap_admin),
    db: AsyncSession = Depends(get_db),
) -> TaxonomyBootstrapRunRead:
    try:
        prepared = await prepare_llm_taxonomy_bootstrap(
            db,
            PrepareLlmTaxonomyBootstrapCommand(
                organization_id=organization_id,
                actor_user_id=context.user.id,
                request_id=body.request_id,
                sample_revision_ids=tuple(body.sample_revision_ids),
                version_key=body.version_key,
                description=body.description,
            ),
        )
        await _commit(db)
    except TaxonomyBootstrapError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    if prepared.reused:
        if prepared.run.status == "processing":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "bootstrap_attempt_in_progress",
            )
        return await _detail_after_commit(
            db,
            organization_id=organization_id,
            actor_user_id=context.user.id,
            run_id=prepared.run.id,
        )
    if prepared.attempt_token is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "bootstrap_attempt_state_changed",
        )

    provider = OpenAICompatibleClassificationProvider(
        base_url=settings.classification_base_url,
        model=settings.classification_model,
        api_key=settings.classification_api_key.get_secret_value(),
        timeout_seconds=settings.classification_provider_timeout_seconds,
    )
    try:
        response = await provider.generate(prepared.messages)
        proposal = parse_llm_taxonomy_bootstrap_output(response.content)
    except ClassificationProviderError as exc:
        error_code = f"provider_{exc.category}"[:64]
        await _record_llm_failure(
            db,
            run_id=prepared.run.id,
            attempt_token=prepared.attempt_token,
            error_code=error_code,
        )
        code = (
            status.HTTP_504_GATEWAY_TIMEOUT
            if exc.category == "timeout"
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(code, error_code) from None
    except TaxonomyBootstrapError as exc:
        await _record_llm_failure(
            db,
            run_id=prepared.run.id,
            attempt_token=prepared.attempt_token,
            error_code=exc.code,
        )
        raise _http_error(exc) from exc
    except Exception:
        await _record_llm_failure(
            db,
            run_id=prepared.run.id,
            attempt_token=prepared.attempt_token,
            error_code="provider_internal_error",
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "provider_internal_error",
        ) from None

    try:
        result = await publish_llm_taxonomy_bootstrap(
            db,
            prepared=prepared,
            proposal=proposal,
        )
        await _commit(db)
    except TaxonomyBootstrapError as exc:
        await _record_llm_failure(
            db,
            run_id=prepared.run.id,
            attempt_token=prepared.attempt_token,
            error_code=exc.code,
        )
        raise _http_error(exc) from exc
    return await _detail_after_commit(
        db,
        organization_id=organization_id,
        actor_user_id=context.user.id,
        run_id=result.run.id,
    )


@router.get(
    "/organizations/{organization_id}/classification-taxonomy-bootstrap/runs",
    response_model=list[TaxonomyBootstrapRunRead],
)
async def taxonomy_bootstrap_runs(
    organization_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=100),
    context: ClassificationAdminRequestContext = Depends(require_taxonomy_bootstrap_admin),
    db: AsyncSession = Depends(get_db),
) -> list[TaxonomyBootstrapRunRead]:
    try:
        runs = await list_taxonomy_bootstrap_runs(
            db,
            organization_id=organization_id,
            actor_user_id=context.user.id,
            limit=limit,
        )
        await _commit(db)
    except TaxonomyBootstrapError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    return [_run_read(run) for run in runs]


@router.get(
    "/organizations/{organization_id}/classification-taxonomy-bootstrap/runs/{run_id}",
    response_model=TaxonomyBootstrapRunRead,
)
async def taxonomy_bootstrap_run_detail(
    organization_id: uuid.UUID,
    run_id: uuid.UUID,
    context: ClassificationAdminRequestContext = Depends(require_taxonomy_bootstrap_admin),
    db: AsyncSession = Depends(get_db),
) -> TaxonomyBootstrapRunRead:
    try:
        detail: TaxonomyBootstrapRunDetail = await get_taxonomy_bootstrap_run_detail(
            db,
            organization_id=organization_id,
            actor_user_id=context.user.id,
            run_id=run_id,
        )
        await _commit(db)
    except TaxonomyBootstrapError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    return _run_read(detail.run, detail.sources)
