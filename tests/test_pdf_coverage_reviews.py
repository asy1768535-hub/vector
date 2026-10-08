from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.audit import AuditLog
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.library import Library
from app.schemas.knowledge_catalog import (
    CatalogPdfCoverageReviewApplyRequest,
    CatalogPdfPageReviewInput,
)
from app.services import pdf_coverage_reviews as reviews_service
from app.services.pdf_coverage import create_pdf_coverage_report
from app.services.pdf_coverage_reviews import (
    PDF_COVERAGE_REVIEW_APPLY,
    PDF_COVERAGE_REVIEW_ROLLBACK,
    PdfCoverageReviewError,
    _canonical_sha256,
    apply_pdf_coverage_review_audit,
    project_pdf_coverage_review_audit,
    rollback_pdf_coverage_review_audit,
)


NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
SOURCE_SHA = "a" * 64
PNG_SHA = "b" * 64


def review(page: int, *, png_sha: str = PNG_SHA) -> dict:
    return {
        "page": page,
        "status": "no_effective_content",
        "reason": "human_review_no_effective_content",
        "render_dpi": 300,
        "render_sha256": png_sha,
        "source_images_checked": 0,
        "checks": [],
        "classification_source": "human_review",
        "review_evidence_ref": f"evidence:bound-source/page-{page}",
    }


def base_report():
    return create_pdf_coverage_report(
        status="partial", total_pages=4, processed_pages=[1, 2],
        unprocessed_visual_pages=[3, 4], skipped_visual_block_count=2,
        reasons=["visual_content_without_ocr"],
    )


def apply_event(document_id, revision_id, key, page_reviews, *, source_sha=SOURCE_SHA):
    return SimpleNamespace(
        action=PDF_COVERAGE_REVIEW_APPLY,
        target={
            "document_id": str(document_id),
            "revision_id": str(revision_id),
            "source_sha256": source_sha,
            "idempotency_key": str(key),
            "review_sha256": _canonical_sha256(page_reviews),
            "page_reviews": page_reviews,
        },
        id=uuid.uuid4(), at=NOW,
    )


def test_audit_projection_applies_and_rolls_back_without_mutating_immutable_report():
    document_id, revision_id = uuid.uuid4(), uuid.uuid4()
    source = base_report()
    page_reviews = [review(3)]
    apply_key = uuid.uuid4()
    applied_event = apply_event(document_id, revision_id, apply_key, page_reviews)

    projected = project_pdf_coverage_review_audit(
        source, [applied_event], document_id=document_id,
        revision_id=revision_id, source_sha256=SOURCE_SHA,
    )

    assert source["unprocessed_visual_pages"] == [3, 4]
    assert projected["no_effective_content_pages"] == [3]
    assert projected["unprocessed_visual_pages"] == [4]
    assert projected["skipped_visual_block_count"] == 1
    assert projected["page_assessments"][0]["classification_source"] == "human_review"
    assert projected["page_assessments"][0]["checks"] == []

    rollback_event = SimpleNamespace(
        action=PDF_COVERAGE_REVIEW_ROLLBACK,
        target={
            "document_id": str(document_id),
            "revision_id": str(revision_id),
            "source_sha256": SOURCE_SHA,
            "idempotency_key": str(uuid.uuid4()),
            "apply_idempotency_key": str(apply_key),
        },
        id=uuid.uuid4(), at=NOW,
    )
    reverted = project_pdf_coverage_review_audit(
        source, [applied_event, rollback_event], document_id=document_id,
        revision_id=revision_id, source_sha256=SOURCE_SHA,
    )
    assert reverted == source


def test_partial_manual_batch_leaves_other_pages_unprocessed():
    source = create_pdf_coverage_report(
        status="partial", total_pages=60, unprocessed_visual_pages=list(range(1, 61)),
        skipped_visual_block_count=60, reasons=["visual_content_without_ocr"],
    )
    reviewed = [review(page) for page in range(1, 45)]

    document_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    revision_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    projected = project_pdf_coverage_review_audit(
        source,
        [apply_event(document_id, revision_id, uuid.uuid4(), reviewed)],
        document_id=document_id, revision_id=revision_id, source_sha256=SOURCE_SHA,
    )
    assert projected["status"] == "partial"
    assert projected["no_effective_content_pages"] == list(range(1, 45))
    assert projected["unprocessed_visual_pages"] == list(range(45, 61))
    assert projected["skipped_visual_block_count"] == 16
    assert all(
        item["classification_source"] == "human_review"
        for item in projected["page_assessments"]
    )


def test_audit_projection_fails_closed_on_wrong_source_and_duplicate_active_page():
    document_id, revision_id = uuid.uuid4(), uuid.uuid4()
    key = uuid.uuid4()
    event = apply_event(document_id, revision_id, key, [review(3)])
    with pytest.raises(PdfCoverageReviewError, match="source_binding_mismatch"):
        project_pdf_coverage_review_audit(
            base_report(), [event], document_id=document_id,
            revision_id=revision_id, source_sha256="c" * 64,
        )
    duplicate = apply_event(document_id, revision_id, uuid.uuid4(), [review(3)])
    with pytest.raises(PdfCoverageReviewError, match="page_conflict"):
        project_pdf_coverage_review_audit(
            base_report(), [event, duplicate], document_id=document_id,
            revision_id=revision_id, source_sha256=SOURCE_SHA,
        )


def test_review_request_requires_page_bound_evidence_and_unique_pages():
    item = {
        "page": 3,
        "render_dpi": 300,
        "render_sha256": PNG_SHA,
        "review_evidence_ref": "evidence:bound-source/page-3",
    }
    CatalogPdfPageReviewInput.model_validate(item)
    with pytest.raises(ValidationError):
        CatalogPdfPageReviewInput.model_validate({
            **item, "review_evidence_ref": "C:/server/private/page.png",
        })
    with pytest.raises(ValidationError):
        CatalogPdfCoverageReviewApplyRequest.model_validate({
            "revision_id": str(uuid.uuid4()),
            "source_sha256": SOURCE_SHA,
            "idempotency_key": str(uuid.uuid4()),
            "reviews": [item, item],
        })


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _DB:
    def __init__(self, document, revision, revision_file, block):
        self.document = document
        self.revision = revision
        self.revision_file = revision_file
        self.block = block
        self.events = []
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        entity = statement.column_descriptions[0].get("entity")
        if entity is Document:
            rows = [self.document]
        elif entity is DocumentRevision:
            rows = [self.revision]
        elif entity is DocumentRevisionFile:
            rows = [self.revision_file]
        elif entity is DocumentBlock:
            rows = [self.block]
        elif entity is AuditLog:
            rows = self.events
        else:
            raise AssertionError(f"unexpected query entity: {entity}")
        return _Result(rows)


def _bound_db():
    library = Library(id=uuid.uuid4(), slug="review", name="Review")
    document_id, revision_id = uuid.uuid4(), uuid.uuid4()
    document = Document(
        id=document_id, library_id=library.id, current_revision_id=revision_id,
        latest_revision_id=revision_id, status="ready", content_hash=SOURCE_SHA,
        doc_metadata={"keep": "same"},
    )
    revision = DocumentRevision(
        id=revision_id, document_id=document_id, library_id=library.id,
        revision_no=1, content_hash="d" * 64, status="ready",
    )
    revision_file = DocumentRevisionFile(
        id=uuid.uuid4(), document_revision_id=revision_id,
        document_id=document_id, library_id=library.id,
        file_name="source.pdf", storage_path="private/source.pdf",
        size_bytes=99, sha256=SOURCE_SHA, lifecycle_status="available",
    )
    report = create_pdf_coverage_report(
        status="partial", total_pages=3, processed_pages=[1],
        unprocessed_visual_pages=[2, 3], skipped_visual_block_count=2,
        reasons=["visual_content_without_ocr"],
    )
    block = DocumentBlock(
        id=uuid.uuid4(), library_id=library.id, document_id=document_id,
        document_revision_id=revision_id, seq=0, block_kind="structured_unit",
        parser_name="pdf", parser_version="1", text="",
        content={"parser_unit": {
            "unit_key": "pdf:coverage:v1", "unit_kind": "structured_unit",
            "source_kind": "pdf", "value": report,
        }},
    )
    return library, document, revision, revision_file, block, _DB(
        document, revision, revision_file, block,
    )


def test_apply_is_revision_bound_idempotent_and_uses_only_append_only_audit(monkeypatch):
    library, document, revision, revision_file, block, db = _bound_db()
    original_block = dict(block.content)
    original_metadata = dict(document.doc_metadata)
    writes = []

    async def record(session, actor_id, action, target):
        event = SimpleNamespace(action=action, target=target, id=uuid.uuid4(), at=NOW)
        session.events.append(event)
        writes.append((actor_id, action, target))

    monkeypatch.setattr(reviews_service.audit_log, "record", record)
    actor_id, apply_key = uuid.uuid4(), uuid.uuid4()

    changed, projected = asyncio.run(apply_pdf_coverage_review_audit(
        db, library=library, document_id=document.id, revision_id=revision.id,
        source_sha256=revision_file.sha256, idempotency_key=apply_key,
        reviews=[{
            "page": 2, "render_dpi": 300, "render_sha256": PNG_SHA,
            "review_evidence_ref": "evidence:bound-source/page-2",
        }], actor_user_id=actor_id,
    ))
    assert changed is True
    assert projected["no_effective_content_pages"] == [2]
    assert projected["unprocessed_visual_pages"] == [3]
    assert len(writes) == 1 and writes[0][1] == PDF_COVERAGE_REVIEW_APPLY
    assert db.events[0].target["source_sha256"] == SOURCE_SHA
    assert db.events[0].target["revision_id"] == str(revision.id)
    assert block.content == original_block
    assert document.doc_metadata == original_metadata

    repeated, same_projection = asyncio.run(apply_pdf_coverage_review_audit(
        db, library=library, document_id=document.id, revision_id=revision.id,
        source_sha256=revision_file.sha256, idempotency_key=apply_key,
        reviews=[{
            "page": 2, "render_dpi": 300, "render_sha256": PNG_SHA,
            "review_evidence_ref": "evidence:bound-source/page-2",
        }], actor_user_id=actor_id,
    ))
    assert repeated is False
    assert same_projection == projected
    assert len(writes) == 1


def test_stale_revision_or_source_is_rejected_before_audit_write(monkeypatch):
    library, document, revision, revision_file, _block, db = _bound_db()
    calls = []

    async def record(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(reviews_service.audit_log, "record", record)
    document.latest_revision_id = uuid.uuid4()
    with pytest.raises(PdfCoverageReviewError, match="stale_revision"):
        asyncio.run(apply_pdf_coverage_review_audit(
            db, library=library, document_id=document.id, revision_id=revision.id,
            source_sha256=revision_file.sha256, idempotency_key=uuid.uuid4(),
            reviews=[{
                "page": 2, "render_dpi": 300, "render_sha256": PNG_SHA,
                "review_evidence_ref": "evidence:bound-source/page-2",
            }], actor_user_id=uuid.uuid4(),
        ))
    assert calls == []

    document.latest_revision_id = revision.id
    with pytest.raises(PdfCoverageReviewError, match="source_mismatch"):
        asyncio.run(apply_pdf_coverage_review_audit(
            db, library=library, document_id=document.id, revision_id=revision.id,
            source_sha256="c" * 64, idempotency_key=uuid.uuid4(),
            reviews=[{
                "page": 2, "render_dpi": 300, "render_sha256": PNG_SHA,
                "review_evidence_ref": "evidence:bound-source/page-2",
            }], actor_user_id=uuid.uuid4(),
        ))
    assert calls == []


def test_rollback_is_an_append_only_idempotent_inverse(monkeypatch):
    library, document, revision, revision_file, _block, db = _bound_db()

    async def record(session, actor_id, action, target):
        session.events.append(SimpleNamespace(action=action, target=target, id=uuid.uuid4(), at=NOW))

    monkeypatch.setattr(reviews_service.audit_log, "record", record)
    apply_key, rollback_key = uuid.uuid4(), uuid.uuid4()
    asyncio.run(apply_pdf_coverage_review_audit(
        db, library=library, document_id=document.id, revision_id=revision.id,
        source_sha256=revision_file.sha256, idempotency_key=apply_key,
        reviews=[{
            "page": 2, "render_dpi": 300, "render_sha256": PNG_SHA,
            "review_evidence_ref": "evidence:bound-source/page-2",
        }], actor_user_id=uuid.uuid4(),
    ))

    changed, report = asyncio.run(rollback_pdf_coverage_review_audit(
        db, library=library, document_id=document.id, revision_id=revision.id,
        source_sha256=revision_file.sha256, idempotency_key=rollback_key,
        apply_idempotency_key=apply_key, actor_user_id=uuid.uuid4(),
    ))
    assert changed is True
    assert report.get("no_effective_content_pages", []) == []
    assert report["unprocessed_visual_pages"] == [2, 3]
    assert [event.action for event in db.events] == [
        PDF_COVERAGE_REVIEW_APPLY, PDF_COVERAGE_REVIEW_ROLLBACK,
    ]

    repeated, same_report = asyncio.run(rollback_pdf_coverage_review_audit(
        db, library=library, document_id=document.id, revision_id=revision.id,
        source_sha256=revision_file.sha256, idempotency_key=rollback_key,
        apply_idempotency_key=apply_key, actor_user_id=uuid.uuid4(),
    ))
    assert repeated is False
    assert same_report == report
    assert len(db.events) == 2
