"""Bounded PDF parsing coverage reports and parser-unit integration."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypedDict

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


class PdfCoverageReportV1(TypedDict):
    contract_version: Literal["pdf-coverage-v1"]
    status: PdfCoverageStatus
    total_pages: int | None
    processed_pages: list[int]
    unprocessed_visual_pages: list[int]
    skipped_visual_block_count: int
    reasons: list[PdfCoverageReason]


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


def validate_pdf_coverage_report(value: object) -> PdfCoverageReportV1:
    """Validate and copy a report without retaining untrusted fields."""
    if not isinstance(value, Mapping):
        raise ValueError("PDF coverage report must be an object")
    expected_fields = {
        "contract_version",
        "status",
        "total_pages",
        "processed_pages",
        "unprocessed_visual_pages",
        "skipped_visual_block_count",
        "reasons",
    }
    if set(value) != expected_fields:
        raise ValueError("PDF coverage report fields are invalid")
    if value.get("contract_version") != PDF_COVERAGE_CONTRACT_VERSION:
        raise ValueError("PDF coverage report version is invalid")

    status = value.get("status")
    if status not in {"complete", "partial", "unknown"}:
        raise ValueError("PDF coverage status is invalid")
    total_pages = value.get("total_pages")
    if total_pages is not None and (
        isinstance(total_pages, bool)
        or not isinstance(total_pages, int)
        or total_pages < 1
        or total_pages > PDF_COVERAGE_MAX_PAGE_NUMBER
    ):
        raise ValueError("PDF coverage total_pages is invalid")
    processed_pages = _bounded_positive_pages(
        value.get("processed_pages"), field="processed_pages"
    )
    unprocessed_visual_pages = _bounded_positive_pages(
        value.get("unprocessed_visual_pages"),
        field="unprocessed_visual_pages",
    )
    if total_pages is not None and any(
        page > total_pages for page in processed_pages + unprocessed_visual_pages
    ):
        raise ValueError("PDF coverage page exceeds total_pages")

    skipped_count = value.get("skipped_visual_block_count")
    if (
        isinstance(skipped_count, bool)
        or not isinstance(skipped_count, int)
        or skipped_count < 0
        or skipped_count > PDF_COVERAGE_MAX_PAGE_ITEMS
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

    if status == "complete" and (
        total_pages is None
        or processed_pages != list(range(1, total_pages + 1))
        or unprocessed_visual_pages
        or skipped_count
        or reasons
    ):
        raise ValueError("complete PDF coverage must account for every page")
    if status == "partial" and skipped_count == 0:
        raise ValueError("partial PDF coverage must identify skipped visual content")

    return {
        "contract_version": PDF_COVERAGE_CONTRACT_VERSION,
        "status": status,
        "total_pages": total_pages,
        "processed_pages": processed_pages,
        "unprocessed_visual_pages": unprocessed_visual_pages,
        "skipped_visual_block_count": skipped_count,
        "reasons": reasons,
    }


def create_pdf_coverage_report(
    *,
    status: PdfCoverageStatus,
    total_pages: int | None,
    processed_pages: Iterable[int] = (),
    unprocessed_visual_pages: Iterable[int] = (),
    skipped_visual_block_count: int = 0,
    reasons: Iterable[PdfCoverageReason] = (),
) -> PdfCoverageReportV1:
    """Create a canonical report with sorted, de-duplicated list fields."""
    return validate_pdf_coverage_report(
        {
            "contract_version": PDF_COVERAGE_CONTRACT_VERSION,
            "status": status,
            "total_pages": total_pages,
            "processed_pages": sorted(set(processed_pages)),
            "unprocessed_visual_pages": sorted(set(unprocessed_visual_pages)),
            "skipped_visual_block_count": skipped_visual_block_count,
            "reasons": sorted(set(reasons)),
        }
    )


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
        existing
        for existing in structured_units
        if not (
            isinstance(existing, Mapping)
            and existing.get("unit_key") == PDF_COVERAGE_UNIT_KEY
        )
    ]
    structured_units.append(unit)
    return validated


def _block_content(block: object) -> object:
    if isinstance(block, Mapping):
        return block.get("content")
    return getattr(block, "content", None)


def pdf_coverage_from_document_blocks(
    blocks: Iterable[object],
) -> PdfCoverageReportV1:
    """Read and aggregate dedicated block values; malformed data fails closed."""
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

    totals = {report["total_pages"] for report in reports}
    total_pages = totals.pop() if len(totals) == 1 else None
    processed = sorted({page for report in reports for page in report["processed_pages"]})
    unprocessed = sorted(
        {page for report in reports for page in report["unprocessed_visual_pages"]}
    )
    skipped_count = sum(report["skipped_visual_block_count"] for report in reports)
    reasons = sorted({reason for report in reports for reason in report["reasons"]})
    if skipped_count:
        status: PdfCoverageStatus = "partial"
    elif (
        total_pages is not None
        and processed == list(range(1, total_pages + 1))
        and all(report["status"] == "complete" for report in reports)
    ):
        status = "complete"
    else:
        status = "unknown"
    try:
        return create_pdf_coverage_report(
            status=status,
            total_pages=total_pages,
            processed_pages=processed,
            unprocessed_visual_pages=unprocessed,
            skipped_visual_block_count=skipped_count,
            reasons=reasons,
        )
    except ValueError:
        return unknown_pdf_coverage_report()
