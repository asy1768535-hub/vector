from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings, settings
from app.db import async_session_factory
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.services.knowledge_artifact_generation import (
    KnowledgeArtifactGenerationError,
    deterministic_outline,
    deterministic_summary,
    parse_model_summary,
    summary_messages,
)
from app.services.knowledge_artifact_jobs import (
    ClaimedArtifactJob,
    claim_knowledge_artifact_job,
    clear_job_claim,
    compensate_ready_revision_artifacts,
    lock_artifact_job,
    recover_stale_knowledge_artifact_jobs,
    renew_knowledge_artifact_lease,
    require_live_claim,
    utcnow,
)
from app.services.knowledge_artifact_policy import (
    CANCELLED_ERROR_CODES,
    SUPERSEDED_ERROR_CODES,
    KnowledgeArtifactRuntimeError,
    fail_artifact_runtime,
    require_current_job_identity,
    select_generation_spec,
    validate_enqueue_scope,
)
from app.services.knowledge_artifact_provider import (
    KnowledgeArtifactProviderError,
    OpenAICompatibleSummaryProvider,
)
from app.services.knowledge_artifact_publication import (
    PreparedArtifactJob,
    publish_knowledge_artifact,
)
from app.services.knowledge_artifact_source import (
    load_artifact_source_text,
    load_artifact_title_paths,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ArtifactProcessResult:
    outcome: str
    error_code: str | None = None


async def prepare_knowledge_artifact_job(
    session_factory,
    *,
    claimed: ClaimedArtifactJob,
    now: datetime | None = None,
    config: Settings = settings,
) -> PreparedArtifactJob:
    now = now or utcnow()
    async with session_factory() as db:
        async with db.begin():
            job = await lock_artifact_job(db, claimed.job_id)
            if job is None:
                fail_artifact_runtime(
                    "job_not_found", "knowledge artifact Job was not found"
                )
            require_live_claim(job, claim_token=claimed.claim_token, now=now)
            library = await db.get(Library, job.library_id)
            document = await db.get(Document, job.document_id)
            revision = await db.get(DocumentRevision, job.document_revision_id)
            if library is None or document is None or revision is None:
                fail_artifact_runtime(
                    "runtime_scope_missing", "knowledge artifact Job scope is missing"
                )
            validate_enqueue_scope(
                library=library,
                document=document,
                revision=revision,
                auto_trigger=job.trigger_type == "revision_ready",
                config=config,
            )
            source = await load_artifact_source_text(db, revision=revision)
            spec = select_generation_spec(
                library=library,
                revision=revision,
                artifact_type=job.artifact_type,
                source_character_count=len(source),
                config=config,
            )
            require_current_job_identity(job, revision=revision, spec=spec)
            title_paths = (
                await load_artifact_title_paths(db, revision=revision)
                if job.artifact_type == "outline"
                else ()
            )
            limit = config.knowledge_artifact_model_max_source_chars
            model_source = source[:limit] if job.generation_mode == "model" else source
            return PreparedArtifactJob(
                job_id=job.id,
                claim_token=claimed.claim_token,
                library_id=job.library_id,
                document_id=job.document_id,
                document_revision_id=job.document_revision_id,
                artifact_type=job.artifact_type,
                contract_version=job.contract_version,
                extractor_version=job.extractor_version,
                generation_mode=job.generation_mode,
                input_fingerprint=job.input_fingerprint,
                revision_content_hash=revision.content_hash,
                source_text=model_source,
                source_character_count=len(source),
                source_truncated=len(model_source) < len(source),
                title_paths=title_paths,
                document_title=document.title,
                model_provider=job.model_provider,
                model_name=job.model_name,
                model_config_hash=job.model_config_hash,
            )


async def finish_claimed_artifact_job(
    session_factory,
    *,
    claimed: ClaimedArtifactJob,
    status: str,
    error_code: str,
    now: datetime | None = None,
) -> bool:
    now = now or utcnow()
    async with session_factory() as db:
        async with db.begin():
            job = await lock_artifact_job(db, claimed.job_id)
            if job is None:
                return False
            try:
                require_live_claim(job, claim_token=claimed.claim_token, now=now)
            except KnowledgeArtifactRuntimeError:
                return False
            job.status = status
            job.error_code = error_code[:64]
            job.error_message = "knowledge artifact generation did not complete"
            job.finished_at = now
            clear_job_claim(job)
            await db.flush()
            return True


async def call_provider_with_lease_renewal(
    session_factory,
    *,
    claimed: ClaimedArtifactJob,
    provider,
    messages: list[dict[str, str]],
    renew_seconds: int,
    lease_seconds: int,
):
    task = asyncio.create_task(provider.generate(messages))
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=renew_seconds)
            if task in done:
                return await task
            async with session_factory() as db:
                async with db.begin():
                    renewed = await renew_knowledge_artifact_lease(
                        db,
                        job_id=claimed.job_id,
                        claim_token=claimed.claim_token,
                        lease_seconds=lease_seconds,
                    )
            if not renewed:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                fail_artifact_runtime(
                    "claim_lost",
                    "knowledge artifact lease was lost during model I/O",
                )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _terminal_status(error_code: str) -> str:
    if error_code in SUPERSEDED_ERROR_CODES:
        return "superseded"
    if error_code in CANCELLED_ERROR_CODES:
        return "cancelled"
    return "failed"


async def process_knowledge_artifact_job(
    session_factory,
    *,
    claimed: ClaimedArtifactJob,
    provider=None,
    config: Settings = settings,
) -> ArtifactProcessResult:
    try:
        prepared = await prepare_knowledge_artifact_job(
            session_factory, claimed=claimed, config=config
        )
    except KnowledgeArtifactRuntimeError as exc:
        status = _terminal_status(exc.code)
        await finish_claimed_artifact_job(
            session_factory,
            claimed=claimed,
            status=status,
            error_code=exc.code,
        )
        return ArtifactProcessResult(status, exc.code)

    try:
        if prepared.artifact_type == "outline":
            payload = deterministic_outline(
                list(prepared.title_paths),
                document_title=prepared.document_title,
            )
        elif prepared.generation_mode == "deterministic":
            payload = deterministic_summary(prepared.source_text)
        else:
            adapter = provider or OpenAICompatibleSummaryProvider(
                base_url=config.knowledge_artifact_base_url,
                model=config.knowledge_artifact_model,
                api_key=config.knowledge_artifact_api_key.get_secret_value(),
                timeout_seconds=config.knowledge_artifact_provider_timeout_seconds,
            )
            response = await call_provider_with_lease_renewal(
                session_factory,
                claimed=claimed,
                provider=adapter,
                messages=summary_messages(prepared.source_text),
                renew_seconds=config.knowledge_artifact_worker_renew_seconds,
                lease_seconds=config.knowledge_artifact_worker_lease_seconds,
            )
            payload = parse_model_summary(
                response.content,
                source_character_count=prepared.source_character_count,
                source_truncated=prepared.source_truncated,
            )
    except KnowledgeArtifactProviderError as exc:
        code = f"provider_{exc.category}"[:64]
        await finish_claimed_artifact_job(
            session_factory,
            claimed=claimed,
            status="failed",
            error_code=code,
        )
        return ArtifactProcessResult("failed", code)
    except KnowledgeArtifactRuntimeError as exc:
        if exc.code == "claim_lost":
            return ArtifactProcessResult("lost_lease", exc.code)
        await finish_claimed_artifact_job(
            session_factory,
            claimed=claimed,
            status="failed",
            error_code=exc.code,
        )
        return ArtifactProcessResult("failed", exc.code)
    except (KnowledgeArtifactGenerationError, ValueError) as exc:
        code = getattr(exc, "code", "generation_failed")
        await finish_claimed_artifact_job(
            session_factory,
            claimed=claimed,
            status="failed",
            error_code=code,
        )
        return ArtifactProcessResult("failed", code)

    try:
        async with session_factory() as db:
            async with db.begin():
                await publish_knowledge_artifact(
                    db,
                    prepared=prepared,
                    payload=payload,
                    config=config,
                )
    except KnowledgeArtifactRuntimeError as exc:
        status = _terminal_status(exc.code)
        await finish_claimed_artifact_job(
            session_factory,
            claimed=claimed,
            status=status,
            error_code=exc.code,
        )
        return ArtifactProcessResult(status, exc.code)
    return ArtifactProcessResult("succeeded")


def knowledge_artifact_worker_id() -> str:
    return f"knowledge-artifact-{uuid.uuid4().hex[:12]}"


async def run_knowledge_artifact_worker(
    *,
    watch: bool,
    metadata: dict,
    session_factory=async_session_factory,
    config: Settings = settings,
) -> None:
    worker_id = knowledge_artifact_worker_id()
    metadata.update(
        {
            "worker_id": worker_id,
            "claimed": 0,
            "succeeded": 0,
            "failed": 0,
            "superseded": 0,
            "compensated": 0,
        }
    )
    while True:
        async with session_factory() as db:
            async with db.begin():
                await recover_stale_knowledge_artifact_jobs(
                    db,
                    max_attempts=config.knowledge_artifact_worker_max_attempts,
                )
                claimed = await claim_knowledge_artifact_job(
                    db,
                    worker_id=worker_id,
                    lease_seconds=config.knowledge_artifact_worker_lease_seconds,
                    max_attempts=config.knowledge_artifact_worker_max_attempts,
                )
        if claimed is None:
            try:
                compensated = await compensate_ready_revision_artifacts(
                    limit=100,
                    session_factory=session_factory,
                    config=config,
                )
            except Exception:  # noqa: BLE001
                log.exception("knowledge artifact compensation scan failed")
                compensated = 0
            metadata["compensated"] += compensated
            if compensated:
                continue
            if not watch:
                return
            await asyncio.sleep(config.knowledge_artifact_worker_poll_seconds)
            continue
        metadata["claimed"] += 1
        result = await process_knowledge_artifact_job(
            session_factory,
            claimed=claimed,
            config=config,
        )
        metadata[result.outcome] = metadata.get(result.outcome, 0) + 1
