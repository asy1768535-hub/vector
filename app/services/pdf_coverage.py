"""Bounded PDF parsing coverage reports and parser-unit integration."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypedDict
import re

from app.services.parser_units import build_parser_unit

PDF_COVERAGE_CONTRACT_VERSION = "pdf-coverage-v1"
PDF_COVERAGE_UNIT_KEY = "pdf:coverage:v1"
PDF_COVERAGE_MAX_PAGE_NUMBER = 1_000_000
PDF_COVERAGE_MAX_PAGE_ITEMS = 5_000
PDF_COVERAGE_MAX_REASONS = 8

PdfCoverageStatus = Literal["complete", "partial", "unknown"]
PdfCoverageReason = Literal[
    "visual_content_without_ocr",
    "visual_content_not_ingested",
]
PDF_COVERAGE_REASONS = frozenset(
    {"visual_content_without_ocr", "visual_content_not_ingested"}
)
PAGE_ASSESSMENT_REASONS = frozenset({
    "uniform_source_and_render", "visible_marks", "inspection_unavailable",
    "native_text_failed", "render_failed", "ocr_failed", "resource_limit",
    "human_review_no_effective_content",
})
PAGE_ASSESSMENT_CHECKS = frozenset({
    "native_text_empty", "ocr_empty", "full_page_uniform",
    "source_images_uniform", "paint_operations_safe",
})
FAILED_ASSESSMENT_REASONS = frozenset({
    "native_text_failed", "render_failed", "ocr_failed", "resource_limit",
})


class PdfPageAssessment(TypedDict):
    page: int
    status: Literal["blank", "no_effective_content", "uncertain", "failed"]
    reason: str
    render_dpi: int
    render_sha256: str | None
    source_images_checked: int
    checks: list[str]
    classification_source: Literal["automatic", "human_review"]
    review_evidence_ref: str | None


class PdfCoverageReportV1(TypedDict, total=False):
    contract_version: Literal["pdf-coverage-v1"]
    status: PdfCoverageStatus
    total_pages: int | None
    processed_pages: list[int]
    unprocessed_visual_pages: list[int]
    skipped_visual_block_count: int
    reasons: list[PdfCoverageReason]
    confirmed_blank_pages: list[int]
    no_effective_content_pages: list[int]
    page_assessments: list[PdfPageAssessment]


def _bounded_positive_pages(value: object, *, field: str) -> list[int]:
    if not isinstance(value, list) or len(value) > PDF_COVERAGE_MAX_PAGE_ITEMS:
        raise ValueError(f"{field} must be a bounded list")
    pages: list[int] = []
    for page in value:
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or page > PDF_COVERAGE_MAX_PAGE_NUMBER
        ):
            raise ValueError(f"{field} must contain positive bounded integers")
        pages.append(page)
    if pages != sorted(set(pages)):
        raise ValueError(f"{field} must be sorted and unique")
    return pages


def _validate_page_assessments(value: object, total_pages: int | None) -> list[PdfPageAssessment]:
    if not isinstance(value, list) or len(value) > PDF_COVERAGE_MAX_PAGE_ITEMS:
        raise ValueError("page_assessments must be a bounded list")
    result: list[PdfPageAssessment] = []
    legacy_expected = {
        "page", "status", "reason", "render_dpi", "render_sha256",
        "source_images_checked", "checks",
    }
    current_expected = legacy_expected | {"classification_source", "review_evidence_ref"}
    for item in value:
        if not isinstance(item, Mapping) or set(item) not in {
            frozenset(legacy_expected), frozenset(current_expected)
        }:
            raise ValueError("page assessment fields are invalid")
        page = item["page"]
        if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > PDF_COVERAGE_MAX_PAGE_NUMBER:
            raise ValueError("page assessment page is invalid")
        if total_pages is not None and page > total_pages:
            raise ValueError("page assessment page exceeds total_pages")
        status = item["status"]
        reason = item["reason"]
        if not isinstance(status, str) or status not in {
            "blank", "no_effective_content", "uncertain", "failed"
        }:
            raise ValueError("page assessment status is invalid")
        if not isinstance(reason, str) or reason not in PAGE_ASSESSMENT_REASONS:
            raise ValueError("page assessment reason is invalid")
        dpi = item["render_dpi"]
        images = item["source_images_checked"]
        if isinstance(dpi, bool) or not isinstance(dpi, int) or not 1 <= dpi <= 10_000:
            raise ValueError("page assessment render_dpi is invalid")
        if isinstance(images, bool) or not isinstance(images, int) or not 0 <= images <= 32:
            raise ValueError("page assessment source_images_checked is invalid")
        digest = item["render_sha256"]
        if digest is not None and (not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None):
            raise ValueError("page assessment render_sha256 is invalid")
        checks = item["checks"]
        if (
            not isinstance(checks, list)
            or any(not isinstance(check, str) or check not in PAGE_ASSESSMENT_CHECKS for check in checks)
            or checks != sorted(set(checks))
        ):
            raise ValueError("page assessment checks are invalid")
        classification_source = item.get("classification_source", "automatic")
        review_evidence_ref = item.get("review_evidence_ref")
        if not isinstance(classification_source, str) or classification_source not in {
            "automatic", "human_review"
        }:
            raise ValueError("page assessment classification_source is invalid")
        if review_evidence_ref is not None and (
            not isinstance(review_evidence_ref, str)
            or not review_evidence_ref.strip()
            or len(review_evidence_ref) > 256
            or any(ord(char) < 32 for char in review_evidence_ref)
        ):
            raise ValueError("page assessment review_evidence_ref is invalid")
        if classification_source == "human_review" and review_evidence_ref is None:
            raise ValueError("human page assessment requires a review evidence reference")
        if classification_source == "automatic" and review_evidence_ref is not None:
            raise ValueError("automatic page assessment cannot cite human review evidence")
        if status == "blank":
            if (
                reason != "uniform_source_and_render"
                or set(checks) != PAGE_ASSESSMENT_CHECKS
                or digest is None
                or classification_source != "automatic"
            ):
                raise ValueError("blank page assessment lacks uniformity evidence")
        elif status == "no_effective_content":
            if (
                reason != "human_review_no_effective_content"
                or classification_source != "human_review"
                or digest is None
            ):
                raise ValueError("no-effective-content page requires bound human visual evidence")
        elif reason == "uniform_source_and_render" or digest is None and status == "blank":
            raise ValueError("non-blank page assessment classification is invalid")
        if status == "failed" and reason not in FAILED_ASSESSMENT_REASONS:
            raise ValueError("failed page assessment reason is invalid")
        result.append({
            "page": page,
            "status": status,
            "reason": reason,
            "render_dpi": dpi,
            "render_sha256": digest,
            "source_images_checked": images,
            "checks": list(checks),
            "classification_source": classification_source,
            "review_evidence_ref": review_evidence_ref,
        })
    pages = [item["page"] for item in result]
    if pages != sorted(set(pages)):
        raise ValueError("page assessments must be sorted and unique")
    return result


def validate_pdf_coverage_report(value: object) -> PdfCoverageReportV1:
    """Validate and copy a report without retaining untrusted fields."""
    if not isinstance(value, Mapping):
        raise ValueError("PDF coverage report must be an object")
    required_fields = {
        "contract_version", "status", "total_pages", "processed_pages",
        "unprocessed_visual_pages", "skipped_visual_block_count", "reasons",
    }
    extension_fields = {"confirmed_blank_pages", "no_effective_content_pages", "page_assessments"}
    fields = set(value)
    if not required_fields.issubset(fields) or fields - required_fields - extension_fields:
        raise ValueError("PDF coverage report fields are invalid")
    has_blank_pages = "confirmed_blank_pages" in fields
    has_no_effective_pages = "no_effective_content_pages" in fields
    has_assessments = "page_assessments" in fields
    if has_blank_pages != has_assessments:
        raise ValueError("blank page extension fields must be provided together")
    if has_no_effective_pages and not has_assessments:
        raise ValueError("no-effective-content pages require page assessments")
    if value.get("contract_version") != PDF_COVERAGE_CONTRACT_VERSION:
        raise ValueError("PDF coverage report version is invalid")

    status = value.get("status")
    if not isinstance(status, str) or status not in {"complete", "partial", "unknown"}:
        raise ValueError("PDF coverage status is invalid")
    total_pages = value.get("total_pages")
    if total_pages is not None and (
        isinstance(total_pages, bool) or not isinstance(total_pages, int)
        or total_pages < 1 or total_pages > PDF_COVERAGE_MAX_PAGE_NUMBER
    ):
        raise ValueError("PDF coverage total_pages is invalid")
    processed_pages = _bounded_positive_pages(value.get("processed_pages"), field="processed_pages")
    unprocessed_visual_pages = _bounded_positive_pages(
        value.get("unprocessed_visual_pages"), field="unprocessed_visual_pages"
    )
    blank_pages = _bounded_positive_pages(value.get("confirmed_blank_pages"), field="confirmed_blank_pages") if has_blank_pages else None
    no_effective_pages = _bounded_positive_pages(
        value.get("no_effective_content_pages"), field="no_effective_content_pages"
    ) if has_no_effective_pages else None
    assessments = _validate_page_assessments(value.get("page_assessments"), total_pages) if has_assessments else None
    all_pages = processed_pages + unprocessed_visual_pages + (blank_pages or []) + (no_effective_pages or [])
    if total_pages is not None and any(page > total_pages for page in all_pages):
        raise ValueError("PDF coverage page exceeds total_pages")
    classified_pages = processed_pages + unprocessed_visual_pages
    if blank_pages is not None and (set(blank_pages) & set(classified_pages + (no_effective_pages or []))):
        raise ValueError("confirmed blank pages overlap another page classification")
    if no_effective_pages is not None and set(no_effective_pages) & set(classified_pages + (blank_pages or [])):
        raise ValueError("no-effective-content pages overlap another page classification")
    skipped_count = value.get("skipped_visual_block_count")
    if (
        isinstance(skipped_count, bool) or not isinstance(skipped_count, int)
        or skipped_count < 0 or skipped_count > PDF_COVERAGE_MAX_PAGE_ITEMS
        or skipped_count < len(unprocessed_visual_pages)
    ):
        raise ValueError("PDF coverage skipped visual count is invalid")
    raw_reasons = value.get("reasons")
    if not isinstance(raw_reasons, list) or len(raw_reasons) > PDF_COVERAGE_MAX_REASONS:
        raise ValueError("PDF coverage reasons must be a bounded list")
    if (
        any(not isinstance(reason, str) or reason not in PDF_COVERAGE_REASONS for reason in raw_reasons)
        or raw_reasons != sorted(set(raw_reasons))
    ):
        raise ValueError("PDF coverage reasons are invalid")
    reasons = list(raw_reasons)

    if assessments is not None:
        assessment_pages = {item["page"]: item for item in assessments}
        blank_assessment_pages = sorted(page for page, item in assessment_pages.items() if item["status"] == "blank")
        if blank_pages != blank_assessment_pages:
            raise ValueError("confirmed blank pages must match blank assessments")
        no_effective_assessment_pages = sorted(
            page for page, item in assessment_pages.items() if item["status"] == "no_effective_content"
        )
        if has_no_effective_pages and no_effective_pages != no_effective_assessment_pages:
            raise ValueError("no-effective-content pages must match their assessments")
        if not has_no_effective_pages and no_effective_assessment_pages:
            raise ValueError("no-effective-content assessments require a page list")
        if any(
            item["status"] not in {"blank", "no_effective_content"}
            and page not in unprocessed_visual_pages
            for page, item in assessment_pages.items()
        ):
            raise ValueError("uncertain or failed assessments must be unprocessed visual pages")

    if status == "complete":
        expected_covered = sorted(processed_pages + (blank_pages or []) + (no_effective_pages or []))
        if (
            total_pages is None or expected_covered != list(range(1, total_pages + 1))
            or unprocessed_visual_pages or skipped_count or reasons
            or (assessments is not None and any(
                item["status"] not in {"blank", "no_effective_content"} for item in assessments
            ))
        ):
            raise ValueError("complete PDF coverage must account for every page")
    if status == "partial" and skipped_count == 0:
        raise ValueError("partial PDF coverage must identify skipped visual content")

    report: PdfCoverageReportV1 = {
        "contract_version": PDF_COVERAGE_CONTRACT_VERSION,
        "status": status,
        "total_pages": total_pages,
        "processed_pages": processed_pages,
        "unprocessed_visual_pages": unprocessed_visual_pages,
        "skipped_visual_block_count": skipped_count,
        "reasons": reasons,
    }
    if has_blank_pages:
        report["confirmed_blank_pages"] = list(blank_pages or [])
        report["page_assessments"] = assessments or []
    if has_no_effective_pages:
        report["no_effective_content_pages"] = list(no_effective_pages or [])
    return report


def create_pdf_coverage_report(
    *,
    status: PdfCoverageStatus,
    total_pages: int | None,
    processed_pages: Iterable[int] = (),
    unprocessed_visual_pages: Iterable[int] = (),
    skipped_visual_block_count: int = 0,
    reasons: Iterable[PdfCoverageReason] = (),
    confirmed_blank_pages: Iterable[int] | None = None,
    no_effective_content_pages: Iterable[int] | None = None,
    page_assessments: Iterable[PdfPageAssessment] | None = None,
) -> PdfCoverageReportV1:
    """Create a canonical report with sorted, de-duplicated list fields."""
    report: dict[str, Any] = {
        "contract_version": PDF_COVERAGE_CONTRACT_VERSION,
        "status": status,
        "total_pages": total_pages,
        "processed_pages": sorted(set(processed_pages)),
        "unprocessed_visual_pages": sorted(set(unprocessed_visual_pages)),
        "skipped_visual_block_count": skipped_visual_block_count,
        "reasons": sorted(set(reasons)),
    }
    if no_effective_content_pages is not None and confirmed_blank_pages is None:
        confirmed_blank_pages = ()
    if (
        confirmed_blank_pages is not None
        or page_assessments is not None
        or no_effective_content_pages is not None
    ):
        if confirmed_blank_pages is None or page_assessments is None:
            raise ValueError("blank page extension fields must be provided together")
        assessments = []
        for assessment in page_assessments:
            copied = dict(assessment)
            copied["checks"] = sorted(set(copied.get("checks", [])))
            assessments.append(copied)
        assessments.sort(key=lambda item: item.get("page", 0))
        report["confirmed_blank_pages"] = sorted(set(confirmed_blank_pages))
        report["page_assessments"] = assessments
        if no_effective_content_pages is not None:
            report["no_effective_content_pages"] = sorted(set(no_effective_content_pages))
    return validate_pdf_coverage_report(report)


def unknown_pdf_coverage_report() -> PdfCoverageReportV1:
    return create_pdf_coverage_report(status="unknown", total_pages=None)


def attach_pdf_coverage_unit(
    segments: list[dict[str, Any]], report: object
) -> PdfCoverageReportV1:
    """Attach one dedicated structured unit below the first parser root."""
    validated = validate_pdf_coverage_report(report)
    if not segments:
        raise ValueError("PDF coverage requires at least one parser segment")
    first = segments[0]
    root = first.get("parser_unit")
    if not isinstance(root, Mapping):
        raise ValueError("PDF coverage requires a root parser unit")
    parser = root.get("parser")
    parent_key = root.get("unit_key")
    if not isinstance(parser, Mapping) or not isinstance(parent_key, str):
        raise ValueError("PDF coverage root parser unit is invalid")

    unit = build_parser_unit(
        source_kind="pdf",
        unit_kind="structured_unit",
        ordinal=0,
        unit_key=PDF_COVERAGE_UNIT_KEY,
        parser=parser,
        parent_key=parent_key,
    )
    unit["value"] = validated  # type: ignore[typeddict-unknown-key]
    unit["text"] = ""
    unit["structure_type"] = "pdf_coverage"  # type: ignore[typeddict-unknown-key]
    structured_units = first.setdefault("structured_units", [])
    if not isinstance(structured_units, list):
        raise ValueError("parser structured_units must be a list")
    structured_units[:] = [
        existing for existing in structured_units
        if not (isinstance(existing, Mapping) and existing.get("unit_key") == PDF_COVERAGE_UNIT_KEY)
    ]
    structured_units.append(unit)
    return validated


def apply_human_no_effective_content_reviews_to_report(
    report_value: object,
    reviews: Iterable[Mapping[str, Any]],
) -> PdfCoverageReportV1:
    """Project human review evidence over an immutable parser coverage report."""
    report = validate_pdf_coverage_report(report_value)
    raw_reviews: list[Mapping[str, Any]] = []
    for review in reviews:
        if len(raw_reviews) >= PDF_COVERAGE_MAX_PAGE_ITEMS:
            raise ValueError("page reviews exceed the configured bound")
        raw_reviews.append(review)
    normalized_reviews = _validate_page_assessments(
        sorted(raw_reviews, key=lambda item: item.get("page", 0)), report["total_pages"]
    )
    if not normalized_reviews:
        raise ValueError("at least one human page review is required")

    unprocessed = set(report["unprocessed_visual_pages"])
    processed = set(report["processed_pages"])
    assessments = {item["page"]: item for item in report.get("page_assessments", [])}
    no_effective = set(report.get("no_effective_content_pages", []))
    blank = set(report.get("confirmed_blank_pages", []))
    newly_resolved: set[int] = set()
    for review in normalized_reviews:
        page = review["page"]
        if review["status"] != "no_effective_content" or review["classification_source"] != "human_review":
            raise ValueError("only human-reviewed no-effective-content pages can be applied")
        if page in processed or page in blank:
            raise ValueError("human review may resolve only an unprocessed page")
        existing = assessments.get(page)
        if page in no_effective:
            if existing == review:
                continue
            raise ValueError("page already has a different no-effective-content review")
        if page not in unprocessed:
            raise ValueError("human review may resolve only an unprocessed page")
        assessments[page] = review
        unprocessed.remove(page)
        no_effective.add(page)
        newly_resolved.add(page)

    total_pages = report["total_pages"]
    remaining_unprocessed = sorted(unprocessed)
    skipped_count = (
        0 if not remaining_unprocessed
        else max(len(remaining_unprocessed), report["skipped_visual_block_count"] - len(newly_resolved))
    )
    reasons = report["reasons"] if remaining_unprocessed else []
    covered = sorted(processed | blank | no_effective)
    if total_pages is not None and not remaining_unprocessed and covered == list(range(1, total_pages + 1)):
        status = "complete"
    elif remaining_unprocessed:
        status = "partial"
    else:
        status = "unknown"
    return create_pdf_coverage_report(
        status=status,
        total_pages=total_pages,
        processed_pages=report["processed_pages"],
        unprocessed_visual_pages=remaining_unprocessed,
        skipped_visual_block_count=skipped_count,
        reasons=reasons,
        confirmed_blank_pages=sorted(blank),
        no_effective_content_pages=sorted(no_effective),
        page_assessments=sorted(assessments.values(), key=lambda item: item["page"]),
    )


def apply_human_no_effective_content_reviews(
    source: Mapping[str, Any],
    reviews: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply auditable human page reviews without changing parsed text or chunks."""
    updated_report = apply_human_no_effective_content_reviews_to_report(
        source.get("coverage"), reviews
    )
    original_segments = source.get("segments")
    if not isinstance(original_segments, list) or not original_segments:
        raise ValueError("human page reviews require parsed source segments")
    segments = list(original_segments)
    first_segment = dict(segments[0])
    existing_units = first_segment.get("structured_units", [])
    if not isinstance(existing_units, list):
        raise ValueError("parser structured_units must be a list")
    first_segment["structured_units"] = list(existing_units)
    segments[0] = first_segment
    attach_pdf_coverage_unit(segments, updated_report)

    updated_source = dict(source)
    updated_source["coverage"] = updated_report
    updated_source["segments"] = segments
    return updated_source


def _block_content(block: object) -> object:
    if isinstance(block, Mapping):
        return block.get("content")
    return getattr(block, "content", None)


def pdf_coverage_from_document_blocks(
    blocks: Iterable[object],
) -> PdfCoverageReportV1:
    """Read and conservatively aggregate dedicated block values."""
    reports: list[PdfCoverageReportV1] = []
    try:
        for block in blocks:
            content = _block_content(block)
            if not isinstance(content, Mapping):
                continue
            unit = content.get("parser_unit")
            if not isinstance(unit, Mapping) or unit.get("unit_key") != PDF_COVERAGE_UNIT_KEY:
                continue
            if unit.get("unit_kind") != "structured_unit" or unit.get("source_kind") != "pdf":
                return unknown_pdf_coverage_report()
            reports.append(validate_pdf_coverage_report(unit.get("value")))
    except (AttributeError, TypeError, ValueError):
        return unknown_pdf_coverage_report()
    if not reports:
        return unknown_pdf_coverage_report()
    if len(reports) == 1:
        return reports[0]

    has_extension = any(
        "confirmed_blank_pages" in report or "no_effective_content_pages" in report
        for report in reports
    )
    has_no_effective_extension = any("no_effective_content_pages" in report for report in reports)
    if has_extension:
        unique_reports: list[PdfCoverageReportV1] = []
        for report in reports:
            if report not in unique_reports:
                unique_reports.append(report)
        reports = unique_reports
    totals = {report["total_pages"] for report in reports}
    total_pages = totals.pop() if len(totals) == 1 else None
    processed = sorted({page for report in reports for page in report["processed_pages"]})
    unprocessed = sorted({page for report in reports for page in report["unprocessed_visual_pages"]})
    skipped_count = sum(report["skipped_visual_block_count"] for report in reports)
    reasons = sorted({reason for report in reports for reason in report["reasons"]})
    blanks = sorted({page for report in reports for page in report.get("confirmed_blank_pages", [])})
    no_effective = sorted({
        page for report in reports for page in report.get("no_effective_content_pages", [])
    })
    assessment_by_page: dict[int, PdfPageAssessment] = {}
    if has_extension:
        for report in reports:
            for page in report.get("processed_pages", []) + report.get("unprocessed_visual_pages", []):
                if page in blanks or page in no_effective:
                    return unknown_pdf_coverage_report()
            for page, assessment in ((item["page"], item) for item in report.get("page_assessments", [])):
                existing = assessment_by_page.get(page)
                if existing is not None and existing != assessment:
                    return unknown_pdf_coverage_report()
                assessment_by_page[page] = assessment
        assessment_blanks = {page for page, item in assessment_by_page.items() if item["status"] == "blank"}
        if assessment_blanks != set(blanks):
            return unknown_pdf_coverage_report()
        assessment_no_effective = {
            page for page, item in assessment_by_page.items()
            if item["status"] == "no_effective_content"
        }
        if assessment_no_effective != set(no_effective):
            return unknown_pdf_coverage_report()
        assessment_visual_pages = set(assessment_by_page) - assessment_blanks - assessment_no_effective
        if set(processed) & (set(blanks) | set(no_effective) | assessment_visual_pages):
            return unknown_pdf_coverage_report()
        if (set(processed) | set(unprocessed)) & (set(blanks) | set(no_effective)):
            return unknown_pdf_coverage_report()
    if skipped_count:
        status: PdfCoverageStatus = "partial"
    elif (
        total_pages is not None
        and sorted(processed + blanks + no_effective) == list(range(1, total_pages + 1))
        and not unprocessed
        and all(report["status"] == "complete" for report in reports)
    ):
        status = "complete"
    else:
        status = "unknown"
    try:
        kwargs: dict[str, Any] = {}
        if has_extension:
            kwargs["confirmed_blank_pages"] = blanks
            if has_no_effective_extension:
                kwargs["no_effective_content_pages"] = no_effective
            kwargs["page_assessments"] = sorted(assessment_by_page.values(), key=lambda item: item["page"])
        return create_pdf_coverage_report(
            status=status,
            total_pages=total_pages,
            processed_pages=processed,
            unprocessed_visual_pages=unprocessed,
            skipped_visual_block_count=skipped_count,
            reasons=reasons,
            **kwargs,
        )
    except ValueError:
        return unknown_pdf_coverage_report()
