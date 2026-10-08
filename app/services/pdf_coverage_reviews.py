"""Append-only manual PDF coverage overlays bound to one current revision."""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision, REVISION_STATUS_READY
from app.models.document_revision_file import DocumentRevisionFile
from app.models.library import Library
from app.services import audit_log
from app.services.pdf_coverage import (
    PDF_COVERAGE_UNIT_KEY,
    PdfCoverageReportV1,
    apply_human_no_effective_content_reviews_to_report,
    pdf_coverage_from_document_blocks,
    validate_pdf_coverage_report,
)

PDF_COVERAGE_REVIEW_APPLY = "document.pdf_coverage_review.apply"
PDF_COVERAGE_REVIEW_ROLLBACK = "document.pdf_coverage_review.rollback"
PDF_COVERAGE_REVIEW_ACTIONS = (PDF_COVERAGE_REVIEW_APPLY, PDF_COVERAGE_REVIEW_ROLLBACK)
PDF_COVERAGE_REVIEW_MAX_EVENTS = 500


class PdfCoverageReviewError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _event_value(event: object, field: str) -> object:
    if isinstance(event, Mapping):
        return event.get(field)
    return getattr(event, field, None)


def _event_target(event: object) -> Mapping[str, Any]:
    target = _event_value(event, "target")
    return target if isinstance(target, Mapping) else {}


def _event_key(target: Mapping[str, Any]) -> str:
    value = target.get("idempotency_key")
    if not isinstance(value, str):
        raise PdfCoverageReviewError("coverage_review_audit_invalid")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise PdfCoverageReviewError("coverage_review_audit_invalid") from exc


def _normalize_reviews(reviews: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not reviews or len(reviews) > 100:
        raise PdfCoverageReviewError("coverage_review_batch_invalid")
    assessments: list[dict[str, Any]] = []
    for item in reviews:
        page = item.get("page")
        render_sha256 = item.get("render_sha256")
        evidence_ref = item.get("review_evidence_ref")
        if (
            isinstance(page, bool) or not isinstance(page, int) or page < 1
            or not isinstance(render_sha256, str) or len(render_sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in render_sha256)
            or not isinstance(evidence_ref, str)
            or not evidence_ref.startswith(("evidence:", "review:"))
            or "\\" in evidence_ref
            or ".." in evidence_ref.split("/")
            or not evidence_ref.endswith(f"/page-{page}")
        ):
            raise PdfCoverageReviewError("coverage_review_evidence_invalid")
        assessments.append({
            "page": page,
            "status": "no_effective_content",
            "reason": "human_review_no_effective_content",
            "render_dpi": item.get("render_dpi"),
            "render_sha256": render_sha256.lower(),
            "source_images_checked": 0,
            "checks": [],
            "classification_source": "human_review",
            "review_evidence_ref": evidence_ref,
        })
    pages = [item["page"] for item in assessments]
    if len(pages) != len(set(pages)):
        raise PdfCoverageReviewError("coverage_review_duplicate_page")
    return sorted(assessments, key=lambda item: item["page"])


def _scoped_audit_target(
    target: Mapping[str, Any], *, document_id: uuid.UUID,
    revision_id: uuid.UUID, source_sha256: str,
) -> bool:
    return (
        target.get("document_id") == str(document_id)
        and target.get("revision_id") == str(revision_id)
        and target.get("source_sha256") == source_sha256
    )


def _active_review_applications(
    audit_events: Sequence[object], *, document_id: uuid.UUID,
    revision_id: uuid.UUID, source_sha256: str,
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    applications: dict[str, list[dict[str, Any]]] = {}
    digests: dict[str, str] = {}
    rolled_back: set[str] = set()
    rollback_events: list[tuple[str, Mapping[str, Any]]] = []
    for event in audit_events:
        action = _event_value(event, "action")
        if action not in PDF_COVERAGE_REVIEW_ACTIONS:
            continue
        target = _event_target(event)
        if not _scoped_audit_target(
            target, document_id=document_id, revision_id=revision_id,
            source_sha256=source_sha256,
        ):
            if (
                target.get("document_id") == str(document_id)
                and target.get("revision_id") == str(revision_id)
            ):
                raise PdfCoverageReviewError("coverage_review_source_binding_mismatch")
            continue
        key = _event_key(target)
        if action == PDF_COVERAGE_REVIEW_APPLY:
            raw_reviews = target.get("page_reviews")
            raw_digest = target.get("review_sha256")
            if not isinstance(raw_reviews, list) or not isinstance(raw_digest, str):
                raise PdfCoverageReviewError("coverage_review_audit_invalid")
            normalized = _normalize_reviews(raw_reviews)
            digest = _canonical_sha256(normalized)
            if digest != raw_digest:
                raise PdfCoverageReviewError("coverage_review_audit_invalid")
            if key in applications and (digests[key] != digest or applications[key] != normalized):
                raise PdfCoverageReviewError("coverage_review_idempotency_conflict")
            applications[key] = normalized
            digests[key] = digest
        else:
            rollback_events.append((key, target))
    for _rollback_key, target in rollback_events:
        apply_key = target.get("apply_idempotency_key")
        if not isinstance(apply_key, str):
            raise PdfCoverageReviewError("coverage_review_audit_invalid")
        try:
            apply_key = str(uuid.UUID(apply_key))
        except ValueError as exc:
            raise PdfCoverageReviewError("coverage_review_audit_invalid") from exc
        if apply_key not in applications:
            raise PdfCoverageReviewError("coverage_review_audit_invalid")
        rolled_back.add(apply_key)
    active = {key: reviews for key, reviews in applications.items() if key not in rolled_back}
    return active, rolled_back


def project_pdf_coverage_review_audit(
    report_value: object,
    audit_events: Sequence[object], *,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    source_sha256: str,
) -> PdfCoverageReportV1:
    """Project active audit annotations over, but never mutate, a revision report."""
    report = validate_pdf_coverage_report(report_value)
    active, _rolled_back = _active_review_applications(
        audit_events, document_id=document_id, revision_id=revision_id,
        source_sha256=source_sha256.lower(),
    )
    reviews = [review for key in sorted(active) for review in active[key]]
    pages = [item["page"] for item in reviews]
    if len(pages) != len(set(pages)):
        raise PdfCoverageReviewError("coverage_review_page_conflict")
    if not reviews:
        return report
    try:
        return apply_human_no_effective_content_reviews_to_report(report, reviews)
    except ValueError as exc:
        raise PdfCoverageReviewError("coverage_review_audit_invalid") from exc


async def _load_bound_coverage_state(
    db: AsyncSession, *, library: Library, document_id: uuid.UUID,
    revision_id: uuid.UUID, source_sha256: str,
) -> tuple[Document, DocumentRevisionFile, PdfCoverageReportV1, list[AuditLog]]:
    document = (await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.library_id == library.id,
            Document.deleted_at.is_(None),
        ).with_for_update()
    )).scalars().first()
    if document is None:
        raise PdfCoverageReviewError("coverage_review_document_not_found")
    if document.current_revision_id != revision_id or document.latest_revision_id != revision_id:
        raise PdfCoverageReviewError("coverage_review_stale_revision")

    revision = (await db.execute(
        select(DocumentRevision).where(
            DocumentRevision.id == revision_id,
            DocumentRevision.document_id == document.id,
            DocumentRevision.library_id == library.id,
        ).with_for_update()
    )).scalars().first()
    if revision is None or revision.status != REVISION_STATUS_READY:
        raise PdfCoverageReviewError("coverage_review_revision_unavailable")

    revision_file = (await db.execute(
        select(DocumentRevisionFile).where(
            DocumentRevisionFile.document_revision_id == revision.id,
            DocumentRevisionFile.document_id == document.id,
            DocumentRevisionFile.library_id == library.id,
            DocumentRevisionFile.lifecycle_status == "available",
        ).with_for_update()
    )).scalars().first()
    if revision_file is None or revision_file.sha256 != source_sha256.lower():
        raise PdfCoverageReviewError("coverage_review_source_mismatch")

    blocks = (await db.execute(
        select(DocumentBlock).where(
            DocumentBlock.library_id == library.id,
            DocumentBlock.document_id == document.id,
            DocumentBlock.document_revision_id == revision.id,
            DocumentBlock.block_kind == "structured_unit",
            DocumentBlock.content["parser_unit"]["unit_key"].astext == PDF_COVERAGE_UNIT_KEY,
        ).with_for_update()
    )).scalars().all()
    if len(blocks) != 1:
        raise PdfCoverageReviewError("coverage_review_report_unavailable")
    report = pdf_coverage_from_document_blocks({"content": blocks[0].content} for _ in blocks)
    if report["status"] == "unknown":
        raise PdfCoverageReviewError("coverage_review_report_unavailable")

    audit_events = (await db.execute(
        select(AuditLog).where(
            AuditLog.action.in_(PDF_COVERAGE_REVIEW_ACTIONS),
            AuditLog.target["document_id"].astext == str(document.id),
            AuditLog.target["revision_id"].astext == str(revision.id),
        ).order_by(AuditLog.at, AuditLog.id).limit(PDF_COVERAGE_REVIEW_MAX_EVENTS + 1)
    )).scalars().all()
    if len(audit_events) > PDF_COVERAGE_REVIEW_MAX_EVENTS:
        raise PdfCoverageReviewError("coverage_review_history_limit")
    return document, revision_file, report, list(audit_events)


def _find_event(
    events: Sequence[object], *, action: str, idempotency_key: str,
) -> object | None:
    for event in events:
        if _event_value(event, "action") != action:
            continue
        if _event_key(_event_target(event)) == idempotency_key:
            return event
    return None


async def apply_pdf_coverage_review_audit(
    db: AsyncSession, *, library: Library, document_id: uuid.UUID,
    revision_id: uuid.UUID, source_sha256: str, idempotency_key: uuid.UUID,
    reviews: Sequence[Mapping[str, Any]], actor_user_id: uuid.UUID,
) -> tuple[bool, PdfCoverageReportV1]:
    normalized = _normalize_reviews(reviews)
    document, _revision_file, base_report, events = await _load_bound_coverage_state(
        db, library=library, document_id=document_id, revision_id=revision_id,
        source_sha256=source_sha256,
    )
    source_sha = source_sha256.lower()
    current = project_pdf_coverage_review_audit(
        base_report, events, document_id=document_id,
        revision_id=revision_id, source_sha256=source_sha,
    )
    event_key = str(idempotency_key)
    review_digest = _canonical_sha256(normalized)
    prior = _find_event(events, action=PDF_COVERAGE_REVIEW_APPLY, idempotency_key=event_key)
    if prior is not None:
        target = _event_target(prior)
        if target.get("review_sha256") != review_digest or target.get("page_reviews") != normalized:
            raise PdfCoverageReviewError("coverage_review_idempotency_conflict")
        return False, current

    try:
        updated = apply_human_no_effective_content_reviews_to_report(current, normalized)
    except ValueError as exc:
        message = str(exc)
        code = (
            "coverage_review_page_not_unprocessed"
            if "unprocessed" in message or "unprocessed" in message.lower()
            else "coverage_review_page_conflict"
        )
        raise PdfCoverageReviewError(code) from exc
    target = {
        "document_id": str(document.id),
        "revision_id": str(revision_id),
        "source_sha256": source_sha,
        "idempotency_key": event_key,
        "review_sha256": review_digest,
        "page_reviews": normalized,
    }
    await audit_log.record(
        db, actor_user_id, PDF_COVERAGE_REVIEW_APPLY, target,
    )
    return True, updated


async def rollback_pdf_coverage_review_audit(
    db: AsyncSession, *, library: Library, document_id: uuid.UUID,
    revision_id: uuid.UUID, source_sha256: str, idempotency_key: uuid.UUID,
    apply_idempotency_key: uuid.UUID, actor_user_id: uuid.UUID,
) -> tuple[bool, PdfCoverageReportV1]:
    document, _revision_file, base_report, events = await _load_bound_coverage_state(
        db, library=library, document_id=document_id, revision_id=revision_id,
        source_sha256=source_sha256,
    )
    source_sha = source_sha256.lower()
    current = project_pdf_coverage_review_audit(
        base_report, events, document_id=document_id,
        revision_id=revision_id, source_sha256=source_sha,
    )
    rollback_key = str(idempotency_key)
    apply_key = str(apply_idempotency_key)
    prior_rollback = _find_event(
        events, action=PDF_COVERAGE_REVIEW_ROLLBACK, idempotency_key=rollback_key,
    )
    if prior_rollback is not None:
        if _event_target(prior_rollback).get("apply_idempotency_key") != apply_key:
            raise PdfCoverageReviewError("coverage_review_idempotency_conflict")
        return False, current

    apply_event = _find_event(events, action=PDF_COVERAGE_REVIEW_APPLY, idempotency_key=apply_key)
    if apply_event is None:
        raise PdfCoverageReviewError("coverage_review_apply_event_not_found")
    if not _scoped_audit_target(
        _event_target(apply_event), document_id=document_id,
        revision_id=revision_id, source_sha256=source_sha,
    ):
        raise PdfCoverageReviewError("coverage_review_source_binding_mismatch")
    active, rolled_back = _active_review_applications(
        events, document_id=document_id, revision_id=revision_id,
        source_sha256=source_sha,
    )
    if apply_key not in active or apply_key in rolled_back:
        return False, current

    target = {
        "document_id": str(document.id),
        "revision_id": str(revision_id),
        "source_sha256": source_sha,
        "idempotency_key": rollback_key,
        "apply_idempotency_key": apply_key,
    }
    await audit_log.record(
        db, actor_user_id, PDF_COVERAGE_REVIEW_ROLLBACK, target,
    )
    after = project_pdf_coverage_review_audit(
        base_report, [*events, {"action": PDF_COVERAGE_REVIEW_ROLLBACK, "target": target}],
        document_id=document_id, revision_id=revision_id, source_sha256=source_sha,
    )
    return True, after
