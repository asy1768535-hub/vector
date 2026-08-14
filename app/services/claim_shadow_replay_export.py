"""Read-only, bounded PostgreSQL export for Claim Shadow Replay.

The caller owns a fresh REPEATABLE READ (or SERIALIZABLE) transaction and
must roll it back after export. This module executes SELECT statements only;
it never flushes, commits, or mutates ORM state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim_decision import GraphClaimDecision
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.schemas.claim_decision import (
    CLAIM_DECISION_ID_NAMESPACE,
    ClaimDecisionProjectionV1,
    claim_decision_fingerprint,
    deterministic_decision_id,
)
from app.schemas.raw_claim import RawClaimV1, stable_evidence_identity
from app.services.claim_shadow_replay_assembler import assemble_claim_shadow_replay_artifact


MAX_EXPORT_JOBS = 64
MAX_EXPORT_REVISIONS = 64
MAX_EXPORT_CLAIMS = 4096
MAX_EXPORT_OCCURRENCES = 4096
MAX_EXPORT_UNITS = 4096
MAX_EXPORT_DECISIONS = 4096


class ClaimShadowReplayExportError(ValueError):
    """Stable, non-sensitive failure from the read-only export boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ClaimShadowReplayExportError(code)


def _bounded_ids(value: Sequence[UUID], *, field: str, maximum: int) -> tuple[UUID, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail(f"{field}_invalid")
    values = tuple(value)
    if not values or len(values) > maximum or any(not isinstance(item, UUID) for item in values):
        _fail(f"{field}_invalid")
    if len(set(values)) != len(values):
        _fail(f"{field}_duplicate")
    return tuple(sorted(values, key=str))


@dataclass(frozen=True, slots=True)
class ClaimShadowReplayExportRequest:
    """Explicit, bounded scope for one replay export."""

    library_id: UUID
    job_ids: tuple[UUID, ...] = ()
    document_revision_ids: tuple[UUID, ...] = ()
    max_jobs: int = MAX_EXPORT_JOBS
    max_revisions: int = MAX_EXPORT_REVISIONS
    max_claims: int = MAX_EXPORT_CLAIMS
    max_occurrences: int = MAX_EXPORT_OCCURRENCES
    max_units: int = MAX_EXPORT_UNITS
    max_decisions: int = MAX_EXPORT_DECISIONS

    def __post_init__(self) -> None:
        if not isinstance(self.library_id, UUID):
            _fail("library_scope_invalid")
        jobs = _bounded_ids(self.job_ids, field="job_ids", maximum=MAX_EXPORT_JOBS) if self.job_ids else ()
        revisions = (
            _bounded_ids(
                self.document_revision_ids,
                field="document_revision_ids",
                maximum=MAX_EXPORT_REVISIONS,
            )
            if self.document_revision_ids
            else ()
        )
        if not jobs and not revisions:
            _fail("export_scope_required")
        if len(revisions) > self.max_revisions or len(jobs) > self.max_jobs:
            _fail("export_bound_invalid")
        for field in (
            "max_jobs",
            "max_revisions",
            "max_claims",
            "max_occurrences",
            "max_units",
            "max_decisions",
        ):
            value = getattr(self, field)
            maximum = {
                "max_jobs": MAX_EXPORT_JOBS,
                "max_revisions": MAX_EXPORT_REVISIONS,
                "max_claims": MAX_EXPORT_CLAIMS,
                "max_occurrences": MAX_EXPORT_OCCURRENCES,
                "max_units": MAX_EXPORT_UNITS,
                "max_decisions": MAX_EXPORT_DECISIONS,
            }[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                or value > maximum
            ):
                _fail("export_bound_invalid")
        object.__setattr__(self, "job_ids", jobs)
        object.__setattr__(self, "document_revision_ids", revisions)


@dataclass(frozen=True, slots=True)
class ClaimShadowReplayExportBundle:
    """Validated typed inputs suitable for the M4C assembler."""

    library_id: UUID
    job_ids: tuple[UUID, ...]
    document_revision_ids: tuple[UUID, ...]
    raw_claims: tuple[RawClaimV1, ...]
    decisions: tuple[ClaimDecisionProjectionV1, ...]
    state_fingerprint_before: str
    state_fingerprint_after: str
    row_counts: tuple[tuple[str, int], ...]

    @property
    def observed_claim_count(self) -> int:
        return len(self.raw_claims)

    def assemble_artifact(self, **kwargs: Any):
        """Pass only validated typed records to the DB-free M4C assembler."""
        return assemble_claim_shadow_replay_artifact(
            raw_claims=self.raw_claims,
            decisions=self.decisions,
            **kwargs,
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    return value


def _row_state(row: Any) -> dict[str, Any]:
    mapper = inspect(row).mapper
    return {
        attribute.key: _jsonable(getattr(row, attribute.key))
        for attribute in mapper.column_attrs
    }


def _state_fingerprint(groups: Mapping[str, Sequence[Any]]) -> str:
    payload = {
        table: sorted(
            (_row_state(row) for row in rows),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
        for table, rows in sorted(groups.items())
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _stable_core_evidence_payload(reference: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(reference)
    value.pop("ref_id", None)
    value.pop("job_id", None)
    value.pop("extraction_unit_id", None)
    value["evidence_identity"] = stable_evidence_identity(reference)
    return value


def _evidence_identity(reference: Any) -> str:
    if not isinstance(reference, Mapping):
        _fail("evidence_refs_invalid")
    try:
        return stable_evidence_identity(reference)
    except Exception as exc:
        raise ClaimShadowReplayExportError("evidence_refs_invalid") from exc


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _assert_core_evidence_matches(
    core: GraphRawClaim,
    references: Sequence[Mapping[str, Any]],
) -> None:
    if not isinstance(core.evidence_refs, list) or not isinstance(references, Sequence):
        _fail("evidence_refs_invalid")
    identities = [_evidence_identity(reference) for reference in references]
    if len(identities) != len(set(identities)):
        _fail("duplicate_evidence_identity")
    if any(not isinstance(item, Mapping) for item in core.evidence_refs):
        _fail("evidence_refs_invalid")
    expected = sorted(
        (_stable_core_evidence_payload(reference) for reference in references),
        key=lambda item: item["evidence_identity"],
    )
    stored = sorted(core.evidence_refs, key=lambda item: item.get("evidence_identity", ""))
    if _canonical(stored) != _canonical(expected):
        _fail("core_evidence_binding_conflict")


def _restore_core_evidence_refs(
    value: Any,
    identity_to_ref: Mapping[str, str],
) -> Any:
    if isinstance(value, Mapping):
        restored = dict(value)
        if "evidence_ref" in restored:
            identity = restored["evidence_ref"]
            if identity is None:
                return restored
            if not isinstance(identity, str) or identity not in identity_to_ref:
                _fail("core_evidence_ref_unmapped")
            restored["evidence_ref"] = identity_to_ref[identity]
        return restored
    if isinstance(value, list):
        return [_restore_core_evidence_refs(item, identity_to_ref) for item in value]
    return value


def _claim_from_rows(
    core: GraphRawClaim,
    occurrence: GraphRawClaimOccurrence,
) -> RawClaimV1:
    if not isinstance(occurrence.evidence_refs, list):
        _fail("occurrence_evidence_refs_invalid")
    _assert_core_evidence_matches(core, occurrence.evidence_refs)
    identity_to_ref = {
        _evidence_identity(reference): reference["ref_id"]
        for reference in occurrence.evidence_refs
    }
    try:
        claim = RawClaimV1.model_validate(
            {
                "claim_schema_version": core.claim_schema_version,
                "claim_id": core.id,
                "library_id": core.library_id,
                "document_id": core.document_id,
                "document_revision_id": core.document_revision_id,
                "revision_no": core.revision_no,
                "job_id": occurrence.job_id,
                "extraction_unit_id": occurrence.extraction_unit_id,
                "source_mention": _restore_core_evidence_refs(
                    core.source_mention, identity_to_ref
                ),
                "raw_predicate": core.raw_predicate,
                "target_mention": _restore_core_evidence_refs(
                    core.target_mention, identity_to_ref
                ),
                "surface_direction": core.surface_direction,
                "negation": _restore_core_evidence_refs(core.negation, identity_to_ref),
                "modality": _restore_core_evidence_refs(core.modality, identity_to_ref),
                "qualifiers": _restore_core_evidence_refs(core.qualifiers, identity_to_ref),
                "valid_time": _restore_core_evidence_refs(core.valid_time, identity_to_ref),
                "effective_time": _restore_core_evidence_refs(
                    core.effective_time, identity_to_ref
                ),
                "evidence_refs": occurrence.evidence_refs,
                "extractor_version": occurrence.extractor_version,
                "prompt_version": occurrence.prompt_version,
                "model_provider": occurrence.model_provider,
                "model_name": occurrence.model_name,
                "model_config_hash": occurrence.model_config_hash,
                "prompt_content_hash": occurrence.prompt_content_hash,
                "parser_version": occurrence.parser_version,
                "normalization_rule_version": occurrence.normalization_rule_version,
                "ontology_snapshot_hash": occurrence.ontology_snapshot_hash,
                "content_scoped_claim_fingerprint": core.content_scoped_claim_fingerprint,
                "extraction_occurrence_id": occurrence.extraction_occurrence_id,
                "extraction_occurrence_fingerprint": occurrence.extraction_occurrence_fingerprint,
            }
        )
    except Exception as exc:
        raise ClaimShadowReplayExportError("raw_claim_row_invalid") from exc
    if (
        claim.claim_id != core.id
        or claim.content_scoped_claim_fingerprint != core.content_scoped_claim_fingerprint
    ):
        _fail("raw_claim_identity_mismatch")
    if claim.extraction_occurrence_fingerprint != occurrence.extraction_occurrence_fingerprint:
        _fail("occurrence_fingerprint_mismatch")
    return claim


def _decision_from_row(row: GraphClaimDecision) -> ClaimDecisionProjectionV1:
    try:
        decision = ClaimDecisionProjectionV1.model_validate(
            {
                "decision_id": row.decision_id,
                "decision_fingerprint": row.decision_fingerprint,
                "decision_schema_version": row.decision_schema_version,
                "decision_version": row.decision_version,
                "library_id": row.library_id,
                "document_id": row.document_id,
                "document_revision_id": row.document_revision_id,
                "revision_no": row.revision_no,
                "claim_id": row.claim_id,
                "extraction_occurrence_id": row.extraction_occurrence_id,
                "decision_kind": row.decision_kind,
                "status": row.status,
                "reason_code": row.reason_code,
                "created_by_kind": row.created_by_kind,
                "producer_key": row.producer_key,
                "producer_version": row.producer_version,
                "created_at": row.created_at,
                "proposal": row.proposal,
            }
        )
    except Exception as exc:
        raise ClaimShadowReplayExportError("decision_row_invalid") from exc
    if decision.decision_fingerprint != claim_decision_fingerprint(decision):
        _fail("decision_fingerprint_mismatch")
    if decision.decision_id != deterministic_decision_id(
        CLAIM_DECISION_ID_NAMESPACE,
        decision.decision_fingerprint or "",
    ):
        _fail("decision_id_mismatch")
    return decision


async def _snapshot_is_repeatable_read(db: AsyncSession) -> None:
    if not db.in_transaction():
        _fail("snapshot_transaction_required")
    isolation = (
        await db.execute(text("SELECT current_setting('transaction_isolation')"))
    ).scalar_one()
    if isolation not in {"repeatable read", "serializable"}:
        _fail("repeatable_read_snapshot_required")


async def export_claim_shadow_replay(
    db: AsyncSession,
    request: ClaimShadowReplayExportRequest,
) -> ClaimShadowReplayExportBundle:
    """Export immutable rows from one explicit, repeatable-read snapshot."""
    if not isinstance(request, ClaimShadowReplayExportRequest):
        _fail("export_request_invalid")
    await _snapshot_is_repeatable_read(db)

    job_statement = select(GraphExtractionJob).where(
        GraphExtractionJob.library_id == request.library_id
    )
    if request.job_ids:
        job_statement = job_statement.where(GraphExtractionJob.id.in_(request.job_ids))
    if request.document_revision_ids:
        job_statement = job_statement.where(
            GraphExtractionJob.document_revision_id.in_(request.document_revision_ids)
        )
    jobs = list(
        (
            await db.execute(
                job_statement.order_by(GraphExtractionJob.id).limit(request.max_jobs + 1)
            )
        ).scalars().all()
    )
    if request.job_ids and {job.id for job in jobs} != set(request.job_ids):
        _fail("job_scope_missing_or_cross_library")
    if not jobs:
        _fail("empty_export")
    if len(jobs) > request.max_jobs:
        _fail("job_bound_exceeded")
    if any(job.status != "succeeded" for job in jobs):
        _fail("job_not_terminal_succeeded")
    job_by_id = {job.id: job for job in jobs}
    if request.document_revision_ids and {
        job.document_revision_id for job in jobs
    } != set(request.document_revision_ids):
        _fail("revision_scope_missing_or_cross_library")

    unit_rows = list(
        (
            await db.execute(
                select(GraphExtractionUnit)
                .where(GraphExtractionUnit.job_id.in_(tuple(job_by_id)))
                .order_by(GraphExtractionUnit.id)
                .limit(request.max_units + 1)
            )
        ).scalars().all()
    )
    if len(unit_rows) > request.max_units:
        _fail("unit_bound_exceeded")
    units_by_id = {unit.id: unit for unit in unit_rows}
    if len(units_by_id) != len(unit_rows):
        _fail("duplicate_unit_id")
    for unit in unit_rows:
        job = job_by_id.get(unit.job_id)
        if (
            job is None
            or unit.library_id != request.library_id
            or unit.document_revision_id != job.document_revision_id
        ):
            _fail("unit_scope_mismatch")

    occurrences = list(
        (
            await db.execute(
                select(GraphRawClaimOccurrence)
                .where(GraphRawClaimOccurrence.job_id.in_(tuple(job_by_id)))
                .order_by(GraphRawClaimOccurrence.extraction_occurrence_id)
                .limit(request.max_occurrences + 1)
            )
        ).scalars().all()
    )
    if not occurrences:
        _fail("empty_export")
    if len(occurrences) > request.max_occurrences:
        _fail("occurrence_bound_exceeded")
    if len({row.extraction_occurrence_id for row in occurrences}) != len(occurrences):
        _fail("duplicate_occurrence_id")

    claim_ids = {row.claim_id for row in occurrences}
    if len(claim_ids) > request.max_claims:
        _fail("claim_bound_exceeded")
    cores = list(
        (
            await db.execute(
                select(GraphRawClaim)
                .where(
                    GraphRawClaim.id.in_(claim_ids),
                    GraphRawClaim.library_id == request.library_id,
                )
                .order_by(GraphRawClaim.id)
            )
        ).scalars().all()
    )
    if len(cores) != len(claim_ids):
        _fail("missing_raw_claim_core")
    core_by_id = {row.id: row for row in cores}
    revision_ids = {job.document_revision_id for job in jobs}
    for core in cores:
        if core.library_id != request.library_id or core.document_revision_id not in revision_ids:
            _fail("core_scope_mismatch")

    claims: list[RawClaimV1] = []
    occurrence_by_id: dict[UUID, GraphRawClaimOccurrence] = {}
    for occurrence in occurrences:
        job = job_by_id.get(occurrence.job_id)
        unit = units_by_id.get(occurrence.extraction_unit_id)
        core = core_by_id.get(occurrence.claim_id)
        if job is None or unit is None or core is None:
            _fail("occurrence_scope_or_reference_missing")
        if (
            unit.job_id != occurrence.job_id
            or unit.library_id != request.library_id
            or unit.document_revision_id != job.document_revision_id
            or core.document_id != job.document_id
            or core.document_revision_id != job.document_revision_id
            or core.revision_no < 1
        ):
            _fail("occurrence_scope_mismatch")
        occurrence_by_id[occurrence.extraction_occurrence_id] = occurrence
        claims.append(_claim_from_rows(core, occurrence))

    decisions = list(
        (
            await db.execute(
                select(GraphClaimDecision)
                .where(
                    GraphClaimDecision.library_id == request.library_id,
                    GraphClaimDecision.document_revision_id.in_(tuple(revision_ids)),
                    GraphClaimDecision.claim_id.in_(claim_ids),
                )
                .order_by(GraphClaimDecision.decision_id)
                .limit(request.max_decisions + 1)
            )
        ).scalars().all()
    )
    if len(decisions) > request.max_decisions:
        _fail("decision_bound_exceeded")
    claim_by_id = {claim.claim_id: claim for claim in claims}
    typed_decisions: list[ClaimDecisionProjectionV1] = []
    for row in decisions:
        claim = claim_by_id.get(row.claim_id)
        if claim is None:
            _fail("decision_claim_not_exported")
        if (
            row.library_id != request.library_id
            or row.document_id != claim.document_id
            or row.document_revision_id != claim.document_revision_id
            or row.revision_no != claim.revision_no
        ):
            _fail("decision_scope_mismatch")
        if row.extraction_occurrence_id is not None:
            occurrence = occurrence_by_id.get(row.extraction_occurrence_id)
            if occurrence is None or occurrence.claim_id != row.claim_id:
                _fail("decision_occurrence_not_exported")
        typed_decisions.append(_decision_from_row(row))

    before_groups = {
        "jobs": jobs,
        "units": unit_rows,
        "occurrences": occurrences,
        "claims": cores,
        "decisions": decisions,
    }
    before = _state_fingerprint(before_groups)

    after_jobs = list(
        (
            await db.execute(
                select(GraphExtractionJob)
                .where(GraphExtractionJob.id.in_(tuple(job_by_id)))
                .order_by(GraphExtractionJob.id)
            )
        ).scalars().all()
    )
    after_units = list(
        (
            await db.execute(
                select(GraphExtractionUnit)
                .where(GraphExtractionUnit.job_id.in_(tuple(job_by_id)))
                .order_by(GraphExtractionUnit.id)
            )
        ).scalars().all()
    )
    after_occurrences = list(
        (
            await db.execute(
                select(GraphRawClaimOccurrence)
                .where(GraphRawClaimOccurrence.job_id.in_(tuple(job_by_id)))
                .order_by(GraphRawClaimOccurrence.extraction_occurrence_id)
            )
        ).scalars().all()
    )
    after_cores = list(
        (
            await db.execute(
                select(GraphRawClaim)
                .where(GraphRawClaim.id.in_(claim_ids))
                .order_by(GraphRawClaim.id)
            )
        ).scalars().all()
    )
    after_decisions = list(
        (
            await db.execute(
                select(GraphClaimDecision)
                .where(
                    GraphClaimDecision.library_id == request.library_id,
                    GraphClaimDecision.document_revision_id.in_(tuple(revision_ids)),
                    GraphClaimDecision.claim_id.in_(claim_ids),
                )
                .order_by(GraphClaimDecision.decision_id)
            )
        ).scalars().all()
    )
    after_groups = {
        "jobs": after_jobs,
        "units": after_units,
        "occurrences": after_occurrences,
        "claims": after_cores,
        "decisions": after_decisions,
    }
    after = _state_fingerprint(after_groups)
    if before != after or any(
        len(before_groups[key]) != len(after_groups[key]) for key in before_groups
    ):
        _fail("export_state_changed")

    claims.sort(
        key=lambda claim: (
            str(claim.library_id),
            claim.content_scoped_claim_fingerprint or "",
            str(claim.extraction_occurrence_id),
        )
    )
    typed_decisions.sort(
        key=lambda decision: (
            str(decision.library_id),
            decision.decision_fingerprint or "",
            str(decision.decision_id),
        )
    )
    return ClaimShadowReplayExportBundle(
        library_id=request.library_id,
        job_ids=tuple(sorted(job_by_id, key=str)),
        document_revision_ids=tuple(sorted(revision_ids, key=str)),
        raw_claims=tuple(claims),
        decisions=tuple(typed_decisions),
        state_fingerprint_before=before,
        state_fingerprint_after=after,
        row_counts=tuple(
            (key, len(before_groups[key]))
            for key in ("jobs", "units", "occurrences", "claims", "decisions")
        ),
    )
