"""Bounded, fact-only PDF preflight for deterministic parser selection."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypedDict

from app.services import pdf_extract

PDF_PREFLIGHT_CONTRACT_VERSION = "pdf-preflight-v1"
PDF_PREFLIGHT_MAX_REPORTED_PAGE_COUNT = 1_000_000

PdfPreflightStatus = Literal["complete", "unknown"]
PdfPreflightUnknownReason = Literal[
    "page_count_unknown",
    "parse_failed",
    "resource_limit",
    "text_extraction_failed",
]
PDF_PREFLIGHT_UNKNOWN_REASONS = frozenset(
    {
        "page_count_unknown",
        "parse_failed",
        "resource_limit",
        "text_extraction_failed",
    }
)


class PdfPagePreflightV1(TypedDict):
    page: int
    native_text_chars: int
    embedded_image_count: int
    has_visual_content: bool
    low_text: bool


class PdfPreflightReportV1(TypedDict):
    contract_version: Literal["pdf-preflight-v1"]
    status: PdfPreflightStatus
    total_pages: int | None
    page_count_known: bool
    page_limit_exceeded: bool
    image_limit_exceeded: bool
    has_mixed_content: bool
    pages: list[PdfPagePreflightV1]
    unknown_reason: PdfPreflightUnknownReason | None


def _page_count_hint(reader: object) -> int | None:
    try:
        root = getattr(reader, "root_object")
        page_tree = root.get("/Pages")
        if page_tree is not None and hasattr(page_tree, "get_object"):
            page_tree = page_tree.get_object()
        count = page_tree.get("/Count") if page_tree is not None else None
        if count is not None:
            return int(count)
    except Exception:  # noqa: BLE001 - corrupted or untrusted PDF page trees must not raise
        pass
    try:
        return int(len(getattr(reader, "pages")))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - page collection access can fail on corrupted PDFs
        return None


def _has_mixed_content(pages: list[PdfPagePreflightV1]) -> bool:
    has_native_page = any(not page["low_text"] for page in pages)
    has_scan_page = any(
        page["low_text"] and page["has_visual_content"] for page in pages
    )
    has_mixed_page = any(
        not page["low_text"] and page["has_visual_content"] for page in pages
    )
    return has_mixed_page or (has_native_page and has_scan_page)


def _unknown_report(
    reason: PdfPreflightUnknownReason,
    *,
    total_pages: int | None = None,
    page_count_known: bool = False,
    page_limit_exceeded: bool = False,
    image_limit_exceeded: bool = False,
    pages: list[PdfPagePreflightV1] | None = None,
) -> PdfPreflightReportV1:
    page_facts = list(pages or [])
    return {
        "contract_version": PDF_PREFLIGHT_CONTRACT_VERSION,
        "status": "unknown",
        "total_pages": total_pages,
        "page_count_known": page_count_known,
        "page_limit_exceeded": page_limit_exceeded,
        "image_limit_exceeded": image_limit_exceeded,
        "has_mixed_content": _has_mixed_content(page_facts),
        "pages": page_facts,
        "unknown_reason": reason,
    }


def validate_pdf_preflight_report(value: object) -> PdfPreflightReportV1:
    """Validate and copy a preflight report without retaining extra fields."""
    if not isinstance(value, Mapping):
        raise ValueError("PDF preflight report must be an object")
    expected_fields = {
        "contract_version",
        "status",
        "total_pages",
        "page_count_known",
        "page_limit_exceeded",
        "image_limit_exceeded",
        "has_mixed_content",
        "pages",
        "unknown_reason",
    }
    if set(value) != expected_fields:
        raise ValueError("PDF preflight report fields are invalid")
    if value.get("contract_version") != PDF_PREFLIGHT_CONTRACT_VERSION:
        raise ValueError("PDF preflight report version is invalid")

    status = value.get("status")
    if status not in {"complete", "unknown"}:
        raise ValueError("PDF preflight status is invalid")
    total_pages = value.get("total_pages")
    if total_pages is not None and (
        isinstance(total_pages, bool)
        or not isinstance(total_pages, int)
        or total_pages < 0
        or total_pages > PDF_PREFLIGHT_MAX_REPORTED_PAGE_COUNT
    ):
        raise ValueError("PDF preflight total_pages is invalid")

    bool_fields = (
        "page_count_known",
        "page_limit_exceeded",
        "image_limit_exceeded",
        "has_mixed_content",
    )
    if any(not isinstance(value.get(field), bool) for field in bool_fields):
        raise ValueError("PDF preflight flags are invalid")

    raw_pages = value.get("pages")
    if not isinstance(raw_pages, list) or len(raw_pages) > pdf_extract.PDF_MAX_PAGES:
        raise ValueError("PDF preflight pages must be a bounded list")
    pages: list[PdfPagePreflightV1] = []
    image_count = 0
    text_chars = 0
    for expected_page, raw_page in enumerate(raw_pages, start=1):
        if not isinstance(raw_page, Mapping) or set(raw_page) != {
            "page",
            "native_text_chars",
            "embedded_image_count",
            "has_visual_content",
            "low_text",
        }:
            raise ValueError("PDF preflight page fields are invalid")
        page = raw_page.get("page")
        native_text_chars = raw_page.get("native_text_chars")
        embedded_image_count = raw_page.get("embedded_image_count")
        if page != expected_page:
            raise ValueError("PDF preflight pages must be contiguous")
        if (
            isinstance(native_text_chars, bool)
            or not isinstance(native_text_chars, int)
            or native_text_chars < 0
            or native_text_chars > pdf_extract.PDF_MAX_NORMALIZED_TEXT_CHARS
        ):
            raise ValueError("PDF preflight native text count is invalid")
        if (
            isinstance(embedded_image_count, bool)
            or not isinstance(embedded_image_count, int)
            or embedded_image_count < 0
            or embedded_image_count > pdf_extract.PDF_MAX_EMBEDDED_IMAGES_PER_PAGE
        ):
            raise ValueError("PDF preflight image count is invalid")
        has_visual_content = raw_page.get("has_visual_content")
        low_text = raw_page.get("low_text")
        if not isinstance(has_visual_content, bool) or not isinstance(low_text, bool):
            raise ValueError("PDF preflight page flags are invalid")
        image_count += embedded_image_count
        text_chars += native_text_chars
        if image_count > pdf_extract.PDF_MAX_EMBEDDED_IMAGES:
            raise ValueError("PDF preflight image count exceeds budget")
        if text_chars > pdf_extract.PDF_MAX_NORMALIZED_TEXT_CHARS:
            raise ValueError("PDF preflight text count exceeds budget")
        pages.append(
            {
                "page": page,
                "native_text_chars": native_text_chars,
                "embedded_image_count": embedded_image_count,
                "has_visual_content": has_visual_content,
                "low_text": low_text,
            }
        )

    reason = value.get("unknown_reason")
    if reason is not None and reason not in PDF_PREFLIGHT_UNKNOWN_REASONS:
        raise ValueError("PDF preflight unknown reason is invalid")
    page_count_known = value["page_count_known"]
    page_limit_exceeded = value["page_limit_exceeded"]
    image_limit_exceeded = value["image_limit_exceeded"]
    if page_count_known and total_pages is None:
        raise ValueError("known PDF page count requires total_pages")
    if status == "complete":
        if (
            reason is not None
            or not page_count_known
            or page_limit_exceeded
            or image_limit_exceeded
            or total_pages != len(pages)
        ):
            raise ValueError("complete PDF preflight report is inconsistent")
    elif reason is None:
        raise ValueError("unknown PDF preflight requires a reason")
    if page_limit_exceeded and (
        not page_count_known
        or total_pages is None
        or total_pages <= pdf_extract.PDF_MAX_PAGES
        or reason != "resource_limit"
    ):
        raise ValueError("PDF page limit report is inconsistent")
    if image_limit_exceeded and reason != "resource_limit":
        raise ValueError("PDF image limit report is inconsistent")
    if value["has_mixed_content"] != _has_mixed_content(pages):
        raise ValueError("PDF mixed-content flag is inconsistent")

    return {
        "contract_version": PDF_PREFLIGHT_CONTRACT_VERSION,
        "status": status,
        "total_pages": total_pages,
        "page_count_known": page_count_known,
        "page_limit_exceeded": page_limit_exceeded,
        "image_limit_exceeded": image_limit_exceeded,
        "has_mixed_content": value["has_mixed_content"],
        "pages": pages,
        "unknown_reason": reason,
    }


def preflight_pdf(
    data: pdf_extract.PdfSource,
    *,
    min_text_chars: int,
) -> PdfPreflightReportV1:
    """Read bounded page facts without decoding images, rendering, or OCR."""
    if (
        isinstance(min_text_chars, bool)
        or not isinstance(min_text_chars, int)
        or min_text_chars < 0
    ):
        raise ValueError("min_text_chars must be a non-negative integer")
    try:
        reader = pdf_extract._open_reader(data)
    except Exception:  # noqa: BLE001 - parser details must not cross this boundary
        return _unknown_report("parse_failed")

    page_count = _page_count_hint(reader)
    if page_count is None:
        return _unknown_report("page_count_unknown")
    if page_count < 0 or page_count > PDF_PREFLIGHT_MAX_REPORTED_PAGE_COUNT:
        return _unknown_report("parse_failed")
    if page_count > pdf_extract.PDF_MAX_PAGES:
        return _unknown_report(
            "resource_limit",
            total_pages=page_count,
            page_count_known=True,
            page_limit_exceeded=True,
        )

    pages: list[PdfPagePreflightV1] = []
    image_totals = [0, 0]
    text_chars = 0
    try:
        page_items = pdf_extract._reader_page_items(reader)
        for index, page in page_items:
            try:
                text = (page.extract_text() or "").replace("\x00", "").strip()
            except Exception:  # noqa: BLE001 - one uncertain page makes routing uncertain
                return _unknown_report(
                    "text_extraction_failed",
                    total_pages=page_count,
                    page_count_known=True,
                    pages=pages,
                )
            text_chars += len(text)
            if text_chars > pdf_extract.PDF_MAX_NORMALIZED_TEXT_CHARS:
                return _unknown_report(
                    "resource_limit",
                    total_pages=page_count,
                    page_count_known=True,
                    pages=pages,
                )
            native_text_chars = pdf_extract._meaningful_char_count(text)
            image_count = pdf_extract._scan_embedded_images(
                pdf_extract._page_images(page),
                image_totals,
            )
            pages.append(
                {
                    "page": index + 1,
                    "native_text_chars": native_text_chars,
                    "embedded_image_count": image_count,
                    "has_visual_content": pdf_extract._page_has_visual_content(
                        page,
                        embedded_image_count=image_count,
                    ),
                    "low_text": native_text_chars < min_text_chars,
                }
            )
    except pdf_extract.PdfResourceLimitError:
        return _unknown_report(
            "resource_limit",
            total_pages=page_count,
            page_count_known=True,
            image_limit_exceeded=True,
            pages=pages,
        )
    except Exception:  # noqa: BLE001 - parser details must not cross this boundary
        return _unknown_report(
            "parse_failed",
            total_pages=page_count,
            page_count_known=True,
            pages=pages,
        )

    if len(pages) != page_count:
        return _unknown_report(
            "parse_failed",
            total_pages=page_count,
            page_count_known=True,
            pages=pages,
        )
    return validate_pdf_preflight_report(
        {
            "contract_version": PDF_PREFLIGHT_CONTRACT_VERSION,
            "status": "complete",
            "total_pages": page_count,
            "page_count_known": True,
            "page_limit_exceeded": False,
            "image_limit_exceeded": False,
            "has_mixed_content": _has_mixed_content(pages),
            "pages": pages,
            "unknown_reason": None,
        }
    )
