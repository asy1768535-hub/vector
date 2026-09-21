from __future__ import annotations

import pytest

from app.services import pdf_routing


def _page(*, text_chars: int, visual: bool, low_text: bool, page: int = 1) -> dict:
    return {
        "page": page,
        "native_text_chars": text_chars,
        "embedded_image_count": 1 if visual else 0,
        "has_visual_content": visual,
        "low_text": low_text,
    }


def _report(
    pages: list[dict],
    *,
    status: str = "complete",
    total_pages: int | None = None,
    page_count_known: bool | None = None,
    page_limit_exceeded: bool = False,
    image_limit_exceeded: bool = False,
    has_mixed_content: bool = False,
    unknown_reason: str | None = None,
) -> dict:
    return {
        "contract_version": "pdf-preflight-v1",
        "status": status,
        "total_pages": total_pages if total_pages is not None else len(pages),
        "page_count_known": status == "complete" if page_count_known is None else page_count_known,
        "page_limit_exceeded": page_limit_exceeded,
        "image_limit_exceeded": image_limit_exceeded,
        "has_mixed_content": has_mixed_content,
        "pages": pages,
        "unknown_reason": unknown_reason,
    }


@pytest.mark.parametrize(
    ("name", "report", "authorized", "selection", "needs_review", "reasons"),
    [
        (
            "native document without provider authorization",
            _report([_page(text_chars=80, visual=False, low_text=False)]),
            False,
            "native_or_rapidocr",
            False,
            ["mineru_not_authorized", "native_text_sufficient"],
        ),
        (
            "native document with provider authorization",
            _report([_page(text_chars=80, visual=False, low_text=False)]),
            True,
            "native_or_rapidocr",
            False,
            ["native_text_sufficient"],
        ),
        (
            "authorized scan document",
            _report([_page(text_chars=0, visual=True, low_text=True)]),
            True,
            "mineru",
            False,
            ["scan_page_detected"],
        ),
        (
            "authorized mixed document",
            _report(
                [
                    _page(text_chars=80, visual=False, low_text=False),
                    _page(text_chars=0, visual=True, low_text=True, page=2),
                ],
                has_mixed_content=True,
            ),
            True,
            "mineru",
            False,
            ["mixed_content_detected", "scan_page_detected"],
        ),
        (
            "provider unavailable for a scan document",
            _report([_page(text_chars=0, visual=True, low_text=True)]),
            False,
            "native_or_rapidocr",
            True,
            ["mineru_not_authorized", "scan_page_detected"],
        ),
        (
            "unknown preflight",
            _report(
                [],
                status="unknown",
                total_pages=None,
                unknown_reason="parse_failed",
            ),
            True,
            "native_or_rapidocr",
            True,
            ["preflight_unknown"],
        ),
        (
            "resource-limited preflight",
            _report(
                [],
                status="unknown",
                total_pages=100001,
                page_count_known=True,
                page_limit_exceeded=True,
                unknown_reason="resource_limit",
            ),
            True,
            "native_or_rapidocr",
            True,
            ["preflight_unknown", "resource_limit"],
        ),
        (
            "resource-limited text budget preflight",
            _report(
                [],
                status="unknown",
                total_pages=5,
                page_count_known=True,
                page_limit_exceeded=False,
                unknown_reason="resource_limit",
            ),
            True,
            "native_or_rapidocr",
            True,
            ["preflight_unknown", "resource_limit"],
        ),
    ],
)
def test_choose_pdf_route_matrix(
    name,
    report,
    authorized,
    selection,
    needs_review,
    reasons,
):
    decision = pdf_routing.choose_pdf_route(
        report,
        mineru_authorized=authorized,
    )

    assert name
    assert decision == {
        "contract_version": "pdf-routing-v1",
        "selection": selection,
        "needs_review": needs_review,
        "reasons": reasons,
    }


def test_uncertain_preflight_can_never_select_mineru():
    report = _report(
        [],
        status="unknown",
        total_pages=None,
        unknown_reason="page_count_unknown",
    )

    decision = pdf_routing.choose_pdf_route(report, mineru_authorized=True)

    assert decision["selection"] == "native_or_rapidocr"
    assert decision["needs_review"] is True
    assert "preflight_unknown" in decision["reasons"]
