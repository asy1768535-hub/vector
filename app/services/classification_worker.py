from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.config import Settings, settings
from app.db import async_session_factory
from app.models.classification_job import DocumentClassificationJob
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.services.classification_decision_contracts import (
    ClassificationDecisionError,
    RecordClassificationFailureCommand,
    SubmitClassificationRunCommand,
)
from app.services.classification_decisions import (
    lock_classification_scope,
    record_classification_failure,
    submit_classification_run,
)
from app.services.classification_jobs import (
    ClaimedClassificationJob,
    claim_classification_job,
    clear_classification_claim,
    compensate_ready_revision_classifications,
    list_unrecorded_attempt_failure_job_ids,
    lock_classification_job,
    recover_stale_classification_jobs,
    renew_classification_lease,
    require_live_classification_claim,
    utcnow,
)
from app.services.classification_provider import (
    ClassificationProviderError,
    OpenAICompatibleClassificationProvider,
)
from app.services.classification_runtime_contracts import (
    ClassificationRuntimeError,
    classification_job_identity,
    classification_messages,
    fail_classification_runtime,
    parse_classifier_output,
)
from app.services.classification_runtime_policy import (
    CANCELLED_CLASSIFICATION_CODES,
    SUPERSEDED_CLASSIFICATION_CODES,
    validate_classification_scope,
)
from app.services.knowledge_artifact_source import load_artifact_source_text


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedClassificationJob:
    job_id: uuid.UUID
    claim_token: uuid.UUID
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    revision_content_hash: str
    taxonomy_version_id: uuid.UUID
    enabled_label_ids: tuple[uuid.UUID, ...]
    enabled_label_set_hash: str
    classifier_version: str
    model_provider: str
    model_name: str
    model_config_hash: str
    prompt_version: str
    retry_generation: int
    trigger_type: str
    requested_by_user_id: uuid.UUID | None
    labels_by_key: dict[str, uuid.UUID]
    messages: list[dict[str, str]]


@dataclass(frozen=True, slots=True)
class ClassificationProcessResult:
    outcome: str
    error_code: str | None = None


def _terminal_status(error_code: str) -> str:
    if error_code in SUPERSEDED_CLASSIFICATION_CODES:
        return "superseded"
    if error_code in CANCELLED_CLASSIFICATION_CODES:
        return "cancelled"
    return "failed"


def _require_current_job_identity(
    job: DocumentClassificationJob,
    *,
    revision: DocumentRevision,
    taxonomy_id: uuid.UUID,
    enabled_label_ids: tuple[uuid.UUID, ...],
    config: Settings,
) -> None:
    identity = classification_job_identity(
        library_id=job.library_id,
        document_id=job.document_id,
        document_revision_id=job.document_revision_id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy_id,
        enabled_label_ids=enabled_label_ids,
        retry_generation=job.retry_generation,
        config=config,
    )
    expected = (
        identity.enabled_label_set_hash,
        identity.model_config_hash,
        identity.input_fingerprint,
        identity.idempotency_key,
        config.classification_classifier_version,
        config.classification_prompt_version,
        config.classification_model,
    )
    actual = (
        job.enabled_label_set_hash,
        job.model_config_hash,
        job.input_fingerprint,
        job.idempotency_key,
        job.classifier_version,
        job.prompt_version,
        job.model_name,
    )
    if actual != expected:
        fail_classification_runtime(
            "classification_job_identity_stale",
            "classification Job identity is stale",
        )


async def prepare_classification_job(
    session_factory,
    *,
    claimed: ClaimedClassificationJob,
    now: datetime | None = None,
    config: Settings = settings,
) -> PreparedClassificationJob:
    now = now or utcnow()
    async with session_factory() as db:
        async with db.begin():
            initial_job = await db.get(DocumentClassificationJob, claimed.job_id)
            if initial_job is None:
                fail_classification_runtime(
                    "classification_job_not_found",
                    "classification Job was not found",
                )
            document = (
                await db.execute(
                    select(Document)
                    .where(
                        Document.id == initial_job.document_id,
                        Document.library_id == initial_job.library_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars().first()
            library = await db.get(Library, initial_job.library_id)
            revision = await db.get(
                DocumentRevision,
                initial_job.document_revision_id,
            )
            if library is None or document is None or revision is None:
                fail_classification_runtime(
                    "classification_scope_missing",
                    "classification Job scope is missing",
                )
            validate_classification_scope(
                library=library,
                document=document,
                revision=revision,
                auto_trigger=initial_job.trigger_type == "revision_ready",
                config=config,
            )
            source = await load_artifact_source_text(db, revision=revision)
            if not isinstance(source, str) or not source.strip():
                fail_classification_runtime(
                    "classification_source_empty",
                    "classification source is empty",
                )
            try:
                taxonomy, enabled, all_labels = await lock_classification_scope(
                    db,
                    library=library,
                    expected_taxonomy_id=initial_job.taxonomy_version_id,
                )
            except ClassificationDecisionError as exc:
                raise ClassificationRuntimeError(
                    exc.code,
                    "classification taxonomy scope changed",
                ) from exc
            job = await lock_classification_job(db, claimed.job_id)
            if job is None:
                fail_classification_runtime(
                    "classification_job_not_found",
                    "classification Job was not found",
                )
            require_live_classification_claim(
                job,
                claim_token=claimed.claim_token,
                now=now,
            )
            enabled_ids = tuple(label.id for label in enabled)
            _require_current_job_identity(
                job,
                revision=revision,
                taxonomy_id=taxonomy.id,
                enabled_label_ids=enabled_ids,
                config=config,
            )
            bounded_source = source[: config.classification_model_max_source_chars]
            labels_by_key = {label.key: label.id for label in all_labels.values()}
            selectable = tuple(
                (label.key, label.label, label.description) for label in enabled
            )
            return PreparedClassificationJob(
                job_id=job.id,
                claim_token=claimed.claim_token,
                library_id=job.library_id,
                document_id=job.document_id,
                document_revision_id=job.document_revision_id,
                revision_content_hash=job.revision_content_hash,
                taxonomy_version_id=job.taxonomy_version_id,
                enabled_label_ids=enabled_ids,
                enabled_label_set_hash=job.enabled_label_set_hash,
                classifier_version=job.classifier_version,
                model_provider=job.model_provider,
                model_name=job.model_name,
                model_config_hash=job.model_config_hash,
                prompt_version=job.prompt_version,
                retry_generation=job.retry_generation,
                trigger_type=job.trigger_type,
                requested_by_user_id=job.requested_by_user_id,
                labels_by_key=labels_by_key,
                messages=classification_messages(
                    bounded_source,
                    enabled_labels=selectable,
                ),
            )


async def call_classification_provider_with_lease_renewal(
    session_factory,
    *,
    claimed: ClaimedClassificationJob,
    provider,
    messages: list[dict[str, str]],
    renew_seconds: int,
    lease_seconds: int,
):
    task = asyncio.create_task(provider.generate(messages))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=renew_seconds)
            if task in done:
                return await task
            async with session_factory() as db:
                async with db.begin():
                    renewed = await renew_classification_lease(
                        db,
                        job_id=claimed.job_id,
                        claim_token=claimed.claim_token,
                        lease_seconds=lease_seconds,
                    )
            if not renewed:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                fail_classification_runtime(
                    "classification_claim_lost",
                    "classification lease was lost during model I/O",
                )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _lock_final_scope(
    db,
    *,
    prepared: PreparedClassificationJob,
    now: datetime,
    config: Settings,
):
    document = (
        await db.execute(
            select(Document)
            .where(
                Document.id == prepared.document_id,
                Document.library_id == prepared.library_id,
                Document.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if document is None:
        fail_classification_runtime(
            "document_deleted",
            "classification document is unavailable",
        )
    library = await db.get(Library, prepared.library_id)
    revision = await db.get(DocumentRevision, prepared.document_revision_id)
    if library is None or revision is None:
        fail_classification_runtime(
            "classification_scope_missing",
            "classification Job scope is missing",
        )
    validate_classification_scope(
        library=library,
        document=document,
        revision=revision,
        auto_trigger=prepared.trigger_type == "revision_ready",
        config=config,
    )
    try:
        taxonomy, enabled, _ = await lock_classification_scope(
            db,
            library=library,
            expected_taxonomy_id=prepared.taxonomy_version_id,
            expected_enabled_label_ids=prepared.enabled_label_ids,
        )
    except ClassificationDecisionError as exc:
        raise ClassificationRuntimeError(
            exc.code,
            "classification taxonomy scope changed",
        ) from exc
    job = await lock_classification_job(db, prepared.job_id)
    if job is None:
        fail_classification_runtime(
            "classification_job_not_found",
            "classification Job was not found",
        )
    require_live_classification_claim(
        job,
        claim_token=prepared.claim_token,
        now=now,
    )
    if job.trigger_type != prepared.trigger_type:
        fail_classification_runtime(
            "classification_job_identity_stale",
            "classification Job trigger identity is stale",
        )
    _require_current_job_identity(
        job,
        revision=revision,
        taxonomy_id=taxonomy.id,
        enabled_label_ids=tuple(label.id for label in enabled),
        config=config,
    )
    return job


async def publish_classification_success(
    session_factory,
    *,
    prepared: PreparedClassificationJob,
    proposals,
    now: datetime | None = None,
    config: Settings = settings,
) -> uuid.UUID:
    now = now or utcnow()
    async with session_factory() as db:
        async with db.begin():
            job = await _lock_final_scope(
                db,
                prepared=prepared,
                now=now,
                config=config,
            )
            try:
                result = await submit_classification_run(
                    db,
                    SubmitClassificationRunCommand(
                        library_id=prepared.library_id,
                        document_id=prepared.document_id,
                        document_revision_id=prepared.document_revision_id,
                        revision_content_hash=prepared.revision_content_hash,
                        taxonomy_version_id=prepared.taxonomy_version_id,
                        enabled_label_ids=prepared.enabled_label_ids,
                        classifier_version=prepared.classifier_version,
                        model_provider=prepared.model_provider,
                        model_name=prepared.model_name,
                        model_config_hash=prepared.model_config_hash,
                        prompt_version=prepared.prompt_version,
                        proposals=proposals,
                        trigger_type=job.trigger_type,
                        retry_generation=prepared.retry_generation,
                        requested_by_user_id=prepared.requested_by_user_id,
                    ),
                )
            except ClassificationDecisionError as exc:
                raise ClassificationRuntimeError(exc.code, "classification scope changed") from exc
            job.status = "succeeded"
            job.result_run_id = result.run.id
            job.error_code = None
            job.error_message = None
            job.finished_at = now
            clear_classification_claim(job)
            await db.flush()
            return result.run.id


async def publish_classification_failure(
    session_factory,
    *,
    prepared: PreparedClassificationJob,
    error_code: str,
    now: datetime | None = None,
    config: Settings = settings,
) -> uuid.UUID:
    now = now or utcnow()
    async with session_factory() as db:
        async with db.begin():
            job = await _lock_final_scope(
                db,
                prepared=prepared,
                now=now,
                config=config,
            )
            try:
                result = await record_classification_failure(
                    db,
                    RecordClassificationFailureCommand(
                        library_id=prepared.library_id,
                        document_id=prepared.document_id,
                        document_revision_id=prepared.document_revision_id,
                        revision_content_hash=prepared.revision_content_hash,
                        taxonomy_version_id=prepared.taxonomy_version_id,
                        enabled_label_ids=prepared.enabled_label_ids,
                        classifier_version=prepared.classifier_version,
                        model_provider=prepared.model_provider,
                        model_name=prepared.model_name,
                        model_config_hash=prepared.model_config_hash,
                        prompt_version=prepared.prompt_version,
                        trigger_type=job.trigger_type,
                        error_code=error_code,
                        retry_generation=prepared.retry_generation,
                        requested_by_user_id=prepared.requested_by_user_id,
                    ),
                )
            except ClassificationDecisionError as exc:
                raise ClassificationRuntimeError(exc.code, "classification scope changed") from exc
            job.status = "failed"
            job.result_run_id = result.run.id
            job.error_code = error_code
            job.error_message = "classification generation did not complete"
            job.finished_at = now
            clear_classification_claim(job)
            await db.flush()
            return result.run.id


async def finish_classification_job_without_run(
    session_factory,
    *,
    claimed: ClaimedClassificationJob,
    status: str,
    error_code: str,
    now: datetime | None = None,
) -> bool:
    now = now or utcnow()
    async with session_factory() as db:
        async with db.begin():
            job = await lock_classification_job(db, claimed.job_id)
            if job is None:
                return False
            try:
                require_live_classification_claim(
                    job,
                    claim_token=claimed.claim_token,
                    now=now,
                )
            except ClassificationRuntimeError:
                return False
            job.status = status
            job.result_run_id = None
            job.error_code = error_code[:64]
            job.error_message = "classification generation did not complete"
            job.finished_at = now
            clear_classification_claim(job)
            await db.flush()
            return True


async def record_recovered_classification_failure(
    session_factory,
    *,
    job_id: uuid.UUID,
    now: datetime | None = None,
    config: Settings = settings,
) -> bool:
    now = now or utcnow()
    async with session_factory() as db:
        async with db.begin():
            initial_job = await db.get(DocumentClassificationJob, job_id)
            if initial_job is None:
                return False
            document = (
                await db.execute(
                    select(Document)
                    .where(
                        Document.id == initial_job.document_id,
                        Document.library_id == initial_job.library_id,
                        Document.deleted_at.is_(None),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars().first()
            library = await db.get(Library, initial_job.library_id)
            revision = await db.get(
                DocumentRevision,
                initial_job.document_revision_id,
            )
            scope_error: ClassificationRuntimeError | ClassificationDecisionError | None = None
            if document is None or library is None or revision is None:
                scope_error = ClassificationRuntimeError(
                    "classification_scope_missing",
                    "classification Job scope is missing",
                )
            try:
                if scope_error is None:
                    validate_classification_scope(
                        library=library,
                        document=document,
                        revision=revision,
                        auto_trigger=initial_job.trigger_type == "revision_ready",
                        config=config,
                    )
                    taxonomy, enabled, _ = await lock_classification_scope(
                        db,
                        library=library,
                        expected_taxonomy_id=initial_job.taxonomy_version_id,
                    )
            except (ClassificationRuntimeError, ClassificationDecisionError) as exc:
                scope_error = exc
            job = await lock_classification_job(db, job_id)
            if (
                job is None
                or job.status != "failed"
                or job.result_run_id is not None
                or job.error_code
                not in {"attempt_limit_exceeded", "lease_expired_attempt_limit"}
            ):
                return False
            if scope_error is not None:
                job.status = _terminal_status(scope_error.code)
                job.error_code = scope_error.code[:64]
                job.error_message = (
                    "classification scope changed before failure recording"
                )
                job.updated_at = now
                await db.flush()
                return False
            enabled_ids = tuple(label.id for label in enabled)
            try:
                _require_current_job_identity(
                    job,
                    revision=revision,
                    taxonomy_id=taxonomy.id,
                    enabled_label_ids=enabled_ids,
                    config=config,
                )
                result = await record_classification_failure(
                    db,
                    RecordClassificationFailureCommand(
                        library_id=job.library_id,
                        document_id=job.document_id,
                        document_revision_id=job.document_revision_id,
                        revision_content_hash=job.revision_content_hash,
                        taxonomy_version_id=job.taxonomy_version_id,
                        enabled_label_ids=enabled_ids,
                        classifier_version=job.classifier_version,
                        model_provider=job.model_provider,
                        model_name=job.model_name,
                        model_config_hash=job.model_config_hash,
                        prompt_version=job.prompt_version,
                        trigger_type=job.trigger_type,
                        error_code=job.error_code,
                        retry_generation=job.retry_generation,
                        requested_by_user_id=job.requested_by_user_id,
                    ),
                )
            except (ClassificationRuntimeError, ClassificationDecisionError):
                return False
            job.result_run_id = result.run.id
            job.updated_at = now
            await db.flush()
            return True


async def process_classification_job(
    session_factory,
    *,
    claimed: ClaimedClassificationJob,
    provider=None,
    config: Settings = settings,
) -> ClassificationProcessResult:
    try:
        prepared = await prepare_classification_job(
            session_factory,
            claimed=claimed,
            config=config,
        )
    except ClassificationRuntimeError as exc:
        status = _terminal_status(exc.code)
        await finish_classification_job_without_run(
            session_factory,
            claimed=claimed,
            status=status,
            error_code=exc.code,
        )
        return ClassificationProcessResult(status, exc.code)

    try:
        adapter = provider or OpenAICompatibleClassificationProvider(
            base_url=config.classification_base_url,
            model=config.classification_model,
            api_key=config.classification_api_key.get_secret_value(),
            timeout_seconds=config.classification_provider_timeout_seconds,
        )
        response = await call_classification_provider_with_lease_renewal(
            session_factory,
            claimed=claimed,
            provider=adapter,
            messages=prepared.messages,
            renew_seconds=config.classification_worker_renew_seconds,
            lease_seconds=config.classification_worker_lease_seconds,
        )
        proposals = parse_classifier_output(
            response.content,
            labels_by_key=prepared.labels_by_key,
        )
    except ClassificationProviderError as exc:
        code = f"provider_{exc.category}"[:64]
        try:
            await publish_classification_failure(
                session_factory,
                prepared=prepared,
                error_code=code,
                config=config,
            )
        except ClassificationRuntimeError as publish_error:
            if publish_error.code == "classification_claim_lost":
                return ClassificationProcessResult("lost_lease", publish_error.code)
            status = _terminal_status(publish_error.code)
            await finish_classification_job_without_run(
                session_factory,
                claimed=claimed,
                status=status,
                error_code=publish_error.code,
            )
            return ClassificationProcessResult(status, publish_error.code)
        return ClassificationProcessResult("failed", code)
    except ClassificationRuntimeError as exc:
        if exc.code == "classification_claim_lost":
            return ClassificationProcessResult("lost_lease", exc.code)
        try:
            await publish_classification_failure(
                session_factory,
                prepared=prepared,
                error_code=exc.code,
                config=config,
            )
        except ClassificationRuntimeError as publish_error:
            if publish_error.code == "classification_claim_lost":
                return ClassificationProcessResult("lost_lease", publish_error.code)
            status = _terminal_status(publish_error.code)
            await finish_classification_job_without_run(
                session_factory,
                claimed=claimed,
                status=status,
                error_code=publish_error.code,
            )
            return ClassificationProcessResult(status, publish_error.code)
        return ClassificationProcessResult("failed", exc.code)

    try:
        await publish_classification_success(
            session_factory,
            prepared=prepared,
            proposals=proposals,
            config=config,
        )
    except ClassificationRuntimeError as exc:
        if exc.code == "classification_claim_lost":
            return ClassificationProcessResult("lost_lease", exc.code)
        status = _terminal_status(exc.code)
        await finish_classification_job_without_run(
            session_factory,
            claimed=claimed,
            status=status,
            error_code=exc.code,
        )
        return ClassificationProcessResult(status, exc.code)
    return ClassificationProcessResult("succeeded")


def classification_worker_id() -> str:
    return f"classification-{uuid.uuid4().hex[:12]}"


async def run_classification_worker(
    *,
    watch: bool,
    metadata: dict,
    session_factory=async_session_factory,
    config: Settings = settings,
) -> None:
    worker_id = classification_worker_id()
    metadata.update(
        {
            "worker_id": worker_id,
            "claimed": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "superseded": 0,
            "lost_lease": 0,
            "compensated": 0,
            "recovered_failed": 0,
        }
    )
    while True:
        async with session_factory() as db:
            async with db.begin():
                recovered = await recover_stale_classification_jobs(
                    db,
                    max_attempts=config.classification_worker_max_attempts,
                )
                claimed = await claim_classification_job(
                    db,
                    worker_id=worker_id,
                    lease_seconds=config.classification_worker_lease_seconds,
                    max_attempts=config.classification_worker_max_attempts,
                )
                unrecorded_failure_ids = (
                    await list_unrecorded_attempt_failure_job_ids(db)
                )
        failure_ids = tuple(
            dict.fromkeys((*recovered.failed_job_ids, *unrecorded_failure_ids))
        )
        for failed_job_id in failure_ids:
            try:
                recorded = await record_recovered_classification_failure(
                    session_factory,
                    job_id=failed_job_id,
                    config=config,
                )
                if recorded:
                    metadata["recovered_failed"] += 1
            except Exception:  # noqa: BLE001
                log.exception(
                    "classification terminal recovery recording failed: job=%s",
                    failed_job_id,
                )
        if claimed is None:
            try:
                compensated = await compensate_ready_revision_classifications(
                    limit=100,
                    session_factory=session_factory,
                    config=config,
                )
            except Exception:  # noqa: BLE001
                log.exception("classification compensation scan failed")
                compensated = 0
            metadata["compensated"] += compensated
            if compensated:
                continue
            if not watch:
                return
            await asyncio.sleep(config.classification_worker_poll_seconds)
            continue
        metadata["claimed"] += 1
        result = await process_classification_job(
            session_factory,
            claimed=claimed,
            config=config,
        )
        metadata[result.outcome] = metadata.get(result.outcome, 0) + 1
