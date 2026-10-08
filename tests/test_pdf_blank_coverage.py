from __future__ import annotations

import pytest

from app.schemas.knowledge_catalog import CatalogParsingCoverageRead
from app.services.pdf_coverage import (
    PDF_COVERAGE_UNIT_KEY,
    attach_pdf_coverage_unit,
    create_pdf_coverage_report,
    pdf_coverage_from_document_blocks,
    validate_pdf_coverage_report,
)

HASH = "a" * 64

CHECKS = [
    "native_text_empty",
    "ocr_empty",
    "full_page_uniform",
    "source_images_uniform",
    "paint_operations_safe",
]


def assessment(page: int, *, status: str = "blank", reason: str = "uniform_source_and_render") -> dict:
    return {
        "page": page,
        "status": status,
        "reason": reason,
        "render_dpi": 150,
        "render_sha256": HASH if status == "blank" else None,
        "source_images_checked": 0,
        "checks": list(CHECKS) if status == "blank" else ["native_text_empty"],
    }


def human_no_effect_assessment(page: int, evidence_ref: str) -> dict:
    return {
        "page": page,
        "status": "no_effective_content",
        "reason": "human_review_no_effective_content",
        "render_dpi": 300,
        "render_sha256": HASH,
        "source_images_checked": 1,
        "checks": ["native_text_empty", "ocr_empty"],
        "classification_source": "human_review",
        "review_evidence_ref": evidence_ref,
    }


def legacy_report(**overrides):
    value = {
        "contract_version": "pdf-coverage-v1",
        "status": "complete",
        "total_pages": 2,
        "processed_pages": [1, 2],
        "unprocessed_visual_pages": [],
        "skipped_visual_block_count": 0,
        "reasons": [],
    }
    value.update(overrides)
    return value


def test_legacy_shape_and_blank_page_coverage_are_compatible():
    legacy = validate_pdf_coverage_report(legacy_report())
    assert set(legacy) == set(legacy_report())

    blank = assessment(1)
    report = create_pdf_coverage_report(
        status="complete", total_pages=2, processed_pages=[2],
        confirmed_blank_pages=[1], page_assessments=[blank],
    )
    assert report["status"] == "complete"
    assert report["confirmed_blank_pages"] == [1]
    assert set(report) == set(legacy_report()) | {"confirmed_blank_pages", "page_assessments"}
    blank["checks"].clear()
    assert report["page_assessments"][0]["checks"] == sorted(CHECKS)
def test_blank_evidence_is_required_and_classification_must_not_overlap():
    base = legacy_report(processed_pages=[2])
    blank = assessment(1)
    with pytest.raises(ValueError):
        validate_pdf_coverage_report({**base, "confirmed_blank_pages": [1], "page_assessments": []})
    with pytest.raises(ValueError):
        validate_pdf_coverage_report({**base, "confirmed_blank_pages": [1], "page_assessments": [assessment(2)]})
    with pytest.raises(ValueError):
        validate_pdf_coverage_report({**base, "confirmed_blank_pages": [1], "page_assessments": [assessment(1)], "unprocessed_visual_pages": [1], "skipped_visual_block_count": 1, "status": "partial", "reasons": ["visual_content_not_ingested"]})



def test_aggregation_rejects_cross_report_page_and_assessment_conflicts():
    def block(value):
        return {"content": {"parser_unit": {
            "unit_key": PDF_COVERAGE_UNIT_KEY, "unit_kind": "structured_unit",
            "source_kind": "pdf", "value": value,
        }}}

    blank_report = create_pdf_coverage_report(
        status="complete", total_pages=2, processed_pages=[1],
        confirmed_blank_pages=[2], page_assessments=[assessment(2)],
    )
    processed_report = create_pdf_coverage_report(
        status="complete", total_pages=2, processed_pages=[1, 2],
        confirmed_blank_pages=[], page_assessments=[],
    )
    assert pdf_coverage_from_document_blocks(
        [block(blank_report), block(processed_report)]
    )["status"] == "unknown"

    first = assessment(2, status="uncertain", reason="inspection_unavailable")
    second = assessment(2, status="uncertain", reason="visible_marks")
    def partial(item):
        return create_pdf_coverage_report(
            status="partial", total_pages=2, processed_pages=[1],
            unprocessed_visual_pages=[2], skipped_visual_block_count=1,
            reasons=["visual_content_without_ocr"], confirmed_blank_pages=[],
            page_assessments=[item],
        )
    assert pdf_coverage_from_document_blocks(
        [block(partial(first)), block(partial(second))]
    )["status"] == "unknown"
@pytest.mark.parametrize("mutate", [
    lambda v: v.update(unknown=True),
    lambda v: v.update(total_pages=True),
    lambda v: v.update(confirmed_blank_pages=[True]),
    lambda v: v.update(confirmed_blank_pages=[3]),
    lambda v: v.update(page_assessments=[{**assessment(1), "render_dpi": True}]),
    lambda v: v.update(page_assessments=[{**assessment(1), "extra": 1}]),
    lambda v: v.update(page_assessments=[assessment(1, status="failed", reason="inspection_unavailable")]),
])
def test_invalid_or_unverified_blank_claims_are_rejected(mutate):
    value = legacy_report(processed_pages=[2])
    value.update(confirmed_blank_pages=[1], page_assessments=[assessment(1)])
    mutate(value)
    with pytest.raises(ValueError):
        validate_pdf_coverage_report(value)


def test_partial_uncertain_and_failed_reports_and_aggregation_conflicts_fail_closed():
    uncertain = assessment(2, status="uncertain", reason="inspection_unavailable")
    partial = create_pdf_coverage_report(
        status="partial", total_pages=2, processed_pages=[1],
        unprocessed_visual_pages=[2], skipped_visual_block_count=1,
        reasons=["visual_content_without_ocr"], page_assessments=[uncertain],
        confirmed_blank_pages=[],
    )
    assert partial["status"] == "partial"

    failed = assessment(2, status="failed", reason="render_failed")
    report = create_pdf_coverage_report(
        status="partial", total_pages=2, processed_pages=[1],
        unprocessed_visual_pages=[2], skipped_visual_block_count=1,
        reasons=["visual_content_without_ocr"], page_assessments=[failed],
        confirmed_blank_pages=[],
    )
    def block(value):
        return {"content": {"parser_unit": {
            "unit_key": PDF_COVERAGE_UNIT_KEY, "unit_kind": "structured_unit",
            "source_kind": "pdf", "value": value,
        }}}
    assert pdf_coverage_from_document_blocks([block(report), block(report)]) == report
    conflict = create_pdf_coverage_report(status="complete", total_pages=2, processed_pages=[1, 2])
    assert pdf_coverage_from_document_blocks([block(conflict), block(report)])["status"] == "unknown"


def test_schema_accepts_old_and_new_coverage_and_attach_round_trips_blank_only_report():
    old = CatalogParsingCoverageRead.model_validate(legacy_report())
    assert old.status == "complete"
    blank_report = create_pdf_coverage_report(
        status="complete", total_pages=1, confirmed_blank_pages=[1],
        page_assessments=[assessment(1)],
    )
    projected = CatalogParsingCoverageRead.model_validate(blank_report)
    assert projected.confirmed_blank_pages == [1]
    segments = [{"parser_unit": {"parser": {"name": "fixture"}, "unit_key": "root"}, "structured_units": []}]
    attach_pdf_coverage_unit(segments, blank_report)
    coverage = segments[0]["structured_units"][0]
    assert coverage["text"] == ""
    assert coverage["value"] == blank_report


def test_human_reviewed_no_effective_page_counts_as_covered_without_text_or_chunk():
    ignored = human_no_effect_assessment(2, "review:bound-pdf/page-2")
    report = create_pdf_coverage_report(
        status="complete", total_pages=2, processed_pages=[1],
        confirmed_blank_pages=[], no_effective_content_pages=[2],
        page_assessments=[ignored],
    )
    assert report["status"] == "complete"
    assert report["confirmed_blank_pages"] == []
    assert report["no_effective_content_pages"] == [2]
    assert report["page_assessments"][0]["classification_source"] == "human_review"
    assert report["page_assessments"][0]["review_evidence_ref"] == "review:bound-pdf/page-2"
    projected = CatalogParsingCoverageRead.model_validate(report)
    assert projected.no_effective_content_pages == [2]

    segments = [{"parser_unit": {"parser": {"name": "fixture"}, "unit_key": "root"},
                 "structured_units": []}]
    attach_pdf_coverage_unit(segments, report)
    coverage_unit = segments[0]["structured_units"][0]
    assert coverage_unit["text"] == ""
    assert coverage_unit["value"] == report
    assert report["processed_pages"] == [1]


def test_no_effective_content_requires_human_review_provenance():
    ignored = human_no_effect_assessment(2, "review:bound-pdf/page-2")
    base = legacy_report(total_pages=2, status="complete", processed_pages=[1])
    base.update(
        confirmed_blank_pages=[], no_effective_content_pages=[2],
        page_assessments=[ignored],
    )
    assert validate_pdf_coverage_report(base)["no_effective_content_pages"] == [2]

    automatic = {**ignored, "classification_source": "automatic", "review_evidence_ref": None}
    base["page_assessments"] = [automatic]
    with pytest.raises(ValueError):
        validate_pdf_coverage_report(base)

    missing_ref = {**ignored, "review_evidence_ref": None}
    base["page_assessments"] = [missing_ref]
    with pytest.raises(ValueError):
        validate_pdf_coverage_report(base)


def test_existing_automatic_reports_default_to_automatic_source():
    report = create_pdf_coverage_report(
        status="complete", total_pages=1, confirmed_blank_pages=[1],
        page_assessments=[assessment(1)],
    )
    page = report["page_assessments"][0]
    assert page["classification_source"] == "automatic"
    assert page["review_evidence_ref"] is None


def test_no_effective_page_survives_block_aggregation_and_conflicts_fail_closed():
    ignored = human_no_effect_assessment(2, "review:bound-pdf/page-2")
    report = create_pdf_coverage_report(
        status="complete", total_pages=2, processed_pages=[1],
        no_effective_content_pages=[2], page_assessments=[ignored],
    )

    def block(value):
        return {"content": {"parser_unit": {
            "unit_key": PDF_COVERAGE_UNIT_KEY, "unit_kind": "structured_unit",
            "source_kind": "pdf", "value": value,
        }}}

    assert pdf_coverage_from_document_blocks([block(report), block(report)]) == report
    conflicting = create_pdf_coverage_report(
        status="complete", total_pages=2, processed_pages=[1, 2],
    )
    assert pdf_coverage_from_document_blocks([block(report), block(conflicting)])[
        "status"
    ] == "unknown"
