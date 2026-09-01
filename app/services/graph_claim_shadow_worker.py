"""Best-effort production boundary for Phase 2 shadow claim extraction.

This module deliberately sits after canonical candidate persistence.  It owns
only the shadow request/evidence adapter, the second provider call, and the
independent raw-claim transaction; canonical worker state is never updated by
an exception from this module.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError

from app.config import settings
from app.models.chunk import Chunk
from app.models.chunk_links import ChunkEvidence
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EVIDENCE_STATUS_ACTIVE, EvidenceUnit
from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.schemas.claim_decision import (
    DecisionEndpointV1,
    MappingCandidateProposalV1,
    SchemaExtensionCandidateProposalV1,
)
from app.schemas.evidence_locator import sha256_text
from app.schemas.raw_claim import EvidenceLocatorClaimRefV1, EvidenceReferenceV1
from app.schemas.shadow_extraction import (
    ShadowEvidenceContextV1,
    ShadowExtractionLimitsV1,
    ShadowProviderResponseV1,
    ShadowTelemetryV1,
    ShadowValidationIssueV1,
)
from app.schemas.shadow_raw_response import (
    ShadowExtractionConfigV1,
    ShadowExtractionProvenanceV1,
)
from app.services.evidence_read import _verified_locator
from app.services.claim_decision_builder import (
    ClaimDecisionBuildError,
    build_claim_decision_projection,
)
from app.services.claim_decision_persistence import create_or_get_claim_decision
from app.services.graph_extraction_rate_limit import call_graph_extraction_provider
from app.services.graph_extraction_provider import GraphExtractionProviderError
from app.services.raw_claim_persistence import (
    RawClaimConflictError,
    RawClaimScopeError,
    create_or_get_raw_claim,
)
from app.services.raw_claim_shadow_builder import build_raw_claim_from_shadow
from app.services.shadow_rollout import resolve_library_shadow_extraction
from app.services.shadow_extraction_provider import (
    SHADOW_PROMPT_VERSION,
    ShadowProviderCallError,
    ShadowProviderRunError,
    build_shadow_extraction_request,
    run_shadow_extraction,
    shadow_prompt_content_hash,
)


SHADOW_EXTRACTOR_VERSION = "graph-claim-shadow-v1"
SHADOW_PARSER_VERSION = "shadow-response-parser-v2"
SHADOW_NORMALIZATION_VERSION = "surface-text-normalization-v1"
SHADOW_ID_NAMESPACE = uuid.UUID("f0a4d99e-1e78-5a9e-8d26-cd90a3f3d5e7")
DECISION_ID_NAMESPACE = uuid.UUID("6d26de8e-6a6f-5f5a-9f8c-1cb0a3a8e8e1")
DECISION_PRODUCER_KEY = "graph-claim-shadow-projector"
DECISION_PRODUCER_VERSION = "graph-claim-shadow-decision-v1"
_UNKNOWN_PREDICATE_MARKERS = frozenset(
    {"unknown", "unknown predicate", "unknown_predicate", "[unknown]", "?"}
)
_PROVIDER_ERROR_CATEGORIES = frozenset(
    {"network", "http", "auth", "rate_limit", "budget", "config", "invalid_envelope", "unknown"}
)
_PARSE_CATEGORIES = frozenset(
    {
        "json_syntax",
        "markdown_or_reasoning_wrapper",
        "schema_missing",
        "schema_extra",
        "schema_type",
        "evidence_reference",
        "bounds",
        "unknown",
    }
)
_FINAL_OUTCOMES = frozenset({"success", "failed", "timeout", "skipped"})


class ShadowAdapterError(ValueError):
    """A stable, fail-closed reason for an unavailable shadow input."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ShadowRunSummary:
    status: str
    reason: str | None = None
    claim_core_created: int = 0
    claim_core_reused: int = 0
    occurrence_created: int = 0
    occurrence_reused: int = 0
    decision_created: int = 0
    decision_reused: int = 0
    latency_ms: int | None = None
    input_token_count: int | None = None
    output_token_count: int | None = None
    provider_error_category: str | None = None
    http_status: int | None = None
    retry_count: int = 0
    final_outcome: str | None = None
    response_sha256: str | None = None
    finish_reason: str | None = None
    parse_category: str | None = None
    validation_error_count: int = 0
    validation_issues: tuple[ShadowValidationIssueV1, ...] = ()


def _stable_provider_error_category(error: GraphExtractionProviderError) -> str:
    """Map provider internals to a bounded token without persisting messages."""

    if error.category == "network_error":
        return "network"
    if error.category == "timeout":
        return "unknown"
    if error.category != "http_error":
        return "unknown"
    if error.status_code in {401, 403}:
        return "auth"
    if error.status_code == 429:
        return "rate_limit"
    if error.status_code in {402, 413}:
        return "budget"
    if "invalid response envelope" in str(error).casefold():
        return "invalid_envelope"
    return "http"


def _telemetry_summary(telemetry: Any, *, status: str, reason: str | None) -> ShadowRunSummary:
    return ShadowRunSummary(
        status=status,
        reason=reason,
        latency_ms=getattr(telemetry, "latency_ms", None),
        input_token_count=getattr(telemetry, "input_token_count", None),
        output_token_count=getattr(telemetry, "output_token_count", None),
        provider_error_category=getattr(telemetry, "provider_error_category", None),
        http_status=getattr(telemetry, "http_status", None),
        retry_count=getattr(telemetry, "retry_count", 0) or 0,
        final_outcome=getattr(telemetry, "final_outcome", None),
        response_sha256=getattr(telemetry, "response_hash", None),
        finish_reason=getattr(telemetry, "finish_reason", None),
        parse_category=getattr(telemetry, "parse_category", None),
        validation_error_count=getattr(telemetry, "validation_error_count", 0) or 0,
        validation_issues=tuple(getattr(telemetry, "validation_issues", ()) or ()),
    )


def _scope_matches(row: Any, *, job: GraphExtractionJob, unit: GraphExtractionUnit) -> bool:
    return (
        row.library_id == job.library_id
        and row.document_id == job.document_id
        and row.document_revision_id == job.document_revision_id
        and getattr(row, "job_id", job.id) == job.id
        and getattr(row, "extraction_unit_id", unit.id) == unit.id
    )


async def _load_verified_evidence(
    db,
    *,
    job: GraphExtractionJob,
    unit: GraphExtractionUnit,
    snapshot: ExtractionContextSnapshot,
    document: Document,
    revision: DocumentRevision,
) -> tuple[tuple[ShadowEvidenceContextV1, ...], dict[str, EvidenceReferenceV1]]:
    mapping = snapshot.context_mapping
    if not isinstance(mapping, dict):
        raise ShadowAdapterError("evidence_mapping_missing")

    context_specs: list[tuple[str, dict[str, Any], uuid.UUID, uuid.UUID, tuple[uuid.UUID, ...]]] = []
    evidence_ids: list[uuid.UUID] = []
    chunk_ids: list[uuid.UUID] = []
    context_json_chunks = (
        snapshot.context_json.get("chunks")
        if isinstance(snapshot.context_json, dict)
        else None
    )
    if not isinstance(context_json_chunks, list):
        raise ShadowAdapterError("evidence_context_payload_missing")
    context_json_by_ref = {
        item.get("context_ref"): item
        for item in context_json_chunks
        if isinstance(item, dict) and isinstance(item.get("context_ref"), str)
    }
    for context_ref, item in mapping.items():
        if not isinstance(item, dict):
            raise ShadowAdapterError("evidence_mapping_invalid")
        if not isinstance(context_ref, str) or not context_ref.strip():
            raise ShadowAdapterError("evidence_context_ref_invalid")
        raw_evidence_ids = item.get("evidence_ids")
        if not isinstance(raw_evidence_ids, list):
            raise ShadowAdapterError("evidence_mapping_invalid")
        parsed_evidence_ids: list[uuid.UUID] = []
        for raw_id in raw_evidence_ids:
            try:
                evidence_id = uuid.UUID(str(raw_id))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ShadowAdapterError("evidence_identity_invalid") from exc
            if evidence_id not in parsed_evidence_ids:
                parsed_evidence_ids.append(evidence_id)
        try:
            primary_evidence_id = uuid.UUID(str(item.get("primary_evidence_id")))
            chunk_id = uuid.UUID(str(item.get("chunk_id")))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ShadowAdapterError("evidence_mapping_identity_invalid") from exc
        if primary_evidence_id not in parsed_evidence_ids:
            raise ShadowAdapterError("primary_evidence_not_in_context")
        context_payload = context_json_by_ref.get(context_ref)
        if context_payload is None or context_payload.get("text") is None:
            raise ShadowAdapterError("evidence_context_not_rendered")
        context_specs.append(
            (context_ref, item, primary_evidence_id, chunk_id, tuple(parsed_evidence_ids))
        )
        if primary_evidence_id not in evidence_ids:
            evidence_ids.append(primary_evidence_id)
        if chunk_id not in chunk_ids:
            chunk_ids.append(chunk_id)
    if not context_specs:
        raise ShadowAdapterError("evidence_missing")

    evidence_result = await db.execute(
        select(EvidenceUnit).where(EvidenceUnit.id.in_(evidence_ids))
    )
    evidence_by_id = {row.id: row for row in evidence_result.scalars().all()}
    chunk_by_id: dict[uuid.UUID, Chunk] = {}
    if chunk_ids:
        chunk_result = await db.execute(select(Chunk).where(Chunk.id.in_(chunk_ids)))
        chunk_by_id = {row.id: row for row in chunk_result.scalars().all()}
    link_result = await db.execute(
        select(ChunkEvidence).where(
            ChunkEvidence.chunk_id.in_(chunk_ids),
            ChunkEvidence.document_revision_id == job.document_revision_id,
        )
    )
    links_by_chunk: dict[uuid.UUID, set[uuid.UUID]] = {}
    for link in link_result.scalars().all():
        links_by_chunk.setdefault(link.chunk_id, set()).add(link.evidence_id)

    contexts: list[ShadowEvidenceContextV1] = []
    references: dict[str, EvidenceReferenceV1] = {}
    for ref_key, _item, evidence_id, chunk_id, _context_evidence_ids in context_specs:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None or evidence.status != EVIDENCE_STATUS_ACTIVE:
            raise ShadowAdapterError("evidence_missing_or_inactive")
        if not _scope_matches(evidence, job=job, unit=unit):
            raise ShadowAdapterError("evidence_scope_mismatch")
        if not isinstance(evidence.text_quote, str) or not evidence.text_quote:
            raise ShadowAdapterError("evidence_quote_missing")
        if evidence.text_quote_hash != sha256_text(evidence.text_quote):
            raise ShadowAdapterError("evidence_quote_hash_mismatch")

        chunk = chunk_by_id.get(chunk_id)
        if chunk is None or not _scope_matches(chunk, job=job, unit=unit):
            raise ShadowAdapterError("chunk_scope_or_text_invalid")
        if not isinstance(chunk.text, str) or not chunk.text:
            raise ShadowAdapterError("chunk_text_missing")
        if context_json_by_ref[ref_key].get("text") != chunk.text:
            raise ShadowAdapterError("evidence_context_text_mismatch")
        if chunk.evidence_id != evidence.id and evidence.id not in links_by_chunk.get(chunk.id, set()):
            raise ShadowAdapterError("chunk_evidence_link_mismatch")
        locator_raw = (
            (chunk.chunk_metadata or {}).get("evidence_locator_v1")
            if isinstance(chunk.chunk_metadata, dict)
            else None
        )
        locator_unit_id = chunk.id
        locator_parent_id = chunk.block_id
        block_id = chunk.block_id
        unit_text_hash = sha256_text(chunk.text)
        if locator_raw is None:
            raise ShadowAdapterError("locator_missing")
        locator = await _verified_locator(
            db,
            locator_raw,
            document=document,
            revision=revision,
            unit_id=locator_unit_id,
            parent_unit_id=locator_parent_id,
            quote=evidence.text_quote,
            unit_text=chunk.text,
        )
        if locator is None or locator.provenance_status != "verified":
            raise ShadowAdapterError("locator_unverified")
        if locator.unit_text_sha256 != unit_text_hash:
            raise ShadowAdapterError("unit_text_hash_mismatch")
        if locator.quote_sha256 != evidence.text_quote_hash:
            raise ShadowAdapterError("quote_hash_mismatch")

        locator_ref = EvidenceLocatorClaimRefV1.from_locator(locator)
        references[ref_key] = EvidenceReferenceV1(
            ref_id=ref_key,
            evidence_id=evidence.id,
            library_id=job.library_id,
            document_id=job.document_id,
            document_revision_id=job.document_revision_id,
            revision_no=revision.revision_no,
            job_id=job.id,
            extraction_unit_id=unit.id,
            unit_id=locator_unit_id,
            chunk_id=chunk.id,
            block_id=block_id,
            quote_sha256=evidence.text_quote_hash,
            unit_text_sha256=unit_text_hash,
            source_span=locator.source.text,
            locator=locator_ref,
        )
        contexts.append(ShadowEvidenceContextV1.from_locator(ref_key, locator_ref))
    return tuple(contexts), references


def _shadow_limits_from_job(job: GraphExtractionJob) -> ShadowExtractionLimitsV1:
    snapshot_config = job.model_config_snapshot
    if not isinstance(snapshot_config, dict):
        raise ShadowAdapterError("shadow_model_budget_invalid")
    context_safety_margin = 256
    default_output_budget = 8_000
    context_window = snapshot_config.get("context_window_tokens")
    output_budget = snapshot_config.get("max_output_tokens")
    if (
        isinstance(context_window, bool)
        or not isinstance(context_window, int)
        or context_window <= 0
    ):
        raise ShadowAdapterError("shadow_model_budget_invalid")
    if output_budget is None:
        output_budget = default_output_budget
    elif (
        isinstance(output_budget, bool)
        or not isinstance(output_budget, int)
        or output_budget <= 0
    ):
        raise ShadowAdapterError("shadow_model_budget_invalid")
    context_budget = min(context_window, 32_768)
    max_context_output = context_budget - context_safety_margin - 256
    if max_context_output < 128 or (
        snapshot_config.get("max_output_tokens") is None
        and output_budget > max_context_output
    ):
        raise ShadowAdapterError("shadow_model_budget_invalid")
    output_budget = min(output_budget, 32_768, max_context_output)
    request_budget = context_budget - output_budget - context_safety_margin
    try:
        return ShadowExtractionLimitsV1(
            max_request_tokens=request_budget,
            max_output_tokens=output_budget,
        )
    except ValidationError as exc:
        raise ShadowAdapterError("shadow_model_budget_invalid") from exc


async def _build_request(
    session_factory,
    *,
    prepared: Any,
) -> tuple[Any, dict[str, EvidenceReferenceV1], ShadowExtractionProvenanceV1]:
    async with session_factory() as db:
        snapshot = await db.get(ExtractionContextSnapshot, prepared.context_snapshot_id)
        unit = await db.get(GraphExtractionUnit, prepared.unit_id)
        job = await db.get(GraphExtractionJob, prepared.job_id)
        if snapshot is None or snapshot.purged_at is not None:
            raise ShadowAdapterError("context_snapshot_unavailable")
        if unit is None or job is None or unit.job_id != job.id:
            raise ShadowAdapterError("worker_scope_missing")
        if (
            snapshot.job_id != job.id
            or
            snapshot.extraction_unit_id != unit.id
            or unit.library_id != job.library_id
            or unit.document_revision_id != job.document_revision_id
        ):
            raise ShadowAdapterError("worker_scope_mismatch")
        document = await db.get(Document, job.document_id)
        revision = await db.get(DocumentRevision, job.document_revision_id)
        if document is None or revision is None:
            raise ShadowAdapterError("revision_scope_missing")
        contexts, references = await _load_verified_evidence(
            db,
            job=job,
            unit=unit,
            snapshot=snapshot,
            document=document,
            revision=revision,
        )
        provenance = ShadowExtractionProvenanceV1(
            library_id=job.library_id,
            document_id=job.document_id,
            document_revision_id=job.document_revision_id,
            revision_no=revision.revision_no,
            job_id=job.id,
            extraction_unit_id=unit.id,
            extractor_version=SHADOW_EXTRACTOR_VERSION,
            prompt_version=SHADOW_PROMPT_VERSION,
            model_provider=job.model_provider,
            model_name=job.model_name,
            model_config_hash=job.model_config_hash,
            prompt_content_hash=shadow_prompt_content_hash(),
            parser_version=SHADOW_PARSER_VERSION,
            normalization_rule_version=SHADOW_NORMALIZATION_VERSION,
            ontology_snapshot_hash=None,
        )
        limits = _shadow_limits_from_job(job)
        request = build_shadow_extraction_request(
            unit_text=snapshot.context_text or "",
            evidence_contexts=contexts,
            provenance=provenance,
            limits=limits,
        )
        return request, references, provenance


async def _persist_claims(session_factory, claims: tuple[Any, ...]) -> ShadowRunSummary:
    core_created = core_reused = occurrence_created = occurrence_reused = 0
    async with session_factory() as db:
        async with db.begin():
            for claim in claims:
                result = await create_or_get_raw_claim(db, claim)
                core_created += int(result.claim_created)
                core_reused += int(not result.claim_created)
                occurrence_created += int(result.occurrence_created)
                occurrence_reused += int(not result.occurrence_created)
    return ShadowRunSummary(
        status="succeeded",
        claim_core_created=core_created,
        claim_core_reused=core_reused,
        occurrence_created=occurrence_created,
        occurrence_reused=occurrence_reused,
    )


def _build_decision_projections(claim: Any) -> tuple[Any, ...]:
    """Create only explicit uncertainty projections; never infer a canonical key."""
    if not hasattr(claim, "evidence_refs") or not claim.evidence_refs:
        return ()
    evidence_ref_ids = tuple(reference.ref_id for reference in claim.evidence_refs)
    proposals: list[tuple[str, str, Any]] = []
    mapping_proposal = MappingCandidateProposalV1(
        raw_predicate=claim.raw_predicate,
        source_mention=claim.source_mention,
        target_mention=claim.target_mention,
        surface_direction=claim.surface_direction,
        evidence_ref_ids=evidence_ref_ids,
        suggested_canonical_key=None,
    )
    if claim.raw_predicate.strip().casefold() in _UNKNOWN_PREDICATE_MARKERS:
        proposals.append(("mapping_candidate", "unknown_predicate", mapping_proposal))
    if claim.surface_direction == "unknown":
        proposals.append(("mapping_candidate", "unknown_direction", mapping_proposal))

    source = claim.source_mention
    target = claim.target_mention
    extension_proposal = SchemaExtensionCandidateProposalV1(
        raw_predicate=claim.raw_predicate,
        source_endpoint=DecisionEndpointV1(
            local_id=source.local_id,
            surface=source.surface,
            entity_type_hint=source.entity_type_hint,
            evidence_ref_ids=(source.evidence_ref,),
        ),
        target_endpoint=DecisionEndpointV1(
            local_id=target.local_id,
            surface=target.surface,
            entity_type_hint=target.entity_type_hint,
            evidence_ref_ids=(target.evidence_ref,),
        ),
        surface_direction=claim.surface_direction,
        evidence_ref_ids=evidence_ref_ids,
    )
    if source.entity_type_hint is None:
        proposals.append(("schema_extension_candidate", "unknown_source_type", extension_proposal))
    if target.entity_type_hint is None:
        proposals.append(("schema_extension_candidate", "unknown_target_type", extension_proposal))

    decisions: list[Any] = []
    for decision_kind, reason_code, proposal in proposals:
        try:
            decisions.append(
                build_claim_decision_projection(
                    claim,
                    decision_kind=decision_kind,
                    reason_code=reason_code,
                    proposal=proposal,
                    decision_version=1,
                    created_by_kind="system",
                    producer_key=DECISION_PRODUCER_KEY,
                    producer_version=DECISION_PRODUCER_VERSION,
                    created_at=datetime.now(UTC),
                    id_namespace=DECISION_ID_NAMESPACE,
                    extraction_occurrence_id=claim.extraction_occurrence_id,
                )
            )
        except (ClaimDecisionBuildError, TypeError, ValueError):
            continue
    return tuple(decisions)


async def _persist_decisions(session_factory, claims: tuple[Any, ...]) -> ShadowRunSummary:
    """Persist decisions after raw claims in a separate best-effort transaction."""
    decisions = tuple(
        decision
        for claim in claims
        for decision in _build_decision_projections(claim)
    )
    if not decisions:
        return ShadowRunSummary(status="succeeded")
    created = reused = 0
    try:
        async with session_factory() as db:
            async with db.begin():
                for decision in decisions:
                    result = await create_or_get_claim_decision(db, decision)
                    created += int(result.decision_created)
                    reused += int(not result.decision_created)
    except Exception:  # noqa: BLE001 - decision projection is best effort
        return ShadowRunSummary(status="succeeded", reason="decision_persistence_failed")
    return ShadowRunSummary(
        status="succeeded",
        decision_created=created,
        decision_reused=reused,
    )


async def _shadow_enabled_at_dispatch(session_factory, prepared: Any) -> bool:
    """Re-authorize the second call against the current Library row."""

    library_id = getattr(prepared, "library_id", None)
    if library_id is None:
        raise ShadowAdapterError("shadow_library_scope_missing_at_dispatch")
    async with session_factory() as db:
        library = await db.get(Library, library_id)
        if library is None:
            raise ShadowAdapterError("shadow_library_missing_at_dispatch")
        return resolve_library_shadow_extraction(library)


def _add_reason(stats: dict[str, Any], reason: str) -> None:
    reasons = stats.setdefault("reason_counts", {})
    reasons[reason] = int(reasons.get(reason, 0)) + 1


def _add_bounded_count(
    stats: dict[str, Any],
    field: str,
    value: str,
    *,
    amount: int = 1,
) -> None:
    counts = stats.setdefault(field, {})
    if not isinstance(counts, dict) or len(counts) >= 32 and value not in counts:
        return
    counts[value] = int(counts.get(value) or 0) + max(0, amount)


def _safe_finish_reason(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().casefold()
    if normalized in {"stop", "length", "max_tokens", "content_filter", "tool_calls"}:
        return normalized
    return "unknown"


async def record_shadow_statistics(
    session_factory,
    *,
    job_id: uuid.UUID,
    summary: ShadowRunSummary,
    attempted: int = 0,
) -> None:
    """Update only the bounded claim_shadow projection in a locked short tx."""

    try:
        async with session_factory() as db:
            async with db.begin():
                job = await db.get(GraphExtractionJob, job_id, with_for_update=True)
                if job is None:
                    return
                statistics = dict(job.statistics or {})
                current = dict(statistics.get("claim_shadow") or {})
                for key in (
                    "attempted",
                    "succeeded",
                    "failed",
                    "skipped",
                    "claim_core_created",
                    "claim_core_reused",
                    "occurrence_created",
                    "occurrence_reused",
                    "decision_created",
                    "decision_reused",
                ):
                    current[key] = int(current.get(key) or 0)
                for key in (
                    "provider_error_category_counts",
                    "provider_http_status_counts",
                    "provider_final_outcome_counts",
                    "parse_category_counts",
                    "finish_reason_counts",
                    "validation_error_path_type_counts",
                ):
                    if not isinstance(current.get(key), dict):
                        current[key] = {}
                if not isinstance(current.get("response_sha256s"), list):
                    current["response_sha256s"] = []
                current["retry_count_total"] = int(current.get("retry_count_total") or 0)
                current["retry_count_max"] = int(current.get("retry_count_max") or 0)
                current["validation_error_count"] = int(
                    current.get("validation_error_count") or 0
                )
                current["attempted"] += attempted
                current[summary.status] += 1
                current["claim_core_created"] += summary.claim_core_created
                current["claim_core_reused"] += summary.claim_core_reused
                current["occurrence_created"] += summary.occurrence_created
                current["occurrence_reused"] += summary.occurrence_reused
                current["decision_created"] += summary.decision_created
                current["decision_reused"] += summary.decision_reused
                if summary.reason:
                    _add_reason(current, summary.reason)
                if summary.latency_ms is not None:
                    current["latency_ms_total"] = int(current.get("latency_ms_total") or 0) + summary.latency_ms
                    current["latency_ms_count"] = int(current.get("latency_ms_count") or 0) + 1
                if summary.input_token_count is not None:
                    current["input_token_count"] = int(current.get("input_token_count") or 0) + summary.input_token_count
                if summary.output_token_count is not None:
                    current["output_token_count"] = int(current.get("output_token_count") or 0) + summary.output_token_count
                if summary.provider_error_category in _PROVIDER_ERROR_CATEGORIES:
                    _add_bounded_count(
                        current,
                        "provider_error_category_counts",
                        summary.provider_error_category,
                    )
                if summary.http_status is not None and 100 <= summary.http_status <= 599:
                    _add_bounded_count(
                        current,
                        "provider_http_status_counts",
                        str(summary.http_status),
                    )
                if summary.final_outcome in _FINAL_OUTCOMES:
                    _add_bounded_count(
                        current,
                        "provider_final_outcome_counts",
                        summary.final_outcome,
                    )
                if summary.parse_category in _PARSE_CATEGORIES:
                    _add_bounded_count(current, "parse_category_counts", summary.parse_category)
                current["validation_error_count"] += max(
                    0, summary.validation_error_count
                )
                for issue in summary.validation_issues:
                    _add_bounded_count(
                        current,
                        "validation_error_path_type_counts",
                        f"{issue.path}|{issue.error_type}",
                        amount=issue.count,
                    )
                safe_finish_reason = _safe_finish_reason(summary.finish_reason)
                if safe_finish_reason:
                    _add_bounded_count(current, "finish_reason_counts", safe_finish_reason)
                current["retry_count_total"] += max(0, summary.retry_count)
                current["retry_count_max"] = max(current["retry_count_max"], max(0, summary.retry_count))
                if summary.response_sha256 and len(summary.response_sha256) == 64 and all(
                    char in "0123456789abcdef" for char in summary.response_sha256
                ):
                    response_hashes = current["response_sha256s"]
                    if summary.response_sha256 not in response_hashes and len(response_hashes) < 64:
                        response_hashes.append(summary.response_sha256)
                statistics["claim_shadow"] = current
                job.statistics = statistics
                await db.flush()
    except Exception:  # noqa: BLE001 - metrics cannot affect canonical outcome
        return


async def run_shadow_after_canonical(
    session_factory,
    *,
    prepared: Any,
    provider: Any = None,
    timeout_seconds: float = 120.0,
) -> ShadowRunSummary:
    """Run one isolated shadow attempt after canonical success."""

    started = time.perf_counter()
    try:
        if getattr(prepared, "library_id", None) is None:
            raise ShadowAdapterError("shadow_library_scope_missing_at_dispatch")
        if not await _shadow_enabled_at_dispatch(session_factory, prepared):
            raise ShadowAdapterError("shadow_disabled_at_dispatch")
        request, evidence, provenance = await _build_request(
            session_factory,
            prepared=prepared,
        )
        active_provider = provider
        if active_provider is None:
            from app.services.graph_extraction_worker import _configured_provider

            try:
                active_provider = _configured_provider(prepared)
            except (TypeError, ValueError) as exc:
                raise ShadowProviderRunError(
                    "provider_error",
                    ShadowTelemetryV1(
                        request_hash=request.request_hash,
                        latency_ms=round((time.perf_counter() - started) * 1000),
                        error_code="provider_error",
                        provider_error_category="config",
                        final_outcome="failed",
                    ),
                ) from exc

        provider_attempts = 0

        async def call_provider(shadow_request):
            nonlocal provider_attempts
            shadow_provider = active_provider
            with_output_budget = getattr(shadow_provider, "with_output_budget", None)
            if callable(with_output_budget):
                shadow_provider = with_output_budget(
                    shadow_request.limits.max_output_tokens
                )
            try:
                async def invoke_provider():
                    nonlocal provider_attempts
                    provider_attempts += 1
                    return await shadow_provider.extract(list(shadow_request.messages))

                response = await call_graph_extraction_provider(
                    invoke_provider,
                    concurrency=settings.graph_extraction_provider_max_concurrency,
                    max_retries=settings.graph_extraction_provider_max_retries,
                    backoff_base_seconds=settings.graph_extraction_provider_backoff_base_seconds,
                )
            except GraphExtractionProviderError as exc:
                if exc.category == "timeout":
                    raise TimeoutError from exc
                raise ShadowProviderCallError(
                    _stable_provider_error_category(exc),
                    http_status=exc.status_code,
                    retry_count=max(0, provider_attempts - 1),
                ) from exc
            try:
                return ShadowProviderResponseV1(
                    content=response.content,
                    finish_reason=response.finish_reason,
                    input_token_count=response.input_token_count,
                    output_token_count=response.output_token_count,
                    latency_ms=response.latency_ms,
                    retry_count=max(0, provider_attempts - 1),
                )
            except ValidationError as exc:
                raise ShadowProviderCallError(
                    "invalid_envelope",
                    http_status=200,
                    retry_count=max(0, provider_attempts - 1),
                ) from exc

        result = await run_shadow_extraction(
            request,
            call_provider,
            config=ShadowExtractionConfigV1(global_enabled=True, library_policy="enabled"),
            timeout_seconds=timeout_seconds,
        )
        claims = tuple(
            build_raw_claim_from_shadow(
                response,
                verified_evidence=evidence,
                provenance=provenance,
                id_namespace=SHADOW_ID_NAMESPACE,
            )
            for response in result.claims
        )
        summary = await _persist_claims(session_factory, claims)
        decision_summary = await _persist_decisions(session_factory, claims)
        summary = replace(
            summary,
            decision_created=decision_summary.decision_created,
            decision_reused=decision_summary.decision_reused,
            reason=summary.reason or decision_summary.reason,
        )
        telemetry = result.telemetry
        summary = replace(
            summary,
            latency_ms=telemetry.latency_ms,
            input_token_count=telemetry.input_token_count,
            output_token_count=telemetry.output_token_count,
            retry_count=getattr(telemetry, "retry_count", 0) or 0,
            final_outcome=getattr(telemetry, "final_outcome", "success"),
            response_sha256=getattr(telemetry, "response_hash", None),
            finish_reason=getattr(telemetry, "finish_reason", None),
            parse_category=getattr(telemetry, "parse_category", None),
            validation_error_count=getattr(
                telemetry, "validation_error_count", 0
            )
            or 0,
            validation_issues=tuple(
                getattr(telemetry, "validation_issues", ()) or ()
            ),
        )
    except ShadowProviderRunError as exc:
        summary = _telemetry_summary(exc.telemetry, status="failed", reason=exc.code)
    except ShadowAdapterError as exc:
        summary = ShadowRunSummary(
            status="skipped",
            reason=exc.reason,
            final_outcome="skipped",
        )
    except (RawClaimConflictError, RawClaimScopeError):
        summary = ShadowRunSummary(
            status="failed",
            reason="persistence_rejected",
            final_outcome="failed",
        )
    except SQLAlchemyError:
        summary = ShadowRunSummary(
            status="failed",
            reason="persistence_failed",
            final_outcome="failed",
        )
    except Exception:  # noqa: BLE001 - shadow is explicitly best effort
        summary = ShadowRunSummary(
            status="failed",
            reason="shadow_internal_error",
            latency_ms=round((time.perf_counter() - started) * 1000),
            final_outcome="failed",
        )
    await record_shadow_statistics(
        session_factory,
        job_id=prepared.job_id,
        summary=summary,
        attempted=1,
    )
    return summary


async def record_shadow_skip(
    session_factory,
    *,
    job_id: uuid.UUID,
    reason: str,
) -> None:
    await record_shadow_statistics(
        session_factory,
        job_id=job_id,
        summary=ShadowRunSummary(
            status="skipped",
            reason=reason,
            final_outcome="skipped",
        ),
    )
