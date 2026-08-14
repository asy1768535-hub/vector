"""Insert-only persistence for validated raw-claim decision projections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim_decision import GraphClaimDecision
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.schemas.claim_decision import (
    CLAIM_DECISION_ID_NAMESPACE,
    ClaimDecisionProjectionV1,
    deterministic_decision_id,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ClaimDecisionPersistenceError(ValueError):
    """Base error for fail-closed decision projection writes."""


class ClaimDecisionScopeError(ClaimDecisionPersistenceError):
    """The decision is not attached to the requested raw-claim scope."""


class ClaimDecisionConflictError(ClaimDecisionPersistenceError):
    """An immutable decision identity conflicts with an existing row."""


@dataclass(frozen=True, slots=True)
class ClaimDecisionWriteResult:
    decision: GraphClaimDecision
    decision_created: bool


def _same_uuid(left: Any, right: UUID) -> bool:
    return left == right or str(left) == str(right)


def _validate_production_decision_identity(
    projection: ClaimDecisionProjectionV1,
) -> ClaimDecisionProjectionV1:
    fingerprint = projection.decision_fingerprint
    if fingerprint is None:
        raise ClaimDecisionPersistenceError("decision fingerprint is missing")
    expected_id = deterministic_decision_id(CLAIM_DECISION_ID_NAMESPACE, fingerprint)
    if projection.decision_id != expected_id:
        raise ClaimDecisionPersistenceError("decision id is not deterministic for production namespace")
    return projection


def _validated_decision(payload: ClaimDecisionProjectionV1) -> ClaimDecisionProjectionV1:
    if not isinstance(payload, ClaimDecisionProjectionV1):
        raise TypeError("claim decision persistence accepts ClaimDecisionProjectionV1 only")
    validated = ClaimDecisionProjectionV1.model_validate(payload.model_dump(mode="json"))
    if _SHA256.fullmatch(validated.decision_fingerprint or "") is None:
        raise ClaimDecisionPersistenceError("invalid decision fingerprint")
    return _validate_production_decision_identity(validated)


async def _validate_claim_scope(
    db: AsyncSession, decision: ClaimDecisionProjectionV1
) -> GraphRawClaim:
    claim = await db.get(GraphRawClaim, decision.claim_id)
    if claim is None:
        raise ClaimDecisionScopeError("decision references a missing raw claim")
    if (
        not _same_uuid(claim.library_id, decision.library_id)
        or not _same_uuid(claim.document_id, decision.document_id)
        or not _same_uuid(claim.document_revision_id, decision.document_revision_id)
        or claim.revision_no != decision.revision_no
    ):
        raise ClaimDecisionScopeError("decision scope does not match the raw claim")

    if decision.extraction_occurrence_id is None:
        return claim
    occurrence = (
        await db.execute(
            select(GraphRawClaimOccurrence).where(
                GraphRawClaimOccurrence.extraction_occurrence_id
                == decision.extraction_occurrence_id
            )
        )
    ).scalar_one_or_none()
    if occurrence is None:
        raise ClaimDecisionScopeError("decision references a missing raw claim occurrence")
    if not _same_uuid(occurrence.claim_id, claim.id):
        raise ClaimDecisionScopeError("decision occurrence does not belong to the raw claim")
    return claim


def _new_decision(payload: ClaimDecisionProjectionV1) -> GraphClaimDecision:
    values = payload.model_dump(mode="python")
    return GraphClaimDecision(
        decision_id=values["decision_id"],
        decision_fingerprint=values["decision_fingerprint"],
        decision_schema_version=values["decision_schema_version"],
        decision_version=values["decision_version"],
        library_id=values["library_id"],
        document_id=values["document_id"],
        document_revision_id=values["document_revision_id"],
        revision_no=values["revision_no"],
        claim_id=values["claim_id"],
        extraction_occurrence_id=values["extraction_occurrence_id"],
        decision_kind=values["decision_kind"],
        status=values["status"],
        reason_code=values["reason_code"],
        created_by_kind=values["created_by_kind"],
        producer_key=values["producer_key"],
        producer_version=values["producer_version"],
        created_at=values["created_at"],
        proposal=values["proposal"],
    )


def _row_projection(row: GraphClaimDecision) -> ClaimDecisionProjectionV1:
    projection = ClaimDecisionProjectionV1.model_validate(
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
    return _validate_production_decision_identity(projection)


def _identity_payload(projection: ClaimDecisionProjectionV1) -> dict[str, Any]:
    payload = projection.model_dump(mode="json")
    payload.pop("created_at", None)
    return payload


def _assert_decision_matches(
    row: GraphClaimDecision, requested: ClaimDecisionProjectionV1
) -> None:
    try:
        stored = _row_projection(row)
    except Exception as exc:  # pragma: no cover - corrupt DB row guard
        raise ClaimDecisionConflictError("existing decision row is invalid") from exc
    if _identity_payload(stored) != _identity_payload(requested):
        raise ClaimDecisionConflictError("existing decision conflicts with the requested identity")


async def _find_by_identity(
    db: AsyncSession, payload: ClaimDecisionProjectionV1
) -> tuple[GraphClaimDecision | None, GraphClaimDecision | None]:
    by_id = await db.get(GraphClaimDecision, payload.decision_id)
    by_fingerprint = (
        await db.execute(
            select(GraphClaimDecision).where(
                GraphClaimDecision.library_id == payload.library_id,
                GraphClaimDecision.document_revision_id == payload.document_revision_id,
                GraphClaimDecision.decision_fingerprint == payload.decision_fingerprint,
            )
        )
    ).scalar_one_or_none()
    return by_id, by_fingerprint


def _coalesce_identity_rows(
    by_id: GraphClaimDecision | None,
    by_fingerprint: GraphClaimDecision | None,
) -> GraphClaimDecision | None:
    if by_id is not None and by_fingerprint is not None and by_id.decision_id != by_fingerprint.decision_id:
        raise ClaimDecisionConflictError("decision ID and fingerprint identify different rows")
    return by_id or by_fingerprint


async def create_or_get_claim_decision(
    db: AsyncSession,
    payload: ClaimDecisionProjectionV1,
) -> ClaimDecisionWriteResult:
    """Validate scope, then insert or reuse one immutable decision event."""
    decision = _validated_decision(payload)
    await _validate_claim_scope(db, decision)
    by_id, by_fingerprint = await _find_by_identity(db, decision)
    existing = _coalesce_identity_rows(by_id, by_fingerprint)
    if existing is not None:
        _assert_decision_matches(existing, decision)
        return ClaimDecisionWriteResult(existing, False)

    row = _new_decision(decision)
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        refreshed_by_id, refreshed_by_fingerprint = await _find_by_identity(db, decision)
        existing = _coalesce_identity_rows(refreshed_by_id, refreshed_by_fingerprint)
        if existing is None:
            raise ClaimDecisionConflictError(
                "decision insert violated an immutable constraint"
            ) from exc
        _assert_decision_matches(existing, decision)
        return ClaimDecisionWriteResult(existing, False)
    return ClaimDecisionWriteResult(row, True)


async def get_claim_decision(
    db: AsyncSession,
    *,
    library_id: UUID,
    document_revision_id: UUID,
    decision_id: UUID,
) -> ClaimDecisionProjectionV1 | None:
    row = (
        await db.execute(
            select(GraphClaimDecision).where(
                GraphClaimDecision.decision_id == decision_id,
                GraphClaimDecision.library_id == library_id,
                GraphClaimDecision.document_revision_id == document_revision_id,
            )
        )
    ).scalar_one_or_none()
    return None if row is None else _row_projection(row)


async def list_claim_decisions(
    db: AsyncSession,
    *,
    library_id: UUID,
    document_revision_id: UUID,
    claim_id: UUID | None = None,
) -> list[ClaimDecisionProjectionV1]:
    statement = select(GraphClaimDecision).where(
        GraphClaimDecision.library_id == library_id,
        GraphClaimDecision.document_revision_id == document_revision_id,
    )
    if claim_id is not None:
        statement = statement.where(GraphClaimDecision.claim_id == claim_id)
    statement = statement.order_by(GraphClaimDecision.created_at, GraphClaimDecision.decision_id)
    rows = (await db.execute(statement)).scalars().all()
    return [_row_projection(row) for row in rows]
