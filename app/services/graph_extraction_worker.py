from __future__ import annotations

import asyncio
import os
import socket
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db import async_session_factory
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.services.graph_candidate_aggregation import (
    CandidateAggregationError,
    canonical_graph_value_hash_v1,
    recompute_job_candidate_aggregates,
    stage_unit_candidate_occurrences,
)
from app.services.graph_candidate_routing import apply_job_candidate_routes
from app.services.graph_candidate_validation import validate_job_candidates
from app.services.graph_extraction_attempts import (
    AttemptCompletion,
    AttemptStateError,
    create_pending_attempt,
    finalize_attempt,
)
from app.services.graph_extraction_cache import (
    load_cached_graph_extraction_payload,
    load_replay_graph_extraction_payload,
)
from app.services.graph_extraction_context import (
    ContextBuildError,
    build_context_snapshot,
)
from app.services.graph_extraction_parser import (
    GraphExtractionParseError,
    parse_graph_extraction_output,
)
from app.services.graph_extraction_rate_limit import (
    call_graph_extraction_provider,
    graph_extraction_provider_gate_snapshot,
)
from app.services.graph_extraction_prompt import (
    build_graph_extraction_messages,
    graph_extraction_prompt_hash,
    graph_extraction_prompt_version,
)
from app.services.graph_extraction_provider import (
    GraphExtractionProviderError,
    OpenAICompatibleGraphExtractor,
    graph_extraction_provider_name,
)
from app.services.shadow_rollout import resolve_library_shadow_extraction


UnitTerminalStatus = Literal["succeeded", "failed", "cancelled"]


def shadow_enabled_library_sql_predicate():
    """Return the SQL predicate reserved for shadow-enabled job routing."""

    return and_(
        Library.graph_extraction_enabled.is_(True),
        Library.external_llm_enabled.is_(True),
        or_(
            and_(
                settings.graph_claim_shadow_enabled,
                Library.claim_graph_shadow_policy != "disabled",
            ),
            and_(
                not settings.graph_claim_shadow_enabled,
                Library.claim_graph_shadow_policy == "enabled",
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class StaleUnitRecoveryResult:
    abandoned_attempt_count: int
    failed_unit_count: int
    requeued_unit_count: int

    @property
    def recovered_unit_count(self) -> int:
        return self.failed_unit_count + self.requeued_unit_count


class GraphExtractionWorkerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class LostGraphExtractionLease(GraphExtractionWorkerError):
    def __init__(self) -> None:
        super().__init__("lost_lease", "graph extraction Unit lease is no longer live")


@dataclass(frozen=True, slots=True)
class PreparedGraphExtractionUnit:
    unit_id: uuid.UUID
    job_id: uuid.UUID
    context_snapshot_id: uuid.UUID
    claim_token: uuid.UUID
    messages: list[dict[str, str]]
    model_config_snapshot: dict[str, Any]
    model_config_hash: str
    library_id: uuid.UUID | None = None
    security_level: str | None = None
    visibility_scope: str | None = None
    cache_key: str | None = None
    cache_enabled: bool = False
    cache_hit: bool = False
    shadow_enabled: bool = False
    center_only: bool = False
    has_prior_attempts: bool = False


@dataclass(frozen=True, slots=True)
class GraphExtractionProcessResult:
    outcome: Literal["succeeded", "failed", "cancelled", "lost_lease"]
    error_code: str | None = None
    ready_for_materialization: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_positive_int(value: int, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")


async def claim_graph_extraction_unit(
    db,
    *,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
    now: datetime | None = None,
) -> GraphExtractionUnit | None:
    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 255:
        raise ValueError("worker_id must contain 1 to 255 characters")
    _require_positive_int(lease_seconds, label="lease_seconds")
    _require_positive_int(max_attempts, label="max_attempts")
    claimed_at = now or _utcnow()
    claim_token = uuid.uuid4()

    async with db.begin():
        result = await db.execute(
            select(GraphExtractionUnit)
            .join(
                GraphExtractionJob,
                GraphExtractionJob.id == GraphExtractionUnit.job_id,
            )
            .join(Library, Library.id == GraphExtractionJob.library_id)
            .where(
                GraphExtractionUnit.status == "queued",
                GraphExtractionUnit.model_attempt_count < max_attempts,
                or_(
                    GraphExtractionJob.model_config_snapshot["batch_size"].as_integer().is_(None),
                    and_(
                        GraphExtractionJob.model_config_snapshot["batch_size"].as_integer() >= 1,
                        shadow_enabled_library_sql_predicate(),
                    ),
                ),
                GraphExtractionJob.status.in_(("queued", "processing")),
            )
            .order_by(
                GraphExtractionJob.created_at.asc(),
                GraphExtractionUnit.ordinal.asc(),
                GraphExtractionUnit.id.asc(),
            )
            .with_for_update(skip_locked=True, of=GraphExtractionUnit)
            .limit(1)
        )
        unit = result.scalars().first()
        if unit is None:
            return None

        job = await db.get(GraphExtractionJob, unit.job_id, with_for_update=True)
        if job is None or job.status not in {"queued", "processing"}:
            return None
        unit.status = "processing"
        unit.worker_id = worker_id.strip()
        unit.claim_token = claim_token
        unit.claimed_at = claimed_at
        unit.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        unit.retryable = False
        unit.error_code = None
        unit.error_message = None
        unit.started_at = unit.started_at or claimed_at
        job.status = "processing"
        job.current_stage = "building_context"
        job.started_at = job.started_at or claimed_at
        job.error_code = None
        job.error_message = None
        await db.flush()
        return unit


async def renew_graph_extraction_unit_lease(
    db,
    *,
    unit_id: uuid.UUID,
    claim_token: uuid.UUID,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    _require_positive_int(lease_seconds, label="lease_seconds")
    renewed_at = now or _utcnow()
    async with db.begin():
        result = await db.execute(
            update(GraphExtractionUnit)
            .where(
                GraphExtractionUnit.id == unit_id,
                GraphExtractionUnit.status == "processing",
                GraphExtractionUnit.claim_token == claim_token,
                GraphExtractionUnit.lease_expires_at > renewed_at,
            )
            .values(
                lease_expires_at=renewed_at + timedelta(seconds=lease_seconds),
                updated_at=renewed_at,
            )
        )
        return result.rowcount == 1


async def lock_live_graph_extraction_claim(
    db,
    *,
    unit_id: uuid.UUID,
    claim_token: uuid.UUID,
    now: datetime | None = None,
) -> GraphExtractionUnit | None:
    checked_at = now or _utcnow()
    result = await db.execute(
        select(GraphExtractionUnit)
        .where(
            GraphExtractionUnit.id == unit_id,
            GraphExtractionUnit.status == "processing",
            GraphExtractionUnit.claim_token == claim_token,
            GraphExtractionUnit.lease_expires_at > checked_at,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def mark_claimed_unit_terminal(
    db,
    *,
    unit_id: uuid.UUID,
    claim_token: uuid.UUID,
    status: UnitTerminalStatus,
    retryable: bool = False,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
) -> bool:
    if status not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("status must be succeeded, failed, or cancelled")
    if status != "failed" and retryable:
        raise ValueError("only failed Units may be retryable")
    finished_at = now or _utcnow()
    async with db.begin():
        result = await db.execute(
            update(GraphExtractionUnit)
            .where(
                GraphExtractionUnit.id == unit_id,
                GraphExtractionUnit.status == "processing",
                GraphExtractionUnit.claim_token == claim_token,
                GraphExtractionUnit.lease_expires_at > finished_at,
            )
            .values(
                status=status,
                retryable=retryable,
                worker_id=None,
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
                error_code=error_code,
                error_message=(error_message[:1000] if error_message else None),
                finished_at=finished_at,
                updated_at=finished_at,
            )
        )
        return result.rowcount == 1


async def recover_stale_graph_extraction_units(
    db,
    *,
    max_attempts: int,
    now: datetime | None = None,
) -> StaleUnitRecoveryResult:
    _require_positive_int(max_attempts, label="max_attempts")
    recovered_at = now or _utcnow()
    stale_unit_ids = select(GraphExtractionUnit.id).where(
        GraphExtractionUnit.status == "processing",
        GraphExtractionUnit.lease_expires_at.is_not(None),
        GraphExtractionUnit.lease_expires_at <= recovered_at,
    )

    async with db.begin():
        affected_result = await db.execute(
            select(GraphExtractionUnit.job_id).where(GraphExtractionUnit.id.in_(stale_unit_ids)).distinct()
        )
        affected_job_ids = list(affected_result.scalars().all())
        abandoned = await db.execute(
            update(ExtractionRawOutputAttempt)
            .where(
                ExtractionRawOutputAttempt.extraction_unit_id.in_(stale_unit_ids),
                ExtractionRawOutputAttempt.request_status == "pending",
            )
            .values(
                request_status="abandoned",
                abandoned_at=recovered_at,
                abandoned_reason="lease_expired",
                updated_at=recovered_at,
            )
        )
        failed = await db.execute(
            update(GraphExtractionUnit)
            .where(
                GraphExtractionUnit.id.in_(stale_unit_ids),
                GraphExtractionUnit.model_attempt_count >= max_attempts,
            )
            .values(
                status="failed",
                retryable=False,
                worker_id=None,
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
                error_code="model_attempt_budget_exhausted",
                error_message=None,
                finished_at=recovered_at,
                updated_at=recovered_at,
            )
        )
        requeued = await db.execute(
            update(GraphExtractionUnit)
            .where(
                GraphExtractionUnit.id.in_(stale_unit_ids),
                GraphExtractionUnit.model_attempt_count < max_attempts,
            )
            .values(
                status="queued",
                retryable=True,
                worker_id=None,
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
                error_code="lease_expired",
                error_message=None,
                finished_at=None,
                updated_at=recovered_at,
            )
        )
        for job_id in affected_job_ids:
            job = await db.get(GraphExtractionJob, job_id, with_for_update=True)
            if job is not None and job.status in {"queued", "processing"}:
                await _refresh_graph_extraction_job_state(
                    db,
                    job=job,
                    now=recovered_at,
                )
    return StaleUnitRecoveryResult(
        abandoned_attempt_count=int(abandoned.rowcount or 0),
        failed_unit_count=int(failed.rowcount or 0),
        requeued_unit_count=int(requeued.rowcount or 0),
    )


async def _load_live_scope(db, *, unit: GraphExtractionUnit):
    job = await db.get(GraphExtractionJob, unit.job_id, with_for_update=True)
    if job is None or job.status != "processing":
        raise GraphExtractionWorkerError(
            "job_not_processing",
            "graph extraction Job is no longer processing",
        )
    library = await db.get(Library, job.library_id)
    document = await db.get(Document, job.document_id)
    revision = await db.get(DocumentRevision, job.document_revision_id)
    ontology = await db.get(OntologyVersion, job.ontology_version_id)
    _require_live_scope_safety(
        job=job,
        unit=unit,
        library=library,
        document=document,
        revision=revision,
        ontology=ontology,
    )
    return job, library, document, revision, ontology


def _require_live_scope_safety(
    *,
    job: GraphExtractionJob,
    unit: GraphExtractionUnit,
    library: Library | None,
    document: Document | None,
    revision: DocumentRevision | None,
    ontology: OntologyVersion | None,
) -> None:
    if not settings.graph_extraction_enabled:
        raise GraphExtractionWorkerError(
            "graph_extraction_disabled",
            "graph extraction is disabled globally",
        )
    if library is None or getattr(library, "deleted_at", None) is not None:
        raise GraphExtractionWorkerError("library_unavailable", "Library is unavailable")
    if not library.graph_extraction_enabled or not library.external_llm_enabled:
        raise GraphExtractionWorkerError(
            "library_opt_out",
            "Library no longer permits graph extraction",
        )
    allowed_levels = library.graph_extraction_allowed_security_levels
    if not isinstance(allowed_levels, list) or not allowed_levels:
        raise GraphExtractionWorkerError(
            "security_allowlist_empty",
            "Library graph extraction security allowlist is empty",
        )
    if (
        document is None
        or document.library_id != library.id
        or document.deleted_at is not None
        or document.current_revision_id != job.document_revision_id
        or document.status != "ready"
    ):
        raise GraphExtractionWorkerError(
            "document_not_current",
            "graph extraction document is deleted or no longer current",
        )
    if (
        revision is None
        or revision.library_id != library.id
        or revision.document_id != document.id
        or revision.id != job.document_revision_id
        or revision.status != "ready"
    ):
        raise GraphExtractionWorkerError(
            "revision_not_ready",
            "graph extraction revision is not the current ready revision",
        )
    if (
        not isinstance(revision.security_level, str)
        or not revision.security_level.strip()
        or revision.security_level.strip() not in allowed_levels
    ):
        raise GraphExtractionWorkerError(
            "security_level_denied",
            "revision security level is no longer authorized",
        )
    frozen_snapshot = job.ontology_snapshot or {}
    explicit_ai_draft = bool(
        job.schema_discovery_run_id is not None
        and frozen_snapshot.get("schema_state") == "ai_draft"
        and frozen_snapshot.get("confirmed") is False
        and frozen_snapshot.get("ontology_version_id") == str(job.ontology_version_id)
    )
    if (
        ontology is None
        or ontology.id != job.ontology_version_id
        or ontology.library_id != library.id
        or (ontology.status != "active" and not (explicit_ai_draft and ontology.status == "draft"))
    ):
        raise GraphExtractionWorkerError(
            "active_ontology_changed",
            "frozen ontology is no longer active",
        )
    if unit.job_id != job.id or unit.library_id != library.id or unit.document_revision_id != revision.id:
        raise GraphExtractionWorkerError(
            "unit_scope_mismatch",
            "graph extraction Unit scope does not match its Job",
        )


def _context_limits(job: GraphExtractionJob) -> tuple[int, int, int]:
    snapshot = job.model_config_snapshot
    try:
        config_hash = canonical_graph_value_hash_v1(snapshot) if isinstance(snapshot, dict) else None
    except (TypeError, ValueError):
        config_hash = None
    if config_hash is None or job.model_config_hash != config_hash:
        raise GraphExtractionWorkerError(
            "model_config_hash_mismatch",
            "frozen model configuration is malformed",
        )
    previous = snapshot.get("previous_chunks")
    following = snapshot.get("next_chunks")
    maximum = snapshot.get("max_context_chars")
    if (
        isinstance(previous, bool)
        or not isinstance(previous, int)
        or previous < 0
        or isinstance(following, bool)
        or not isinstance(following, int)
        or following < 0
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or maximum < 1
    ):
        raise GraphExtractionWorkerError(
            "invalid_context_policy",
            "frozen Context limits are malformed",
        )
    return previous, following, maximum


def _center_only_prompt(job: GraphExtractionJob) -> bool:
    policy_snapshot = job.policy_config_snapshot
    try:
        policy_hash = (
            canonical_graph_value_hash_v1(policy_snapshot) if isinstance(policy_snapshot, dict) else None
        )
    except (TypeError, ValueError):
        policy_hash = None
    if policy_hash is None or job.policy_config_hash != policy_hash:
        raise GraphExtractionWorkerError(
            "policy_config_hash_mismatch",
            "frozen extraction policy is malformed",
        )
    center_only = policy_snapshot.get("center_only", False)
    if not isinstance(center_only, bool):
        raise GraphExtractionWorkerError(
            "invalid_extraction_policy",
            "frozen center-only policy is malformed",
        )
    if job.prompt_version != graph_extraction_prompt_version(
        center_only=center_only
    ) or job.prompt_content_hash != graph_extraction_prompt_hash(center_only=center_only):
        raise GraphExtractionWorkerError(
            "prompt_contract_mismatch",
            "frozen Prompt contract is unsupported",
        )
    return center_only


async def _prepare_graph_extraction_unit(
    session_factory,
    *,
    unit_id: uuid.UUID,
    claim_token: uuid.UUID,
) -> PreparedGraphExtractionUnit:
    async with session_factory() as db:
        async with db.begin():
            unit = await lock_live_graph_extraction_claim(
                db,
                unit_id=unit_id,
                claim_token=claim_token,
            )
            if unit is None:
                raise LostGraphExtractionLease()
            job, _library, _document, _revision, _ontology = await _load_live_scope(
                db,
                unit=unit,
            )
            center_only = _center_only_prompt(job)
            previous, following, maximum = _context_limits(job)
            job.current_stage = "building_context"
            snapshot = await build_context_snapshot(
                db,
                job=job,
                unit=unit,
                previous_chunks=previous,
                next_chunks=following,
                max_context_chars=maximum,
            )
            if snapshot.context_text is None or snapshot.purged_at is not None:
                raise GraphExtractionWorkerError(
                    "context_unavailable",
                    "graph extraction Context is unavailable",
                )
            messages = build_graph_extraction_messages(
                context_text=snapshot.context_text,
                ontology_snapshot=job.ontology_snapshot,
                center_only=center_only,
            )
            job.current_stage = "extracting"
            await db.flush()
            cache_enabled = (
                job.model_config_snapshot.get("cache_policy_version") == "semantic-v1"
            )
            cache_key = canonical_graph_value_hash_v1(
                {
                    "library_id": str(job.library_id),
                    "security_level": _revision.security_level,
                    "visibility_scope": _revision.visibility_scope,
                    "context_hash": snapshot.context_hash,
                    "schema_subset_hash": job.ontology_snapshot_hash,
                    "prompt_version": job.prompt_version,
                    "prompt_content_hash": job.prompt_content_hash,
                    "output_parser_version": job.output_parser_version,
                    "context_policy_version": job.context_policy_version,
                    "extraction_policy_version": job.extraction_policy_version,
                    "normalization_rule_version": job.normalization_rule_version,
                    "confidence_policy_version": job.confidence_policy_version,
                    "policy_config_hash": job.policy_config_hash,
                    "model_config_hash": job.model_config_hash,
                }
            )
            return PreparedGraphExtractionUnit(
                unit_id=unit.id,
                job_id=job.id,
                context_snapshot_id=snapshot.id,
                claim_token=claim_token,
                messages=messages,
                model_config_snapshot=dict(job.model_config_snapshot),
                model_config_hash=job.model_config_hash,
                library_id=job.library_id,
                security_level=_revision.security_level,
                visibility_scope=_revision.visibility_scope,
                cache_key=cache_key,
                cache_enabled=cache_enabled,
                shadow_enabled=resolve_library_shadow_extraction(_library),
                center_only=center_only,
                has_prior_attempts=unit.model_attempt_count > 0,
            )


async def _preflight_provider_call(
    session_factory,
    *,
    unit_id: uuid.UUID,
    claim_token: uuid.UUID,
) -> None:
    async with session_factory() as db:
        async with db.begin():
            unit = await lock_live_graph_extraction_claim(
                db,
                unit_id=unit_id,
                claim_token=claim_token,
            )
            if unit is None:
                raise LostGraphExtractionLease()
            job, _library, _document, _revision, _ontology = await _load_live_scope(
                db,
                unit=unit,
            )
            job.current_stage = "extracting"
            await db.flush()


def _request_payload_hash(prepared: PreparedGraphExtractionUnit) -> str:
    if prepared.cache_enabled and prepared.cache_key is not None:
        return prepared.cache_key
    return canonical_graph_value_hash_v1(
        {
            "messages": prepared.messages,
            "model_config_hash": prepared.model_config_hash,
        }
    )


async def _load_prepared_cache(
    session_factory,
    prepared: PreparedGraphExtractionUnit,
):
    if (
        not prepared.cache_enabled
        or prepared.cache_key is None
        or prepared.library_id is None
    ):
        return None
    async with session_factory() as db:
        return await load_cached_graph_extraction_payload(
            db,
            cache_key=prepared.cache_key,
            library_id=prepared.library_id,
            security_level=prepared.security_level,
            visibility_scope=prepared.visibility_scope,
            current_unit_id=prepared.unit_id,
        )


async def _load_prepared_replay(
    session_factory,
    prepared: PreparedGraphExtractionUnit,
):
    if not prepared.has_prior_attempts:
        return None
    async with session_factory() as db:
        return await load_replay_graph_extraction_payload(
            db,
            unit_id=prepared.unit_id,
            context_snapshot_id=prepared.context_snapshot_id,
        )


def _configured_provider(prepared: PreparedGraphExtractionUnit):
    snapshot = prepared.model_config_snapshot
    provider_name = snapshot.get("provider")
    base_url = snapshot.get("base_url")
    model = snapshot.get("model")
    timeout = snapshot.get("timeout_seconds")
    max_output_tokens = snapshot.get("max_output_tokens")
    if (
        not isinstance(provider_name, str)
        or not provider_name.strip()
        or not isinstance(base_url, str)
        or not base_url.strip()
        or not isinstance(model, str)
        or not model.strip()
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
        or (
            max_output_tokens is not None
            and (
                isinstance(max_output_tokens, bool)
                or not isinstance(max_output_tokens, int)
                or max_output_tokens < 1
            )
        )
    ):
        raise GraphExtractionWorkerError(
            "provider_not_configured",
            "frozen Provider configuration is invalid",
        )
    try:
        expected_provider_name = graph_extraction_provider_name(
            base_url=base_url,
            model=model,
        )
    except ValueError as exc:
        raise GraphExtractionWorkerError(
            "provider_config_retired",
            "frozen Provider configuration is not an approved contract",
        ) from exc
    if provider_name != expected_provider_name:
        raise GraphExtractionWorkerError(
            "provider_config_retired",
            "frozen Provider name does not match its approved contract",
        )
    api_key = settings.graph_extraction_api_key.get_secret_value().strip()
    if not api_key:
        raise GraphExtractionWorkerError(
            "provider_not_configured",
            "graph extraction Provider credential is unavailable",
        )
    return OpenAICompatibleGraphExtractor(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=float(timeout),
        max_output_tokens=max_output_tokens,
    )


async def _call_provider_with_lease_renewal(
    session_factory,
    *,
    provider,
    prepared: PreparedGraphExtractionUnit,
    lease_seconds: int,
    renew_seconds: int,
):
    _require_positive_int(lease_seconds, label="lease_seconds")
    _require_positive_int(renew_seconds, label="renew_seconds")
    lease_lost = False

    async def renew_loop() -> None:
        nonlocal lease_lost
        while True:
            await asyncio.sleep(renew_seconds)
            async with session_factory() as db:
                renewed = await renew_graph_extraction_unit_lease(
                    db,
                    unit_id=prepared.unit_id,
                    claim_token=prepared.claim_token,
                    lease_seconds=lease_seconds,
                )
            if not renewed:
                lease_lost = True
                return

    renew_task = asyncio.create_task(renew_loop())
    try:
        response = await call_graph_extraction_provider(
            lambda: provider.extract(prepared.messages),
            concurrency=settings.graph_extraction_provider_max_concurrency,
            max_retries=settings.graph_extraction_provider_max_retries,
            backoff_base_seconds=(settings.graph_extraction_provider_backoff_base_seconds),
        )
    finally:
        renew_task.cancel()
        try:
            await renew_task
        except asyncio.CancelledError:
            pass
    return response, lease_lost


async def _refresh_graph_extraction_job_state(
    db,
    *,
    job: GraphExtractionJob,
    now: datetime,
) -> bool:
    result = await db.execute(
        select(GraphExtractionUnit.status, func.count(GraphExtractionUnit.id))
        .where(GraphExtractionUnit.job_id == job.id)
        .group_by(GraphExtractionUnit.status)
    )
    by_status = {status: int(count) for status, count in result.all()}
    counts = {
        "total": sum(by_status.values()),
        "queued": by_status.get("queued", 0),
        "processing": by_status.get("processing", 0),
        "succeeded": by_status.get("succeeded", 0),
        "failed": by_status.get("failed", 0),
        "cancelled": by_status.get("cancelled", 0),
    }
    job.counts = counts
    pending = counts["queued"] + counts["processing"]
    if pending:
        job.status = "processing"
        job.current_stage = "extracting"
        job.finished_at = None
        return False
    if counts["failed"] or counts["cancelled"]:
        successful = counts["succeeded"] > 0
        job.status = "partially_succeeded" if successful else "failed"
        job.current_stage = "finalizing"
        job.error_code = "unit_failures"
        job.error_message = None
        job.finished_at = now
        return False
    if job.execution_mode == "production":
        job.status = "processing"
        job.current_stage = "materializing"
        job.error_code = None
        job.error_message = None
        job.finished_at = None
        return True
    job.status = "succeeded"
    job.current_stage = "finalizing"
    job.error_code = None
    job.error_message = None
    job.finished_at = now
    return False


def _set_unit_terminal(
    unit: GraphExtractionUnit,
    *,
    status: UnitTerminalStatus,
    retryable: bool,
    error_code: str | None,
    error_message: str | None,
    now: datetime,
) -> None:
    unit.status = status
    unit.retryable = retryable
    unit.worker_id = None
    unit.claim_token = None
    unit.claimed_at = None
    unit.lease_expires_at = None
    unit.error_code = error_code[:64] if error_code else None
    unit.error_message = error_message[:1000] if error_message else None
    unit.finished_at = now


async def _finish_claim_after_error(
    session_factory,
    *,
    unit_id: uuid.UUID,
    claim_token: uuid.UUID,
    max_attempts: int,
    status: UnitTerminalStatus,
    error_code: str,
    error_message: str | None = None,
    abandon_pending: bool = False,
) -> bool:
    finished_at = _utcnow()
    async with session_factory() as db:
        async with db.begin():
            unit = await lock_live_graph_extraction_claim(
                db,
                unit_id=unit_id,
                claim_token=claim_token,
                now=finished_at,
            )
            if unit is None:
                return False
            job = await db.get(GraphExtractionJob, unit.job_id, with_for_update=True)
            if job is None:
                return False
            if abandon_pending:
                await db.execute(
                    update(ExtractionRawOutputAttempt)
                    .where(
                        ExtractionRawOutputAttempt.extraction_unit_id == unit.id,
                        ExtractionRawOutputAttempt.request_status == "pending",
                        ExtractionRawOutputAttempt.claim_token == claim_token,
                    )
                    .values(
                        request_status="abandoned",
                        abandoned_at=finished_at,
                        abandoned_reason="unit_cancelled",
                        updated_at=finished_at,
                    )
                )
            retryable = status == "failed" and unit.model_attempt_count < max_attempts
            _set_unit_terminal(
                unit,
                status=status,
                retryable=retryable,
                error_code=error_code,
                error_message=error_message,
                now=finished_at,
            )
            await db.flush()
            await _refresh_graph_extraction_job_state(db, job=job, now=finished_at)
            return True


def _validate_center_only_evidence(payload) -> None:
    claims = [claim for fact in (*payload.entities, *payload.relations) for claim in fact.evidence]
    if any(claim.context_ref != "c0" for claim in claims):
        raise GraphExtractionWorkerError(
            "neighbor_evidence_forbidden",
            "center-only extraction cannot persist neighbor-only evidence",
        )


async def _persist_candidate_result(
    session_factory,
    *,
    prepared: PreparedGraphExtractionUnit,
    payload,
) -> bool | None:
    completed_at = _utcnow()
    async with session_factory() as db:
        async with db.begin():
            unit = await lock_live_graph_extraction_claim(
                db,
                unit_id=prepared.unit_id,
                claim_token=prepared.claim_token,
                now=completed_at,
            )
            if unit is None:
                return None
            job, _library, _document, _revision, _ontology = await _load_live_scope(
                db,
                unit=unit,
            )
            if _center_only_prompt(job):
                _validate_center_only_evidence(payload)
            snapshot = await db.get(
                ExtractionContextSnapshot,
                prepared.context_snapshot_id,
            )
            if snapshot is None or snapshot.extraction_unit_id != unit.id or snapshot.purged_at is not None:
                raise GraphExtractionWorkerError(
                    "context_unavailable",
                    "graph extraction Context is missing or purged",
                )

            job.current_stage = "binding_evidence"
            await stage_unit_candidate_occurrences(
                db,
                job=job,
                unit=unit,
                snapshot=snapshot,
                payload=payload,
            )
            job.current_stage = "aggregating"
            aggregate = await recompute_job_candidate_aggregates(db, job=job)
            job.current_stage = "validating"
            validation = await validate_job_candidates(db, job=job)
            job.current_stage = "scoring"
            routes = await apply_job_candidate_routes(db, job=job)
            statistics = dict(job.statistics or {})
            if prepared.cache_hit:
                statistics["cache_hits"] = int(statistics.get("cache_hits") or 0) + 1
            provider_gate = graph_extraction_provider_gate_snapshot()
            if provider_gate is not None:
                statistics["provider_gate"] = asdict(provider_gate)
            statistics["candidate_pipeline"] = {
                "entity_candidate_count": aggregate.entity_candidate_count,
                "relation_candidate_count": aggregate.relation_candidate_count,
                "property_conflict_count": aggregate.property_conflict_count,
                "validation_rejected_count": validation.rejected_count,
                "validation_pending_review_count": validation.pending_review_count,
                "validated_count": routes.validated_count,
                "pending_review_count": routes.pending_review_count,
                "rejected_count": routes.rejected_count,
                "extraction": {
                    "entity_count": aggregate.entity_candidate_count,
                    "relation_count": aggregate.relation_candidate_count,
                },
                "validation": {
                    "rejected_count": validation.rejected_count,
                    "pending_review_count": validation.pending_review_count,
                    "conflict_count": validation.conflict_count,
                },
                "routing": {
                    "entities": routes.entity_status_counts,
                    "relations": routes.relation_status_counts,
                },
                "failure_reasons": routes.failure_reasons,
            }
            job.statistics = statistics
            _set_unit_terminal(
                unit,
                status="succeeded",
                retryable=False,
                error_code=None,
                error_message=None,
                now=completed_at,
            )
            await db.flush()
            return await _refresh_graph_extraction_job_state(
                db,
                job=job,
                now=completed_at,
            )


async def _persist_payload_process_result(
    session_factory,
    *,
    prepared: PreparedGraphExtractionUnit,
    payload,
    max_attempts: int,
) -> GraphExtractionProcessResult:
    try:
        ready = await _persist_candidate_result(
            session_factory,
            prepared=prepared,
            payload=payload,
        )
    except GraphExtractionWorkerError as exc:
        finished = await _finish_claim_after_error(
            session_factory,
            unit_id=prepared.unit_id,
            claim_token=prepared.claim_token,
            max_attempts=max_attempts,
            status="cancelled",
            error_code=exc.code,
        )
        outcome = "cancelled" if finished else "lost_lease"
        return GraphExtractionProcessResult(outcome, exc.code)
    except CandidateAggregationError as exc:
        finished = await _finish_claim_after_error(
            session_factory,
            unit_id=prepared.unit_id,
            claim_token=prepared.claim_token,
            max_attempts=max_attempts,
            status="failed",
            error_code=exc.code,
            error_message=type(exc).__name__,
        )
        outcome = "failed" if finished else "lost_lease"
        return GraphExtractionProcessResult(outcome, exc.code)
    except (DBAPIError, OSError):
        raise
    except Exception as exc:  # noqa: BLE001
        finished = await _finish_claim_after_error(
            session_factory,
            unit_id=prepared.unit_id,
            claim_token=prepared.claim_token,
            max_attempts=max_attempts,
            status="failed",
            error_code="candidate_processing_failed",
            error_message=type(exc).__name__,
        )
        outcome = "failed" if finished else "lost_lease"
        return GraphExtractionProcessResult(outcome, "candidate_processing_failed")
    if ready is None:
        return GraphExtractionProcessResult("lost_lease", "lost_lease")
    return GraphExtractionProcessResult(
        "succeeded",
        ready_for_materialization=ready,
    )


async def process_graph_extraction_unit(
    session_factory,
    *,
    unit_id: uuid.UUID,
    claim_token: uuid.UUID,
    provider=None,
    shadow_provider=None,
    lease_seconds: int | None = None,
    renew_seconds: int | None = None,
    max_attempts: int | None = None,
) -> GraphExtractionProcessResult:
    lease_seconds = lease_seconds or settings.graph_extraction_unit_lease_seconds
    renew_seconds = renew_seconds or settings.graph_extraction_unit_lease_renew_seconds
    max_attempts = max_attempts or settings.graph_extraction_worker_max_model_attempts
    try:
        prepared = await _prepare_graph_extraction_unit(
            session_factory,
            unit_id=unit_id,
            claim_token=claim_token,
        )
    except LostGraphExtractionLease:
        return GraphExtractionProcessResult("lost_lease", "lost_lease")
    except GraphExtractionWorkerError as exc:
        finished = await _finish_claim_after_error(
            session_factory,
            unit_id=unit_id,
            claim_token=claim_token,
            max_attempts=max_attempts,
            status="cancelled",
            error_code=exc.code,
            abandon_pending=True,
        )
        outcome = "cancelled" if finished else "lost_lease"
        return GraphExtractionProcessResult(outcome, exc.code)
    except ContextBuildError as exc:
        finished = await _finish_claim_after_error(
            session_factory,
            unit_id=unit_id,
            claim_token=claim_token,
            max_attempts=max_attempts,
            status="failed",
            error_code="context_build_failed",
            error_message=str(exc),
        )
        outcome = "failed" if finished else "lost_lease"
        return GraphExtractionProcessResult(outcome, "context_build_failed")

    replayed_payload = await _load_prepared_replay(session_factory, prepared)
    if replayed_payload is not None:
        replayed_result = await _persist_payload_process_result(
            session_factory,
            prepared=prepared,
            payload=replayed_payload,
            max_attempts=max_attempts,
        )
        if prepared.shadow_enabled and replayed_result.outcome == "succeeded":
            from app.services.graph_claim_shadow_worker import record_shadow_skip

            await record_shadow_skip(
                session_factory,
                job_id=prepared.job_id,
                reason="canonical_replay",
            )
        return replayed_result

    request_hash = _request_payload_hash(prepared)
    cached_payload = await _load_prepared_cache(session_factory, prepared)
    if cached_payload is not None:
        cached_result = await _persist_payload_process_result(
            session_factory,
            prepared=replace(prepared, cache_hit=True),
            payload=cached_payload,
            max_attempts=max_attempts,
        )
        if prepared.shadow_enabled and cached_result.outcome == "succeeded":
            from app.services.graph_claim_shadow_worker import record_shadow_skip

            await record_shadow_skip(
                session_factory,
                job_id=prepared.job_id,
                reason="canonical_cache_hit",
            )
        return cached_result
    try:
        async with session_factory() as db:
            attempt = await create_pending_attempt(
                db,
                unit_id=unit_id,
                context_snapshot_id=prepared.context_snapshot_id,
                claim_token=claim_token,
                request_payload_hash=request_hash,
                max_attempts=max_attempts,
            )
    except AttemptStateError:
        finished = await _finish_claim_after_error(
            session_factory,
            unit_id=unit_id,
            claim_token=claim_token,
            max_attempts=max_attempts,
            status="failed",
            error_code="attempt_preparation_failed",
        )
        outcome = "failed" if finished else "lost_lease"
        return GraphExtractionProcessResult(outcome, "attempt_preparation_failed")

    try:
        await _preflight_provider_call(
            session_factory,
            unit_id=unit_id,
            claim_token=claim_token,
        )
        active_provider = provider or _configured_provider(prepared)
    except LostGraphExtractionLease:
        return GraphExtractionProcessResult("lost_lease", "lost_lease")
    except GraphExtractionWorkerError as exc:
        finished = await _finish_claim_after_error(
            session_factory,
            unit_id=unit_id,
            claim_token=claim_token,
            max_attempts=max_attempts,
            status="cancelled",
            error_code=exc.code,
            abandon_pending=True,
        )
        outcome = "cancelled" if finished else "lost_lease"
        return GraphExtractionProcessResult(outcome, exc.code)

    try:
        response, lease_lost = await _call_provider_with_lease_renewal(
            session_factory,
            provider=active_provider,
            prepared=prepared,
            lease_seconds=lease_seconds,
            renew_seconds=renew_seconds,
        )
    except GraphExtractionProviderError as exc:
        completion = AttemptCompletion(
            request_status=exc.category,
            latency_ms=exc.latency_ms,
        )
        async with session_factory() as db:
            finalized = await finalize_attempt(
                db,
                attempt_id=attempt.id,
                claim_token=claim_token,
                completion=completion,
            )
        if not finalized:
            return GraphExtractionProcessResult("lost_lease", "lost_lease")
        error_code = f"provider_{exc.category}"
        finished = await _finish_claim_after_error(
            session_factory,
            unit_id=unit_id,
            claim_token=claim_token,
            max_attempts=max_attempts,
            status="failed",
            error_code=error_code,
        )
        outcome = "failed" if finished else "lost_lease"
        return GraphExtractionProcessResult(outcome, error_code)

    if lease_lost:
        return GraphExtractionProcessResult("lost_lease", "lost_lease")

    payload = None
    parse_error = None
    if response.finish_reason in {"length", "max_tokens"}:
        parse_status = "invalid_json"
        parse_error = "provider output was truncated"
    else:
        try:
            payload = parse_graph_extraction_output(response.content)
            parse_status = "valid"
        except GraphExtractionParseError as exc:
            parse_status = exc.parse_status
            parse_error = str(exc)

    completion = AttemptCompletion(
        request_status="succeeded",
        parse_status=parse_status,
        provider_request_id=response.provider_request_id,
        raw_response=response.raw_response,
        parsed_response=(payload.model_dump(mode="json") if payload is not None else None),
        parse_error=parse_error,
        input_token_count=response.input_token_count,
        output_token_count=response.output_token_count,
        latency_ms=response.latency_ms,
        finish_reason=response.finish_reason,
    )
    async with session_factory() as db:
        finalized = await finalize_attempt(
            db,
            attempt_id=attempt.id,
            claim_token=claim_token,
            completion=completion,
        )
    if not finalized:
        return GraphExtractionProcessResult("lost_lease", "lost_lease")
    if payload is None:
        error_code = f"parse_{parse_status}"
        finished = await _finish_claim_after_error(
            session_factory,
            unit_id=unit_id,
            claim_token=claim_token,
            max_attempts=max_attempts,
            status="failed",
            error_code=error_code,
        )
        outcome = "failed" if finished else "lost_lease"
        return GraphExtractionProcessResult(outcome, error_code)

    result = await _persist_payload_process_result(
        session_factory,
        prepared=prepared,
        payload=payload,
        max_attempts=max_attempts,
    )
    if result.outcome == "succeeded" and prepared.shadow_enabled:
        try:
            from app.services.graph_claim_shadow_worker import run_shadow_after_canonical

            await run_shadow_after_canonical(
                session_factory,
                prepared=prepared,
                provider=shadow_provider,
            )
        except Exception:  # noqa: BLE001 - shadow cannot alter canonical outcome
            from app.services.graph_claim_shadow_worker import (
                ShadowRunSummary,
                record_shadow_statistics,
            )

            await record_shadow_statistics(
                session_factory,
                job_id=prepared.job_id,
                summary=ShadowRunSummary(
                    status="failed",
                    reason="shadow_internal_error",
                ),
                attempted=1,
            )
    return result


def graph_extraction_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


async def run_graph_extraction_worker(
    *,
    watch: bool,
    metadata: dict[str, Any] | None = None,
    session_factory=async_session_factory,
    maintenance_enabled: bool = True,
) -> None:
    worker_id = graph_extraction_worker_id()
    metadata = metadata if metadata is not None else {}
    metadata.setdefault("claimed", 0)
    metadata.setdefault("succeeded", 0)
    metadata.setdefault("failed", 0)
    metadata.setdefault("cancelled", 0)
    metadata.setdefault("lost_lease", 0)
    metadata.setdefault("published", 0)
    metadata.setdefault("publication_failed", 0)
    metadata.setdefault("schema_discovery_runs", 0)
    while True:
        if maintenance_enabled and session_factory is async_session_factory:
            from app.services.schema_discovery_runs import process_next_schema_discovery_run

            discovery_run = await process_next_schema_discovery_run(
                session_factory=session_factory,
            )
            if discovery_run is not None:
                metadata["schema_discovery_runs"] += 1
                continue
        if maintenance_enabled:
            from app.services.graph_extraction_triggers import (
                compensate_ready_graph_extractions,
            )

            metadata["compensated"] = await compensate_ready_graph_extractions(
                session_factory=session_factory,
            )
            async with session_factory() as db:
                recovery = await recover_stale_graph_extraction_units(
                    db,
                    max_attempts=settings.graph_extraction_worker_max_model_attempts,
                )
            metadata["stale_recovered"] = recovery.recovered_unit_count
        from app.services.graph_extraction_batch_eval import (
            claim_eval_graph_extraction_batch,
            process_eval_graph_extraction_batch,
        )

        async with session_factory() as db:
            batch = await claim_eval_graph_extraction_batch(
                db,
                worker_id=worker_id,
                batch_size=settings.graph_extraction_batch_size,
                lease_seconds=settings.graph_extraction_unit_lease_seconds,
                max_attempts=settings.graph_extraction_worker_max_model_attempts,
                production_only=True,
            )
        if batch:
            batch_result = await process_eval_graph_extraction_batch(
                session_factory,
                units=batch,
            )
            metadata["claimed"] += len(batch)
            for outcome, count in batch_result.outcomes.items():
                metadata[outcome] += count
            materialization_job_ids = set(batch_result.ready_job_ids)
            materialization_job_ids.update(unit.job_id for unit in batch)
            if materialization_job_ids:
                from app.services.graph_extraction_auto_publication import (
                    GraphExtractionAutoPublicationError,
                    auto_publish_graph_extraction_job,
                )
                from app.services.graph_extraction_materializer import (
                    GraphExtractionMaterializationError,
                    materialize_graph_extraction_job,
                )

                for job_id in sorted(materialization_job_ids, key=str):
                    try:
                        await materialize_graph_extraction_job(
                            session_factory,
                            job_id=job_id,
                        )
                    except GraphExtractionMaterializationError as exc:
                        if exc.code != "job_not_materializable":
                            metadata["failed"] += 1
                    else:
                        try:
                            publication = await auto_publish_graph_extraction_job(
                                session_factory,
                                job_id=job_id,
                            )
                        except GraphExtractionAutoPublicationError:
                            metadata["publication_failed"] += 1
                        else:
                            metadata["published"] += int(publication.outcome == "activated")
            continue
        async with session_factory() as db:
            unit = await claim_graph_extraction_unit(
                db,
                worker_id=worker_id,
                lease_seconds=settings.graph_extraction_unit_lease_seconds,
                max_attempts=settings.graph_extraction_worker_max_model_attempts,
            )
        if unit is None:
            if not watch:
                return
            await asyncio.sleep(settings.graph_extraction_worker_poll_seconds)
            continue
        metadata["claimed"] += 1
        token = unit.claim_token
        if token is None:
            metadata["lost_lease"] += 1
            continue
        result = await process_graph_extraction_unit(
            session_factory,
            unit_id=unit.id,
            claim_token=token,
        )
        if result.ready_for_materialization or result.outcome == "failed":
            from app.services.graph_extraction_auto_publication import (
                GraphExtractionAutoPublicationError,
                auto_publish_graph_extraction_job,
            )
            from app.services.graph_extraction_materializer import (
                GraphExtractionMaterializationError,
                materialize_graph_extraction_job,
            )

            try:
                await materialize_graph_extraction_job(
                    session_factory,
                    job_id=unit.job_id,
                )
            except GraphExtractionMaterializationError as exc:
                if exc.code != "job_not_materializable":
                    result = GraphExtractionProcessResult("failed", exc.code)
            else:
                try:
                    publication = await auto_publish_graph_extraction_job(
                        session_factory,
                        job_id=unit.job_id,
                    )
                except GraphExtractionAutoPublicationError:
                    metadata["publication_failed"] += 1
                else:
                    metadata["published"] += int(publication.outcome == "activated")
        metadata[result.outcome] += 1


async def run_graph_extraction_worker_pool(
    *,
    watch: bool,
    metadata: dict[str, Any] | None = None,
    session_factory=async_session_factory,
) -> None:
    metadata = metadata if metadata is not None else {}
    concurrency = settings.graph_extraction_worker_concurrency
    metadata["worker_concurrency"] = concurrency

    async def run_resilient_loop(worker_index: int) -> None:
        while True:
            try:
                await run_graph_extraction_worker(
                    watch=watch,
                    metadata=metadata,
                    session_factory=session_factory,
                    maintenance_enabled=worker_index == 0,
                )
                return
            except (DBAPIError, OSError):
                if not watch:
                    raise
                metadata["database_reconnects"] = int(
                    metadata.get("database_reconnects", 0)
                ) + 1
                if session_factory is async_session_factory:
                    from app.db import get_engine

                    await get_engine().dispose()
                await asyncio.sleep(
                    max(1.0, settings.graph_extraction_worker_poll_seconds)
                )

    await asyncio.gather(
        *(run_resilient_loop(worker_index) for worker_index in range(concurrency))
    )
