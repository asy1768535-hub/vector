from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import select

from app.models.chunk import Chunk
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EVIDENCE_STATUS_ACTIVE, EvidenceUnit
from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)


class EvidenceClaimError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class EvidenceClaimProtocolError(EvidenceClaimError):
    pass


class EvidenceClaimSecurityError(EvidenceClaimError):
    pass


class CandidateEvidenceReplayError(EvidenceClaimError):
    pass


@dataclass(frozen=True)
class EvidenceClaimResolution:
    quote_hash: str
    candidate_matches: list[dict[str, Any]]
    validation_status: str
    validation_error: str | None
    resolved_evidence_id: uuid.UUID | None = None
    resolved_document_id: uuid.UUID | None = None
    resolved_document_revision_id: uuid.UUID | None = None
    resolved_chunk_id: uuid.UUID | None = None
    resolved_block_id: uuid.UUID | None = None
    resolved_source_span: dict[str, Any] | None = None
    evidence_type: str | None = None
    evidence_quality_score: float | None = None


_CandidateEvidence = TypeVar(
    "_CandidateEvidence",
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)

_TABLE_EVIDENCE_KINDS = frozenset({"table_row", "sheet_row"})
_TABLE_BLOCK_KINDS = frozenset({"table", "sheet_row"})
_STRUCTURAL_EVIDENCE_KINDS = frozenset(
    {"heading", "title", "title_path", "document_metadata", "structure"}
)
_STRUCTURAL_BLOCK_KINDS = frozenset({"heading", "title", "structure"})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _quote_hash(quote: str) -> str:
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()


def _claim_key(
    *,
    candidate_id: uuid.UUID,
    extraction_unit_id: uuid.UUID,
    context_ref: str,
    quote_hash: str,
) -> str:
    payload = {
        "candidate_id": str(candidate_id),
        "extraction_unit_id": str(extraction_unit_id),
        "context_ref": context_ref,
        "quote_hash": quote_hash,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _scope_matches(value: Any, job: Any) -> bool:
    return (
        getattr(value, "library_id", None) == job.library_id
        and getattr(value, "document_id", None) == job.document_id
        and getattr(value, "document_revision_id", None) == job.document_revision_id
    )


def _raise_scope_error(message: str) -> None:
    raise EvidenceClaimSecurityError("evidence_scope_mismatch", message)


def _validate_request_scope(*, job: Any, unit: Any, snapshot: Any) -> None:
    if (
        getattr(unit, "job_id", None) != job.id
        or getattr(unit, "library_id", None) != job.library_id
        or getattr(unit, "document_revision_id", None) != job.document_revision_id
        or getattr(snapshot, "job_id", None) != job.id
        or getattr(snapshot, "extraction_unit_id", None) != unit.id
    ):
        _raise_scope_error("Job, Unit and Context Snapshot scope does not match")
    if getattr(snapshot, "purged_at", None) is not None:
        raise EvidenceClaimProtocolError(
            "context_snapshot_purged",
            "Context Snapshot was purged and cannot resolve Evidence Claims",
        )
    if not isinstance(getattr(snapshot, "context_mapping", None), dict):
        raise EvidenceClaimProtocolError(
            "invalid_context_mapping",
            "Context Snapshot mapping is missing or invalid",
        )


def _parse_uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceClaimProtocolError(
            "invalid_context_mapping",
            f"Context Snapshot {field} is not a UUID",
        ) from exc


def _mapping_scope(snapshot: Any, context_ref: str) -> tuple[uuid.UUID, list[uuid.UUID]]:
    mapping = snapshot.context_mapping.get(context_ref)
    if mapping is None:
        raise EvidenceClaimProtocolError(
            "unknown_context_ref",
            f"unknown Context reference: {context_ref}",
        )
    if not isinstance(mapping, dict) or not isinstance(mapping.get("evidence_ids"), list):
        raise EvidenceClaimProtocolError(
            "invalid_context_mapping",
            f"Context mapping for {context_ref} is invalid",
        )

    chunk_id = _parse_uuid(mapping.get("chunk_id"), field="chunk_id")
    evidence_ids: list[uuid.UUID] = []
    for raw_id in mapping["evidence_ids"]:
        evidence_id = _parse_uuid(raw_id, field="evidence_id")
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
    return chunk_id, evidence_ids


def _searchable_text(evidence: Any, revision: Any) -> str | None:
    if evidence.text_quote is not None:
        return evidence.text_quote
    normalized_text = getattr(revision, "normalized_text", None)
    start = getattr(evidence, "source_start", None)
    end = getattr(evidence, "source_end", None)
    if (
        not isinstance(normalized_text, str)
        or not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end < start
        or end > len(normalized_text)
    ):
        return None
    return normalized_text[start:end]


def _evidence_type(evidence: Any, block: Any | None) -> tuple[str, float] | None:
    evidence_kind = getattr(evidence, "evidence_kind", None)
    block_kind = getattr(block, "block_kind", None)
    if evidence_kind in _TABLE_EVIDENCE_KINDS or block_kind in _TABLE_BLOCK_KINDS:
        return "table_cell", 0.95
    if (
        evidence_kind in _STRUCTURAL_EVIDENCE_KINDS
        or block_kind in _STRUCTURAL_BLOCK_KINDS
    ):
        return None
    return "direct_statement", 1.0


def _overlapping_spans(text: str, quote: str):
    start = 0
    while True:
        found = text.find(quote, start)
        if found < 0:
            return
        yield {
            "start": found,
            "end": found + len(quote),
            "coordinate_system": "evidence_text_v1",
        }
        start = found + 1


async def resolve_evidence_claim(
    db,
    *,
    job,
    unit,
    snapshot,
    context_ref: str,
    quote: str,
) -> EvidenceClaimResolution:
    if not isinstance(context_ref, str) or not context_ref:
        raise EvidenceClaimProtocolError(
            "unknown_context_ref", "Context reference must be non-empty"
        )
    if not isinstance(quote, str) or not quote:
        raise EvidenceClaimProtocolError(
            "invalid_evidence_quote", "Evidence quote must be non-empty"
        )

    _validate_request_scope(job=job, unit=unit, snapshot=snapshot)
    chunk_id, evidence_ids = _mapping_scope(snapshot, context_ref)
    chunk = await db.get(Chunk, chunk_id)
    revision = await db.get(DocumentRevision, job.document_revision_id)
    if chunk is None or revision is None:
        raise EvidenceClaimProtocolError(
            "context_source_missing", "mapped Chunk or Document Revision is missing"
        )
    if not _scope_matches(chunk, job) or (
        revision.id != job.document_revision_id
        or revision.library_id != job.library_id
        or revision.document_id != job.document_id
    ):
        _raise_scope_error("mapped Chunk or Document Revision is outside the Job scope")

    if evidence_ids:
        evidence_result = await db.execute(
            select(EvidenceUnit).where(EvidenceUnit.id.in_(evidence_ids))
        )
        loaded_evidence = evidence_result.scalars().all()
    else:
        loaded_evidence = []

    evidence_by_id: dict[uuid.UUID, Any] = {}
    for evidence in loaded_evidence:
        if not _scope_matches(evidence, job):
            _raise_scope_error("mapped Evidence is outside the Job scope")
        if evidence.id in evidence_ids:
            evidence_by_id[evidence.id] = evidence

    missing_ids = set(evidence_ids) - set(evidence_by_id)
    if missing_ids:
        raise EvidenceClaimProtocolError(
            "context_evidence_missing", "mapped Evidence row is missing"
        )

    matches: list[dict[str, Any]] = []
    match_types: dict[tuple[str, int, int], tuple[str, float]] = {}
    for evidence_id in evidence_ids:
        evidence = evidence_by_id[evidence_id]
        if evidence.status != EVIDENCE_STATUS_ACTIVE:
            continue

        block = None
        if evidence.document_block_id is not None:
            block = await db.get(DocumentBlock, evidence.document_block_id)
            if block is None:
                raise EvidenceClaimProtocolError(
                    "context_block_missing", "Evidence source Block is missing"
                )
            if not _scope_matches(block, job):
                _raise_scope_error("Evidence source Block is outside the Job scope")

        derived_type = _evidence_type(evidence, block)
        if derived_type is None:
            continue
        searchable_text = _searchable_text(evidence, revision)
        if searchable_text is None:
            continue

        for source_span in _overlapping_spans(searchable_text, quote):
            match = {
                "evidence_id": str(evidence.id),
                "document_id": str(evidence.document_id),
                "revision_id": str(evidence.document_revision_id),
                "chunk_id": str(chunk.id),
                "block_id": (
                    str(evidence.document_block_id)
                    if evidence.document_block_id is not None
                    else None
                ),
                "source_span": source_span,
            }
            matches.append(match)
            match_types[
                (str(evidence.id), source_span["start"], source_span["end"])
            ] = derived_type

    matches.sort(
        key=lambda item: (
            item["evidence_id"],
            item["source_span"]["start"],
            item["source_span"]["end"],
        )
    )
    quote_hash = _quote_hash(quote)
    if not matches:
        return EvidenceClaimResolution(
            quote_hash=quote_hash,
            candidate_matches=[],
            validation_status="invalid",
            validation_error="quote_not_found",
        )
    if len(matches) > 1:
        return EvidenceClaimResolution(
            quote_hash=quote_hash,
            candidate_matches=matches,
            validation_status="ambiguous",
            validation_error="multiple_quote_matches",
        )

    match = matches[0]
    source_span = match["source_span"]
    evidence_type, evidence_quality_score = match_types[
        (match["evidence_id"], source_span["start"], source_span["end"])
    ]
    return EvidenceClaimResolution(
        quote_hash=quote_hash,
        candidate_matches=matches,
        validation_status="valid",
        validation_error=None,
        resolved_evidence_id=uuid.UUID(match["evidence_id"]),
        resolved_document_id=uuid.UUID(match["document_id"]),
        resolved_document_revision_id=uuid.UUID(match["revision_id"]),
        resolved_chunk_id=uuid.UUID(match["chunk_id"]),
        resolved_block_id=(
            uuid.UUID(match["block_id"]) if match["block_id"] is not None else None
        ),
        resolved_source_span=source_span,
        evidence_type=evidence_type,
        evidence_quality_score=evidence_quality_score,
    )


def _validate_candidate_scope(*, job: Any, unit: Any, candidate: Any) -> None:
    if (
        getattr(candidate, "job_id", None) != job.id
        or getattr(candidate, "library_id", None) != job.library_id
        or getattr(candidate, "ontology_version_id", None)
        != getattr(job, "ontology_version_id", None)
        or getattr(unit, "job_id", None) != job.id
    ):
        _raise_scope_error("Candidate is outside the Job scope")


async def _create_candidate_evidence(
    db,
    *,
    model: type[_CandidateEvidence],
    job: Any,
    unit: Any,
    candidate: Any,
    snapshot: Any,
    context_ref: str,
    quote: str,
) -> _CandidateEvidence:
    if not isinstance(context_ref, str) or not context_ref:
        raise EvidenceClaimProtocolError(
            "unknown_context_ref", "Context reference must be non-empty"
        )
    if not isinstance(quote, str) or not quote:
        raise EvidenceClaimProtocolError(
            "invalid_evidence_quote", "Evidence quote must be non-empty"
        )
    _validate_candidate_scope(job=job, unit=unit, candidate=candidate)
    quote_hash = _quote_hash(quote)
    claim_key = _claim_key(
        candidate_id=candidate.id,
        extraction_unit_id=unit.id,
        context_ref=context_ref,
        quote_hash=quote_hash,
    )
    existing_result = await db.execute(
        select(model).where(
            model.candidate_id == candidate.id,
            model.extraction_unit_id == unit.id,
            model.claim_key == claim_key,
        )
    )
    existing = existing_result.scalars().first()
    if existing is not None:
        if existing.purged_at is not None:
            raise CandidateEvidenceReplayError(
                "candidate_evidence_purged",
                "purged Candidate Evidence cannot be rebuilt",
            )
        if (
            existing.job_id != job.id
            or existing.candidate_id != candidate.id
            or existing.extraction_unit_id != unit.id
            or existing.context_ref != context_ref
            or existing.quote_hash != quote_hash
            or existing.quote_text != quote
        ):
            raise CandidateEvidenceReplayError(
                "candidate_evidence_replay_mismatch",
                "existing Candidate Evidence does not match the replayed Claim",
            )
        return existing

    resolution = await resolve_evidence_claim(
        db,
        job=job,
        unit=unit,
        snapshot=snapshot,
        context_ref=context_ref,
        quote=quote,
    )
    row = model(
        job_id=job.id,
        extraction_unit_id=unit.id,
        candidate_id=candidate.id,
        claim_key=claim_key,
        context_ref=context_ref,
        quote_text=quote,
        quote_hash=resolution.quote_hash,
        resolved_evidence_id=resolution.resolved_evidence_id,
        resolved_document_id=resolution.resolved_document_id,
        resolved_document_revision_id=resolution.resolved_document_revision_id,
        resolved_chunk_id=resolution.resolved_chunk_id,
        resolved_block_id=resolution.resolved_block_id,
        resolved_source_span=resolution.resolved_source_span,
        candidate_matches=resolution.candidate_matches,
        evidence_type=resolution.evidence_type,
        evidence_quality_score=resolution.evidence_quality_score,
        validation_status=resolution.validation_status,
        validation_error=resolution.validation_error,
    )
    db.add(row)
    await db.flush()
    return row


async def create_entity_candidate_evidence(
    db,
    *,
    job,
    unit,
    candidate,
    snapshot,
    context_ref: str,
    quote: str,
) -> GraphEntityCandidateEvidence:
    return await _create_candidate_evidence(
        db,
        model=GraphEntityCandidateEvidence,
        job=job,
        unit=unit,
        candidate=candidate,
        snapshot=snapshot,
        context_ref=context_ref,
        quote=quote,
    )


async def create_relation_candidate_evidence(
    db,
    *,
    job,
    unit,
    candidate,
    snapshot,
    context_ref: str,
    quote: str,
) -> GraphRelationCandidateEvidence:
    return await _create_candidate_evidence(
        db,
        model=GraphRelationCandidateEvidence,
        job=job,
        unit=unit,
        candidate=candidate,
        snapshot=snapshot,
        context_ref=context_ref,
        quote=quote,
    )
