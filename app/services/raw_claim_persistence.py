"""Insert-only persistence boundary for validated RawClaimV1 values.

The M2A storage contract is also used by the M2D2 shadow writer.  It remains
insert-only and validates every scope/evidence reference before mutation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EVIDENCE_STATUS_ACTIVE, EvidenceUnit
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.schemas.raw_claim import RawClaimV1, stable_evidence_identity
from app.services.raw_claim_projection_binding import (
    RawClaimProjectionAnchor,
    create_or_get_raw_claim_projection_binding,
)


_SENSITIVE_KEYS = frozenset(
    {
        "rawfilesha256",
        "normalizedcontenthash",
        "storagepath",
        "objectkey",
        "bucket",
        "etag",
        "quote",
        "text",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RawClaimPersistenceError(ValueError):
    """Base error for fail-closed raw claim writes."""


class RawClaimScopeError(RawClaimPersistenceError):
    """The claim does not match its immutable revision/job/unit scope."""


class RawClaimConflictError(RawClaimPersistenceError):
    """An existing immutable row conflicts with the requested identity."""


@dataclass(frozen=True, slots=True)
class RawClaimWriteResult:
    claim: GraphRawClaim
    occurrence: GraphRawClaimOccurrence
    claim_created: bool
    occurrence_created: bool
    projection_binding: Any | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _assert_safe_payload(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = _normalized_key(key)
            if normalized_key in _SENSITIVE_KEYS and not (
                normalized_key == "text"
                and isinstance(child, dict)
                and set(child).issubset({"start", "end", "ranges"})
            ):
                raise RawClaimPersistenceError(f"sensitive field is not persistable: {key}")
            _assert_safe_payload(child)
    elif isinstance(value, list):
        for child in value:
            _assert_safe_payload(child)


def _validated_claim(payload: RawClaimV1) -> RawClaimV1:
    if not isinstance(payload, RawClaimV1):
        raise TypeError("raw claim persistence accepts RawClaimV1 only")
    validated = RawClaimV1.model_validate(payload.model_dump(mode="json"))
    serialized = validated.model_dump(mode="json")
    _assert_safe_payload(serialized)
    if not _SHA256.fullmatch(validated.content_scoped_claim_fingerprint or ""):
        raise RawClaimPersistenceError("invalid content claim fingerprint")
    if not _SHA256.fullmatch(validated.extraction_occurrence_fingerprint or ""):
        raise RawClaimPersistenceError("invalid occurrence fingerprint")
    return validated


def _core_payload(claim: RawClaimV1) -> dict[str, Any]:
    payload = claim.model_dump(mode="json")
    evidence_identities: dict[str, str] = {}
    for reference in payload["evidence_refs"]:
        ref_id = reference["ref_id"]
        evidence_identities[ref_id] = stable_evidence_identity(reference)
    for field in ("source_mention", "target_mention", "negation", "modality", "valid_time", "effective_time"):
        value = payload.get(field)
        if isinstance(value, dict) and value.get("evidence_ref") is not None:
            value["evidence_ref"] = evidence_identities[value["evidence_ref"]]
    for qualifier in payload["qualifiers"]:
        if qualifier.get("evidence_ref") is not None:
            qualifier["evidence_ref"] = evidence_identities[qualifier["evidence_ref"]]
    stable_evidence_refs = []
    for reference in payload["evidence_refs"]:
        stable_reference = dict(reference)
        stable_reference.pop("ref_id")
        stable_reference.pop("job_id", None)
        stable_reference.pop("extraction_unit_id", None)
        stable_reference["evidence_identity"] = stable_evidence_identity(reference)
        stable_evidence_refs.append(stable_reference)
    stable_evidence_refs.sort(key=lambda item: item["evidence_identity"])
    return {
        "claim_schema_version": payload["claim_schema_version"],
        "library_id": payload["library_id"],
        "document_id": payload["document_id"],
        "document_revision_id": payload["document_revision_id"],
        "revision_no": payload["revision_no"],
        "content_scoped_claim_fingerprint": payload["content_scoped_claim_fingerprint"],
        "source_mention": payload["source_mention"],
        "raw_predicate": payload["raw_predicate"],
        "target_mention": payload["target_mention"],
        "surface_direction": payload["surface_direction"],
        "negation": payload["negation"],
        "modality": payload["modality"],
        "qualifiers": payload["qualifiers"],
        "valid_time": payload["valid_time"],
        "effective_time": payload["effective_time"],
        "evidence_refs": stable_evidence_refs,
    }


def _model_core_payload(row: GraphRawClaim) -> dict[str, Any]:
    return {
        "claim_schema_version": row.claim_schema_version,
        "library_id": str(row.library_id),
        "document_id": str(row.document_id),
        "document_revision_id": str(row.document_revision_id),
        "revision_no": row.revision_no,
        "content_scoped_claim_fingerprint": row.content_scoped_claim_fingerprint,
        "source_mention": row.source_mention,
        "raw_predicate": row.raw_predicate,
        "target_mention": row.target_mention,
        "surface_direction": row.surface_direction,
        "negation": row.negation,
        "modality": row.modality,
        "qualifiers": row.qualifiers,
        "valid_time": row.valid_time,
        "effective_time": row.effective_time,
        "evidence_refs": row.evidence_refs,
    }


def _assert_core_matches(row: GraphRawClaim, claim: RawClaimV1) -> None:
    if _canonical_json(_model_core_payload(row)) != _canonical_json(_core_payload(claim)):
        raise RawClaimConflictError("existing raw claim core conflicts with the requested payload")


async def _validate_scope(db: AsyncSession, claim: RawClaimV1) -> None:
    revision = await db.get(DocumentRevision, claim.document_revision_id)
    if revision is None:
        raise RawClaimScopeError("document revision does not exist")
    if (
        revision.library_id != claim.library_id
        or revision.document_id != claim.document_id
        or revision.revision_no != claim.revision_no
    ):
        raise RawClaimScopeError("claim revision scope does not match DocumentRevision")

    job = await db.get(GraphExtractionJob, claim.job_id)
    if job is None:
        raise RawClaimScopeError("extraction job does not exist")
    if (
        job.library_id != claim.library_id
        or job.document_id != claim.document_id
        or job.document_revision_id != claim.document_revision_id
    ):
        raise RawClaimScopeError("claim scope does not match GraphExtractionJob")

    unit = await db.get(GraphExtractionUnit, claim.extraction_unit_id)
    if unit is None:
        raise RawClaimScopeError("extraction unit does not exist")
    if (
        unit.job_id != claim.job_id
        or unit.library_id != claim.library_id
        or unit.document_revision_id != claim.document_revision_id
    ):
        raise RawClaimScopeError("claim scope does not match GraphExtractionUnit")


def _reference_span(reference: Any) -> tuple[int, int] | None:
    span = reference.source_span
    if span is None and reference.locator is not None and reference.locator.source is not None:
        span = reference.locator.source.text
    if span is None:
        return None
    return span.start, span.end


async def _validate_evidence_scope(db: AsyncSession, claim: RawClaimV1) -> None:
    """Verify every claim reference against existing, claimable DB evidence.

    M0 validates the internal RawClaim shape only. This boundary validates the
    external identities and hashes before either immutable claim table mutates.
    """
    references = claim.evidence_refs
    evidence_ids = {reference.evidence_id for reference in references}
    evidence_result = await db.execute(
        select(EvidenceUnit).where(EvidenceUnit.id.in_(evidence_ids))
    )
    evidence_by_id = {row.id: row for row in evidence_result.scalars().all()}
    missing_evidence = evidence_ids.difference(evidence_by_id)
    if missing_evidence:
        raise RawClaimScopeError("claim references missing EvidenceUnit")

    chunk_ids = {reference.chunk_id for reference in references if reference.chunk_id is not None}
    chunks_by_id: dict[UUID, Chunk] = {}
    if chunk_ids:
        chunk_result = await db.execute(select(Chunk).where(Chunk.id.in_(chunk_ids)))
        chunks_by_id = {row.id: row for row in chunk_result.scalars().all()}

    block_ids = {reference.block_id for reference in references if reference.block_id is not None}
    blocks_by_id: dict[UUID, DocumentBlock] = {}
    if block_ids:
        block_result = await db.execute(select(DocumentBlock).where(DocumentBlock.id.in_(block_ids)))
        blocks_by_id = {row.id: row for row in block_result.scalars().all()}

    for reference in references:
        evidence = evidence_by_id[reference.evidence_id]
        if evidence.status != EVIDENCE_STATUS_ACTIVE:
            raise RawClaimScopeError("EvidenceUnit is not claimable")
        if (
            evidence.library_id != claim.library_id
            or evidence.document_id != claim.document_id
            or evidence.document_revision_id != claim.document_revision_id
        ):
            raise RawClaimScopeError("EvidenceUnit crosses the claim scope")
        if evidence.text_quote_hash != reference.quote_sha256:
            raise RawClaimScopeError("EvidenceUnit quote hash does not match the claim")
        if reference.block_id is not None and evidence.document_block_id != reference.block_id:
            raise RawClaimScopeError("EvidenceUnit block does not match the claim")

        block = blocks_by_id.get(reference.block_id) if reference.block_id is not None else None
        if reference.block_id is not None:
            if block is None:
                raise RawClaimScopeError("claim references missing DocumentBlock")
            if (
                block.library_id != claim.library_id
                or block.document_id != claim.document_id
                or block.document_revision_id != claim.document_revision_id
            ):
                raise RawClaimScopeError("DocumentBlock crosses the claim scope")
            if reference.unit_id == reference.block_id:
                if not isinstance(block.text, str):
                    raise RawClaimScopeError("DocumentBlock text cannot verify the claim unit")
                if hashlib.sha256(block.text.encode("utf-8")).hexdigest() != reference.unit_text_sha256:
                    raise RawClaimScopeError("DocumentBlock unit text hash does not match the claim")

        reference_span = _reference_span(reference)
        if reference_span is not None:
            if evidence.source_start is None or evidence.source_end is None:
                raise RawClaimScopeError("EvidenceUnit source span cannot verify the claim")
            if (evidence.source_start, evidence.source_end) != reference_span:
                raise RawClaimScopeError("EvidenceUnit source span does not match the claim")

        if reference.chunk_id is None:
            if reference.unit_id == reference.evidence_id:
                if reference.unit_text_sha256 != reference.quote_sha256:
                    raise RawClaimScopeError(
                        "EvidenceUnit minimum unit text hash does not match the quote hash"
                    )
                if not isinstance(evidence.text_quote, str):
                    raise RawClaimScopeError("EvidenceUnit quote cannot verify the claim unit")
                if hashlib.sha256(evidence.text_quote.encode("utf-8")).hexdigest() != reference.quote_sha256:
                    raise RawClaimScopeError("EvidenceUnit quote text hash does not match the claim")
            elif reference.unit_id != reference.block_id:
                raise RawClaimScopeError("claim unit cannot be verified from EvidenceUnit or DocumentBlock")
            continue
        chunk = chunks_by_id.get(reference.chunk_id)
        if chunk is None:
            raise RawClaimScopeError("claim references missing Chunk")
        if (
            chunk.library_id != claim.library_id
            or chunk.document_id != claim.document_id
            or chunk.document_revision_id != claim.document_revision_id
        ):
            raise RawClaimScopeError("Chunk crosses the claim scope")
        if chunk.evidence_id != reference.evidence_id:
            raise RawClaimScopeError("Chunk evidence does not match the claim")
        if chunk.block_id != evidence.document_block_id:
            raise RawClaimScopeError("Chunk and EvidenceUnit blocks do not match")
        if reference.block_id is not None and chunk.block_id != reference.block_id:
            raise RawClaimScopeError("Chunk block does not match the claim")
        if not isinstance(chunk.text, str):
            raise RawClaimScopeError("Chunk text cannot verify the claim unit")
        if hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() != reference.unit_text_sha256:
            raise RawClaimScopeError("Chunk unit text hash does not match the claim")


def _new_core(claim: RawClaimV1) -> GraphRawClaim:
    values = _core_payload(claim)
    return GraphRawClaim(id=claim.claim_id, **values)


def _new_occurrence(claim: RawClaimV1, *, claim_id: UUID) -> GraphRawClaimOccurrence:
    return GraphRawClaimOccurrence(
        extraction_occurrence_id=claim.extraction_occurrence_id,
        claim_id=claim_id,
        extraction_occurrence_fingerprint=claim.extraction_occurrence_fingerprint,
        job_id=claim.job_id,
        extraction_unit_id=claim.extraction_unit_id,
        extractor_version=claim.extractor_version,
        prompt_version=claim.prompt_version,
        model_provider=claim.model_provider,
        model_name=claim.model_name,
        model_config_hash=claim.model_config_hash,
        prompt_content_hash=claim.prompt_content_hash,
        parser_version=claim.parser_version,
        normalization_rule_version=claim.normalization_rule_version,
        ontology_snapshot_hash=claim.ontology_snapshot_hash,
        evidence_refs=claim.model_dump(mode="json")["evidence_refs"],
    )


def _assert_occurrence_matches(row: GraphRawClaimOccurrence, claim: RawClaimV1, claim_id: UUID) -> None:
    values = {
        "claim_id": claim_id,
        "extraction_occurrence_id": claim.extraction_occurrence_id,
        "extraction_occurrence_fingerprint": claim.extraction_occurrence_fingerprint,
        "job_id": claim.job_id,
        "extraction_unit_id": claim.extraction_unit_id,
        "extractor_version": claim.extractor_version,
        "prompt_version": claim.prompt_version,
        "model_provider": claim.model_provider,
        "model_name": claim.model_name,
        "model_config_hash": claim.model_config_hash,
        "prompt_content_hash": claim.prompt_content_hash,
        "parser_version": claim.parser_version,
        "normalization_rule_version": claim.normalization_rule_version,
        "ontology_snapshot_hash": claim.ontology_snapshot_hash,
        "evidence_refs": claim.model_dump(mode="json")["evidence_refs"],
    }
    for field, expected in values.items():
        actual = getattr(row, field)
        if actual != expected:
            raise RawClaimConflictError(f"existing occurrence conflicts on {field}")


async def create_or_get_raw_claim(
    db: AsyncSession,
    payload: RawClaimV1,
    *,
    projection_anchor: RawClaimProjectionAnchor | None = None,
) -> RawClaimWriteResult:
    """Atomically insert or retrieve one core and one immutable occurrence."""

    claim = _validated_claim(payload)
    await _validate_scope(db, claim)
    await _validate_evidence_scope(db, claim)

    by_id = await db.get(GraphRawClaim, claim.claim_id)
    by_fingerprint = (
        await db.execute(
            select(GraphRawClaim).where(
                GraphRawClaim.library_id == claim.library_id,
                GraphRawClaim.document_revision_id == claim.document_revision_id,
                GraphRawClaim.content_scoped_claim_fingerprint
                == claim.content_scoped_claim_fingerprint,
            )
        )
    ).scalar_one_or_none()
    if by_id is not None and by_fingerprint is not None and by_id.id != by_fingerprint.id:
        raise RawClaimConflictError("claim ID and content fingerprint identify different cores")
    if by_id is not None:
        _assert_core_matches(by_id, claim)
    if by_fingerprint is not None:
        _assert_core_matches(by_fingerprint, claim)
    core = by_fingerprint or by_id
    claim_created = core is None
    if core is None:
        core = _new_core(claim)

    occurrence_by_id = (
        await db.execute(
            select(GraphRawClaimOccurrence).where(
                GraphRawClaimOccurrence.extraction_occurrence_id
                == claim.extraction_occurrence_id
            )
        )
    ).scalar_one_or_none()
    occurrence_by_fingerprint = (
        await db.execute(
            select(GraphRawClaimOccurrence).where(
                GraphRawClaimOccurrence.extraction_occurrence_fingerprint
                == claim.extraction_occurrence_fingerprint
            )
        )
    ).scalar_one_or_none()
    if (
        occurrence_by_id is not None
        and occurrence_by_fingerprint is not None
        and occurrence_by_id.id != occurrence_by_fingerprint.id
    ):
        raise RawClaimConflictError("occurrence ID and fingerprint identify different rows")
    if occurrence_by_id is not None:
        _assert_occurrence_matches(occurrence_by_id, claim, core.id)
    if occurrence_by_fingerprint is not None:
        _assert_occurrence_matches(occurrence_by_fingerprint, claim, core.id)
    occurrence = occurrence_by_id or occurrence_by_fingerprint
    occurrence_created = occurrence is None
    if occurrence is None:
        occurrence = _new_occurrence(claim, claim_id=core.id)

    try:
        async with db.begin_nested():
            if claim_created:
                db.add(core)
            if occurrence_created:
                db.add(occurrence)
            if claim_created or occurrence_created:
                await db.flush()
    except IntegrityError as exc:
        # A concurrent writer may have won a unique constraint race. The
        # savepoint is rolled back before re-reading; only a fully matching row
        # is reusable, and a missing occurrence may be retried against the now
        # committed core.
        refreshed_core_by_id = await db.get(GraphRawClaim, claim.claim_id)
        refreshed_core_by_fingerprint = (
            await db.execute(
                select(GraphRawClaim).where(
                    GraphRawClaim.library_id == claim.library_id,
                    GraphRawClaim.document_revision_id == claim.document_revision_id,
                    GraphRawClaim.content_scoped_claim_fingerprint
                    == claim.content_scoped_claim_fingerprint,
                )
            )
        ).scalar_one_or_none()
        if (
            refreshed_core_by_id is not None
            and refreshed_core_by_fingerprint is not None
            and refreshed_core_by_id.id != refreshed_core_by_fingerprint.id
        ):
            raise RawClaimConflictError(
                "claim ID and content fingerprint identify different cores"
            ) from exc
        core = refreshed_core_by_fingerprint or refreshed_core_by_id
        if core is None:
            raise RawClaimConflictError(
                "raw claim insert violated an immutable constraint"
            ) from exc
        claim_created = False
        _assert_core_matches(core, claim)

        refreshed_occurrence_by_id = (
            await db.execute(
                select(GraphRawClaimOccurrence).where(
                    GraphRawClaimOccurrence.extraction_occurrence_id
                    == claim.extraction_occurrence_id
                )
            )
        ).scalar_one_or_none()
        refreshed_occurrence_by_fingerprint = (
            await db.execute(
                select(GraphRawClaimOccurrence).where(
                    GraphRawClaimOccurrence.extraction_occurrence_fingerprint
                    == claim.extraction_occurrence_fingerprint
                )
            )
        ).scalar_one_or_none()
        if (
            refreshed_occurrence_by_id is not None
            and refreshed_occurrence_by_fingerprint is not None
            and refreshed_occurrence_by_id.id != refreshed_occurrence_by_fingerprint.id
        ):
            raise RawClaimConflictError(
                "occurrence ID and fingerprint identify different rows"
            ) from exc
        occurrence = refreshed_occurrence_by_id or refreshed_occurrence_by_fingerprint
        if occurrence is not None:
            _assert_occurrence_matches(occurrence, claim, core.id)
            occurrence_created = False
        else:
            occurrence = _new_occurrence(claim, claim_id=core.id)
            try:
                async with db.begin_nested():
                    db.add(occurrence)
                    await db.flush()
            except IntegrityError as retry_exc:
                existing_occurrence = (
                    await db.execute(
                        select(GraphRawClaimOccurrence).where(
                            GraphRawClaimOccurrence.extraction_occurrence_id
                            == claim.extraction_occurrence_id
                        )
                    )
                ).scalar_one_or_none()
                if existing_occurrence is None:
                    existing_occurrence = (
                        await db.execute(
                            select(GraphRawClaimOccurrence).where(
                                GraphRawClaimOccurrence.extraction_occurrence_fingerprint
                                == claim.extraction_occurrence_fingerprint
                            )
                        )
                    ).scalar_one_or_none()
                if existing_occurrence is None:
                    raise RawClaimConflictError(
                        "raw claim occurrence insert violated an immutable constraint"
                    ) from retry_exc
                _assert_occurrence_matches(existing_occurrence, claim, core.id)
                occurrence = existing_occurrence
                occurrence_created = False
    projection_binding = None
    if projection_anchor is not None:
        projection_binding = await create_or_get_raw_claim_projection_binding(
            db,
            claim=core,
            occurrence=occurrence,
            source_claim=claim,
            anchor=projection_anchor,
        )
    return RawClaimWriteResult(
        core,
        occurrence,
        claim_created,
        occurrence_created,
        projection_binding,
    )


async def get_raw_claim(
    db: AsyncSession,
    *,
    library_id: UUID,
    document_revision_id: UUID,
    claim_id: UUID,
) -> GraphRawClaim | None:
    return (
        await db.execute(
            select(GraphRawClaim).where(
                GraphRawClaim.id == claim_id,
                GraphRawClaim.library_id == library_id,
                GraphRawClaim.document_revision_id == document_revision_id,
            )
        )
    ).scalar_one_or_none()


async def get_raw_claim_occurrence(
    db: AsyncSession,
    *,
    library_id: UUID,
    document_revision_id: UUID,
    extraction_occurrence_id: UUID,
) -> GraphRawClaimOccurrence | None:
    return (
        await db.execute(
            select(GraphRawClaimOccurrence)
            .join(GraphRawClaim, GraphRawClaim.id == GraphRawClaimOccurrence.claim_id)
            .where(
                GraphRawClaim.library_id == library_id,
                GraphRawClaim.document_revision_id == document_revision_id,
                GraphRawClaimOccurrence.extraction_occurrence_id == extraction_occurrence_id,
            )
        )
    ).scalar_one_or_none()


async def list_raw_claims(
    db: AsyncSession,
    *,
    library_id: UUID,
    document_revision_id: UUID,
) -> list[GraphRawClaim]:
    result = await db.execute(
        select(GraphRawClaim)
        .where(
            GraphRawClaim.library_id == library_id,
            GraphRawClaim.document_revision_id == document_revision_id,
        )
        .order_by(GraphRawClaim.created_at, GraphRawClaim.id)
    )
    return list(result.scalars().all())


async def list_raw_claim_occurrences(
    db: AsyncSession,
    *,
    library_id: UUID,
    document_revision_id: UUID,
) -> list[GraphRawClaimOccurrence]:
    result = await db.execute(
        select(GraphRawClaimOccurrence)
        .join(GraphRawClaim, GraphRawClaim.id == GraphRawClaimOccurrence.claim_id)
        .where(
            GraphRawClaim.library_id == library_id,
            GraphRawClaim.document_revision_id == document_revision_id,
        )
        .order_by(GraphRawClaimOccurrence.created_at, GraphRawClaimOccurrence.id)
    )
    return list(result.scalars().all())
