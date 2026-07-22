from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings, settings
from app.models.classification_taxonomy import ClassificationLabel, ClassificationTaxonomy
from app.models.classification_taxonomy_bootstrap import (
    TaxonomyBootstrapRun,
    TaxonomyBootstrapSource,
)
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.services import audit_log
from app.services.classification_runtime_contracts import (
    CLASSIFICATION_PROVIDER_NAME,
    canonical_sha256,
)
from app.services.classification_taxonomies import lock_classification_admin_scope
from app.services.classification_taxonomy_bootstrap_contracts import (
    ApplyBuiltinTemplateCommand,
    ImportTaxonomyBootstrapCommand,
    LlmTaxonomyProposal,
    NormalizedTaxonomyDraft,
    PrepareLlmTaxonomyBootstrapCommand,
    TaxonomyBootstrapError,
    normalize_taxonomy_draft,
    require_error_code,
    taxonomy_bootstrap_messages,
    taxonomy_bootstrap_model_config_hash,
)
from app.services.classification_taxonomy_templates import (
    get_taxonomy_bootstrap_template,
)


@dataclass(frozen=True, slots=True)
class TaxonomyBootstrapResult:
    run: TaxonomyBootstrapRun
    taxonomy: ClassificationTaxonomy | None
    labels: tuple[ClassificationLabel, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class BootstrapSampleSnapshot:
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    revision_content_hash: str
    security_level: str


@dataclass(frozen=True, slots=True)
class PreparedLlmTaxonomyBootstrap:
    run: TaxonomyBootstrapRun
    attempt_token: uuid.UUID | None
    actor_user_id: uuid.UUID
    version_key: str
    description: str | None
    snapshots: tuple[BootstrapSampleSnapshot, ...]
    messages: list[dict[str, str]]
    reused: bool


@dataclass(frozen=True, slots=True)
class TaxonomyBootstrapRunDetail:
    run: TaxonomyBootstrapRun
    sources: tuple[TaxonomyBootstrapSource, ...]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _warnings_payload(draft: NormalizedTaxonomyDraft) -> list[dict[str, str]]:
    return [warning.as_dict() for warning in draft.warnings]


def _bootstrap_identity(
    *,
    organization_id: uuid.UUID,
    request_id: uuid.UUID,
    source_type: str,
    source_key: str,
    source_version: str,
    source_hash: str,
    version_key: str,
    description: str | None,
    draft_hash: str | None,
    model_config_hash: str | None,
) -> tuple[str, str]:
    fingerprint = canonical_sha256(
        {
            "contract_version": "taxonomy-bootstrap-input-v1",
            "organization_id": str(organization_id),
            "source_type": source_type,
            "source_key": source_key,
            "source_version": source_version,
            "source_hash": source_hash,
            "version_key": version_key,
            "description": description,
            "draft_hash": draft_hash,
            "model_config_hash": model_config_hash,
        }
    )
    return fingerprint, canonical_sha256(
        {
            "organization_id": str(organization_id),
            "request_id": str(request_id),
            "input_fingerprint": fingerprint,
        }
    )


async def _load_request_run(
    db,
    *,
    organization_id: uuid.UUID,
    request_id: uuid.UUID,
    lock: bool,
) -> TaxonomyBootstrapRun | None:
    statement = select(TaxonomyBootstrapRun).where(
        TaxonomyBootstrapRun.organization_id == organization_id,
        TaxonomyBootstrapRun.request_id == request_id,
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(statement)).scalars().first()


def _require_same_run_identity(
    run: TaxonomyBootstrapRun,
    *,
    source_type: str,
    source_key: str,
    source_version: str,
    source_hash: str,
    input_fingerprint: str,
    idempotency_key: str,
    model_config_hash: str | None,
) -> None:
    expected = (
        source_type,
        source_key,
        source_version,
        source_hash,
        input_fingerprint,
        idempotency_key,
        model_config_hash,
    )
    actual = (
        run.source_type,
        run.source_key,
        run.source_version,
        run.source_hash,
        run.input_fingerprint,
        run.idempotency_key,
        run.model_config_hash,
    )
    if actual != expected:
        raise TaxonomyBootstrapError(
            "bootstrap_idempotency_conflict",
            "taxonomy bootstrap request ID has another identity",
        )


async def _repair_expired_processing_run(
    db,
    *,
    organization_id: uuid.UUID,
    now: datetime,
) -> int:
    runs = (
        await db.execute(
            select(TaxonomyBootstrapRun)
            .where(
                TaxonomyBootstrapRun.organization_id == organization_id,
                TaxonomyBootstrapRun.status == "processing",
                TaxonomyBootstrapRun.expires_at <= now,
            )
            .with_for_update()
        )
    ).scalars().all()
    for run in runs:
        run.status = "failed"
        run.error_code = "bootstrap_attempt_expired"
        run.error_message = "taxonomy bootstrap attempt expired"
        run.attempt_token = None
        run.expires_at = None
        run.finished_at = now
        run.updated_at = now
    if runs:
        await db.flush()
    return len(runs)


async def _require_initial_scope(
    db,
    *,
    organization_id: uuid.UUID,
    ignore_processing_run_id: uuid.UUID | None = None,
) -> None:
    taxonomy_id = (
        await db.execute(
            select(ClassificationTaxonomy.id)
            .where(ClassificationTaxonomy.organization_id == organization_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if taxonomy_id is not None:
        raise TaxonomyBootstrapError(
            "bootstrap_taxonomy_already_initialized",
            "Organization taxonomy is already initialized",
        )
    processing_statement = select(TaxonomyBootstrapRun.id).where(
        TaxonomyBootstrapRun.organization_id == organization_id,
        TaxonomyBootstrapRun.status == "processing",
    )
    if ignore_processing_run_id is not None:
        processing_statement = processing_statement.where(
            TaxonomyBootstrapRun.id != ignore_processing_run_id
        )
    processing_id = (
        await db.execute(
            processing_statement.limit(1)
        )
    ).scalar_one_or_none()
    if processing_id is not None:
        raise TaxonomyBootstrapError(
            "bootstrap_attempt_in_progress",
            "Organization taxonomy bootstrap is already processing",
        )


async def _persist_draft(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    version_key: str,
    description: str | None,
    draft: NormalizedTaxonomyDraft,
) -> tuple[ClassificationTaxonomy, tuple[ClassificationLabel, ...]]:
    taxonomy = ClassificationTaxonomy(
        id=uuid.uuid4(),
        organization_id=organization_id,
        version_key=version_key,
        version_no=1,
        status="draft",
        description=description,
        created_by_user_id=actor_user_id,
    )
    db.add(taxonomy)
    await db.flush()
    ids_by_key = {label.key: uuid.uuid4() for label in draft.labels}
    labels = tuple(
        ClassificationLabel(
            id=ids_by_key[label.key],
            taxonomy_version_id=taxonomy.id,
            key=label.key,
            label=label.label,
            description=label.description,
            parent_label_id=None,
            sort_order=label.sort_order,
            status=label.status,
        )
        for label in draft.labels
    )
    db.add_all(labels)
    await db.flush()
    by_key = {label.key: label for label in labels}
    for source in draft.labels:
        if source.parent_key is not None:
            by_key[source.key].parent_label_id = ids_by_key[source.parent_key]
    await db.flush()
    return taxonomy, labels


async def _load_output(
    db,
    run: TaxonomyBootstrapRun,
) -> tuple[ClassificationTaxonomy | None, tuple[ClassificationLabel, ...]]:
    if run.output_taxonomy_id is None:
        return None, ()
    taxonomy = await db.get(ClassificationTaxonomy, run.output_taxonomy_id)
    labels = tuple(
        (
            await db.execute(
                select(ClassificationLabel)
                .where(ClassificationLabel.taxonomy_version_id == run.output_taxonomy_id)
                .order_by(ClassificationLabel.sort_order, ClassificationLabel.key)
            )
        ).scalars().all()
    )
    return taxonomy, labels


async def _create_direct_bootstrap(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    request_id: uuid.UUID,
    source_type: str,
    source_key: str,
    source_version: str,
    source_hash: str,
    version_key: str,
    description: str | None,
    draft: NormalizedTaxonomyDraft,
    now: datetime,
) -> TaxonomyBootstrapResult:
    await lock_classification_admin_scope(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        lock=True,
    )
    await _repair_expired_processing_run(
        db,
        organization_id=organization_id,
        now=now,
    )
    fingerprint, idempotency_key = _bootstrap_identity(
        organization_id=organization_id,
        request_id=request_id,
        source_type=source_type,
        source_key=source_key,
        source_version=source_version,
        source_hash=source_hash,
        version_key=version_key,
        description=description,
        draft_hash=draft.payload_hash,
        model_config_hash=None,
    )
    existing = await _load_request_run(
        db,
        organization_id=organization_id,
        request_id=request_id,
        lock=True,
    )
    if existing is not None:
        _require_same_run_identity(
            existing,
            source_type=source_type,
            source_key=source_key,
            source_version=source_version,
            source_hash=source_hash,
            input_fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            model_config_hash=None,
        )
        taxonomy, labels = await _load_output(db, existing)
        return TaxonomyBootstrapResult(existing, taxonomy, labels, False)
    await _require_initial_scope(db, organization_id=organization_id)
    try:
        taxonomy, labels = await _persist_draft(
            db,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            version_key=version_key,
            description=description,
            draft=draft,
        )
        run = TaxonomyBootstrapRun(
            id=uuid.uuid4(),
            organization_id=organization_id,
            request_id=request_id,
            source_type=source_type,
            source_key=source_key,
            source_version=source_version,
            source_hash=source_hash,
            input_fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            status="succeeded",
            warning_items=_warnings_payload(draft),
            output_taxonomy_id=taxonomy.id,
            created_by_user_id=actor_user_id,
            started_at=now,
            finished_at=now,
            updated_at=now,
        )
        db.add(run)
        await db.flush()
    except IntegrityError as exc:
        raise TaxonomyBootstrapError("bootstrap_conflict") from exc
    await audit_log.record(
        db,
        actor_user_id,
        "classification.taxonomy_bootstrap",
        {
            "organization_id": str(organization_id),
            "bootstrap_run_id": str(run.id),
            "taxonomy_id": str(taxonomy.id),
            "source_type": source_type,
            "source_version": source_version,
            "source_hash": source_hash,
            "label_count": len(labels),
            "warning_count": len(draft.warnings),
            "status": run.status,
        },
    )
    return TaxonomyBootstrapResult(run, taxonomy, labels, True)


async def apply_builtin_taxonomy_template(
    db,
    command: ApplyBuiltinTemplateCommand,
    *,
    now: datetime | None = None,
) -> TaxonomyBootstrapResult:
    template = get_taxonomy_bootstrap_template(command.template_key)
    description = command.description or template.description
    return await _create_direct_bootstrap(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
        request_id=command.request_id,
        source_type="builtin_template",
        source_key=template.key,
        source_version=template.version,
        source_hash=template.template_hash,
        version_key=command.version_key,
        description=description,
        draft=template.draft,
        now=now or utcnow(),
    )


async def import_taxonomy_bootstrap(
    db,
    command: ImportTaxonomyBootstrapCommand,
    *,
    now: datetime | None = None,
) -> TaxonomyBootstrapResult:
    draft = normalize_taxonomy_draft(command.labels)
    source_hash = canonical_sha256(
        {
            "source_name": command.source_name,
            "source_version": command.source_version,
            "draft_hash": draft.payload_hash,
        }
    )
    return await _create_direct_bootstrap(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
        request_id=command.request_id,
        source_type="admin_import",
        source_key=command.source_name,
        source_version=command.source_version,
        source_hash=source_hash,
        version_key=command.version_key,
        description=command.description,
        draft=draft,
        now=now or utcnow(),
    )


async def _load_sample_scope(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    sample_revision_ids: tuple[uuid.UUID, ...],
    config: Settings,
) -> tuple[
    tuple[Document, ...],
    tuple[Library, ...],
    tuple[DocumentRevision, ...],
]:
    if len(sample_revision_ids) > config.classification_taxonomy_bootstrap_max_samples:
        raise TaxonomyBootstrapError("bootstrap_sample_selection_invalid")
    initial_revisions = tuple(
        (
            await db.execute(
                select(DocumentRevision)
                .where(DocumentRevision.id.in_(sample_revision_ids))
                .order_by(DocumentRevision.id)
            )
        ).scalars().all()
    )
    if len(initial_revisions) != len(sample_revision_ids):
        raise TaxonomyBootstrapError("bootstrap_sample_not_found")
    document_ids = {revision.document_id for revision in initial_revisions}
    documents = tuple(
        (
            await db.execute(
                select(Document)
                .where(Document.id.in_(document_ids))
                .order_by(Document.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )
    if len(documents) != len(document_ids):
        raise TaxonomyBootstrapError("bootstrap_sample_not_found")
    await lock_classification_admin_scope(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        lock=True,
    )
    library_ids = {revision.library_id for revision in initial_revisions}
    libraries = tuple(
        (
            await db.execute(
                select(Library)
                .where(Library.id.in_(library_ids))
                .order_by(Library.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )
    revisions = tuple(
        (
            await db.execute(
                select(DocumentRevision)
                .where(DocumentRevision.id.in_(sample_revision_ids))
                .order_by(DocumentRevision.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )
    if len(libraries) != len(library_ids) or len(revisions) != len(sample_revision_ids):
        raise TaxonomyBootstrapError("bootstrap_sample_not_found")
    documents_by_id = {document.id: document for document in documents}
    libraries_by_id = {library.id: library for library in libraries}
    for revision in revisions:
        document = documents_by_id.get(revision.document_id)
        library = libraries_by_id.get(revision.library_id)
        if document is None or library is None:
            raise TaxonomyBootstrapError("bootstrap_sample_not_found")
        if (
            library.organization_id != organization_id
            or library.deleted_at is not None
            or document.library_id != library.id
            or document.deleted_at is not None
        ):
            raise TaxonomyBootstrapError("bootstrap_sample_not_found")
        if (
            document.current_revision_id != revision.id
            or document.status != "ready"
            or revision.status != "ready"
        ):
            raise TaxonomyBootstrapError("bootstrap_sample_revision_stale")
        if not library.external_llm_enabled:
            raise TaxonomyBootstrapError("bootstrap_library_external_llm_disabled")
        if not library.classification_external_model_enabled:
            raise TaxonomyBootstrapError("bootstrap_library_model_disabled")
        allowed = library.classification_allowed_security_levels
        if (
            not isinstance(allowed, list)
            or not allowed
            or revision.security_level not in allowed
        ):
            raise TaxonomyBootstrapError("bootstrap_security_level_denied")
        if not isinstance(revision.normalized_text, str) or not revision.normalized_text.strip():
            raise TaxonomyBootstrapError("bootstrap_sample_text_unavailable")
    return documents, libraries, revisions


def _ordered_revisions(
    revisions: tuple[DocumentRevision, ...],
    ids: tuple[uuid.UUID, ...],
) -> tuple[DocumentRevision, ...]:
    by_id = {revision.id: revision for revision in revisions}
    return tuple(by_id[revision_id] for revision_id in ids)


def _sample_messages(
    revisions: tuple[DocumentRevision, ...],
    *,
    maximum_chars: int,
) -> list[dict[str, str]]:
    remaining = maximum_chars
    samples: list[tuple[str, str]] = []
    for index, revision in enumerate(revisions):
        remaining_samples = len(revisions) - index
        allowance = max(1, remaining // remaining_samples)
        source = revision.normalized_text.strip()[:allowance]
        remaining -= len(source)
        title = (
            revision.title.strip()
            if isinstance(revision.title, str) and revision.title.strip()
            else "Untitled document"
        )
        samples.append((title[:500], source))
    return taxonomy_bootstrap_messages(tuple(samples))


async def prepare_llm_taxonomy_bootstrap(
    db,
    command: PrepareLlmTaxonomyBootstrapCommand,
    *,
    now: datetime | None = None,
    config: Settings = settings,
) -> PreparedLlmTaxonomyBootstrap:
    if not (
        config.classification_taxonomy_bootstrap_enabled
        and config.classification_taxonomy_bootstrap_llm_enabled
        and config.classification_external_model_enabled
    ):
        raise TaxonomyBootstrapError("bootstrap_llm_disabled")
    now = now or utcnow()
    _, _, revisions = await _load_sample_scope(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
        sample_revision_ids=command.sample_revision_ids,
        config=config,
    )
    await _repair_expired_processing_run(
        db,
        organization_id=command.organization_id,
        now=now,
    )
    ordered = _ordered_revisions(revisions, command.sample_revision_ids)
    snapshots = tuple(
        BootstrapSampleSnapshot(
            library_id=revision.library_id,
            document_id=revision.document_id,
            document_revision_id=revision.id,
            revision_content_hash=revision.content_hash,
            security_level=revision.security_level,
        )
        for revision in ordered
    )
    source_hash = canonical_sha256(
        [
            {
                "library_id": str(snapshot.library_id),
                "document_id": str(snapshot.document_id),
                "document_revision_id": str(snapshot.document_revision_id),
                "revision_content_hash": snapshot.revision_content_hash,
                "security_level": snapshot.security_level,
            }
            for snapshot in snapshots
        ]
    )
    model_hash = taxonomy_bootstrap_model_config_hash(config)
    source_key = "organization-revision-samples"
    source_version = "1"
    fingerprint, idempotency_key = _bootstrap_identity(
        organization_id=command.organization_id,
        request_id=command.request_id,
        source_type="llm_proposal",
        source_key=source_key,
        source_version=source_version,
        source_hash=source_hash,
        version_key=command.version_key,
        description=command.description,
        draft_hash=None,
        model_config_hash=model_hash,
    )
    existing = await _load_request_run(
        db,
        organization_id=command.organization_id,
        request_id=command.request_id,
        lock=True,
    )
    if existing is not None:
        _require_same_run_identity(
            existing,
            source_type="llm_proposal",
            source_key=source_key,
            source_version=source_version,
            source_hash=source_hash,
            input_fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            model_config_hash=model_hash,
        )
        return PreparedLlmTaxonomyBootstrap(
            existing,
            existing.attempt_token,
            command.actor_user_id,
            command.version_key,
            command.description,
            snapshots,
            [],
            True,
        )
    await _require_initial_scope(db, organization_id=command.organization_id)
    attempt_token = uuid.uuid4()
    run = TaxonomyBootstrapRun(
        id=uuid.uuid4(),
        organization_id=command.organization_id,
        request_id=command.request_id,
        source_type="llm_proposal",
        source_key=source_key,
        source_version=source_version,
        source_hash=source_hash,
        input_fingerprint=fingerprint,
        idempotency_key=idempotency_key,
        status="processing",
        warning_items=[],
        model_provider=CLASSIFICATION_PROVIDER_NAME,
        model_name=config.classification_model,
        model_config_hash=model_hash,
        prompt_version=config.classification_taxonomy_bootstrap_prompt_version,
        attempt_token=attempt_token,
        expires_at=now
        + timedelta(seconds=config.classification_taxonomy_bootstrap_attempt_seconds),
        created_by_user_id=command.actor_user_id,
        started_at=now,
        updated_at=now,
    )
    db.add(run)
    db.add_all(
        TaxonomyBootstrapSource(
            id=uuid.uuid4(),
            bootstrap_run_id=run.id,
            ordinal=ordinal,
            library_id=snapshot.library_id,
            document_id=snapshot.document_id,
            document_revision_id=snapshot.document_revision_id,
            revision_content_hash=snapshot.revision_content_hash,
            security_level=snapshot.security_level,
        )
        for ordinal, snapshot in enumerate(snapshots)
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise TaxonomyBootstrapError("bootstrap_conflict") from exc
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.taxonomy_bootstrap_reserve",
        {
            "organization_id": str(command.organization_id),
            "bootstrap_run_id": str(run.id),
            "source_type": run.source_type,
            "source_hash": source_hash,
            "sample_count": len(snapshots),
            "status": run.status,
        },
    )
    return PreparedLlmTaxonomyBootstrap(
        run,
        attempt_token,
        command.actor_user_id,
        command.version_key,
        command.description,
        snapshots,
        _sample_messages(
            ordered,
            maximum_chars=config.classification_taxonomy_bootstrap_max_source_chars,
        ),
        False,
    )


async def _lock_run_by_attempt(
    db,
    *,
    run_id: uuid.UUID,
    attempt_token: uuid.UUID,
) -> TaxonomyBootstrapRun:
    run = (
        await db.execute(
            select(TaxonomyBootstrapRun)
            .where(TaxonomyBootstrapRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if run is None:
        raise TaxonomyBootstrapError("bootstrap_run_not_found")
    if (
        run.status != "processing"
        or run.attempt_token != attempt_token
        or run.expires_at is None
    ):
        raise TaxonomyBootstrapError("bootstrap_attempt_state_changed")
    return run


async def record_llm_taxonomy_bootstrap_failure(
    db,
    *,
    run_id: uuid.UUID,
    attempt_token: uuid.UUID,
    error_code: str,
    now: datetime | None = None,
) -> bool:
    require_error_code(error_code)
    now = now or utcnow()
    try:
        run = await _lock_run_by_attempt(
            db,
            run_id=run_id,
            attempt_token=attempt_token,
        )
    except TaxonomyBootstrapError as exc:
        if exc.code in {"bootstrap_run_not_found", "bootstrap_attempt_state_changed"}:
            return False
        raise
    run.status = "failed"
    run.error_code = error_code
    run.error_message = "taxonomy bootstrap generation did not complete"
    run.attempt_token = None
    run.expires_at = None
    run.finished_at = now
    run.updated_at = now
    await audit_log.record(
        db,
        run.created_by_user_id,
        "classification.taxonomy_bootstrap_failed",
        {
            "organization_id": str(run.organization_id),
            "bootstrap_run_id": str(run.id),
            "source_type": run.source_type,
            "error_code": error_code,
            "status": run.status,
        },
    )
    await db.flush()
    return True


async def publish_llm_taxonomy_bootstrap(
    db,
    *,
    prepared: PreparedLlmTaxonomyBootstrap,
    proposal: LlmTaxonomyProposal,
    now: datetime | None = None,
    config: Settings = settings,
) -> TaxonomyBootstrapResult:
    if prepared.reused or prepared.attempt_token is None:
        raise TaxonomyBootstrapError("bootstrap_attempt_state_changed")
    now = now or utcnow()
    revision_ids = tuple(
        snapshot.document_revision_id for snapshot in prepared.snapshots
    )
    _, _, revisions = await _load_sample_scope(
        db,
        organization_id=prepared.run.organization_id,
        actor_user_id=prepared.actor_user_id,
        sample_revision_ids=revision_ids,
        config=config,
    )
    ordered = _ordered_revisions(revisions, revision_ids)
    for revision, snapshot in zip(ordered, prepared.snapshots, strict=True):
        if (
            revision.library_id != snapshot.library_id
            or revision.document_id != snapshot.document_id
            or revision.content_hash != snapshot.revision_content_hash
            or revision.security_level != snapshot.security_level
        ):
            raise TaxonomyBootstrapError("bootstrap_sample_revision_stale")
    await _require_initial_scope(
        db,
        organization_id=prepared.run.organization_id,
        ignore_processing_run_id=prepared.run.id,
    )
    run = await _lock_run_by_attempt(
        db,
        run_id=prepared.run.id,
        attempt_token=prepared.attempt_token,
    )
    if run.expires_at is None or run.expires_at <= now:
        raise TaxonomyBootstrapError("bootstrap_attempt_expired")
    if run.model_config_hash != taxonomy_bootstrap_model_config_hash(config):
        raise TaxonomyBootstrapError("bootstrap_model_state_changed")
    description = prepared.description or proposal.description
    try:
        taxonomy, labels = await _persist_draft(
            db,
            organization_id=run.organization_id,
            actor_user_id=prepared.actor_user_id,
            version_key=prepared.version_key,
            description=description,
            draft=proposal.draft,
        )
    except IntegrityError as exc:
        raise TaxonomyBootstrapError("bootstrap_conflict") from exc
    run.status = "succeeded"
    run.output_taxonomy_id = taxonomy.id
    run.warning_items = _warnings_payload(proposal.draft)
    run.attempt_token = None
    run.expires_at = None
    run.error_code = None
    run.error_message = None
    run.finished_at = now
    run.updated_at = now
    await audit_log.record(
        db,
        prepared.actor_user_id,
        "classification.taxonomy_bootstrap_publish",
        {
            "organization_id": str(run.organization_id),
            "bootstrap_run_id": str(run.id),
            "taxonomy_id": str(taxonomy.id),
            "source_type": run.source_type,
            "source_hash": run.source_hash,
            "label_count": len(labels),
            "warning_count": len(proposal.draft.warnings),
            "status": run.status,
        },
    )
    await db.flush()
    return TaxonomyBootstrapResult(run, taxonomy, labels, True)


async def list_taxonomy_bootstrap_runs(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    limit: int = 100,
    now: datetime | None = None,
) -> tuple[TaxonomyBootstrapRun, ...]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise TaxonomyBootstrapError("bootstrap_limit_invalid")
    await lock_classification_admin_scope(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        lock=True,
    )
    await _repair_expired_processing_run(
        db,
        organization_id=organization_id,
        now=now or utcnow(),
    )
    return tuple(
        (
            await db.execute(
                select(TaxonomyBootstrapRun)
                .where(TaxonomyBootstrapRun.organization_id == organization_id)
                .order_by(
                    TaxonomyBootstrapRun.created_at.desc(),
                    TaxonomyBootstrapRun.id.desc(),
                )
                .limit(limit)
            )
        ).scalars().all()
    )


async def get_taxonomy_bootstrap_run_detail(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    run_id: uuid.UUID,
    now: datetime | None = None,
) -> TaxonomyBootstrapRunDetail:
    await lock_classification_admin_scope(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        lock=True,
    )
    await _repair_expired_processing_run(
        db,
        organization_id=organization_id,
        now=now or utcnow(),
    )
    run = (
        await db.execute(
            select(TaxonomyBootstrapRun).where(
                TaxonomyBootstrapRun.id == run_id,
                TaxonomyBootstrapRun.organization_id == organization_id,
            )
        )
    ).scalars().first()
    if run is None:
        raise TaxonomyBootstrapError("bootstrap_run_not_found")
    sources = tuple(
        (
            await db.execute(
                select(TaxonomyBootstrapSource)
                .where(TaxonomyBootstrapSource.bootstrap_run_id == run.id)
                .order_by(TaxonomyBootstrapSource.ordinal)
            )
        ).scalars().all()
    )
    return TaxonomyBootstrapRunDetail(run, sources)
