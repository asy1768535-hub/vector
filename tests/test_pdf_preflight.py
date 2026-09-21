from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import pdf_extract, pdf_preflight


class _Page:
    def __init__(self, text: str = "", images: list[object] | None = None, *, visual=False):
        self._text = text
        self.images = images or []
        self._resources = {"/XObject": object()} if visual else {}

    def extract_text(self):
        return self._text

    def get(self, key):
        return self._resources if key == "/Resources" else None


class _BrokenPage(_Page):
    def extract_text(self):
        raise RuntimeError("private parser detail")


class _UnknownPages:
    def __iter__(self):
        return iter([_Page("text")])


class _NoDataImage:
    @property
    def data(self):
        raise AssertionError("preflight must not decode image data")


def _reader(pages):
    return SimpleNamespace(pages=pages)


def test_preflight_reports_native_scan_mixed_and_blank_pages(monkeypatch):
    pages = [
        _Page("native text is sufficiently long"),
        _Page("", [_NoDataImage()]),
        _Page("mixed page has sufficient native text", [_NoDataImage()]),
        _Page(""),
    ]
    monkeypatch.setattr(pdf_extract, "_open_reader", lambda _data: _reader(pages))

    report = pdf_preflight.preflight_pdf(b"pdf", min_text_chars=10)

    assert report["status"] == "complete"
    assert report["total_pages"] == 4
    assert report["has_mixed_content"] is True
    assert report["pages"] == [
        {
            "page": 1,
            "native_text_chars": 28,
            "embedded_image_count": 0,
            "has_visual_content": False,
            "low_text": False,
        },
        {
            "page": 2,
            "native_text_chars": 0,
            "embedded_image_count": 1,
            "has_visual_content": True,
            "low_text": True,
        },
        {
            "page": 3,
            "native_text_chars": 32,
            "embedded_image_count": 1,
            "has_visual_content": True,
            "low_text": False,
        },
        {
            "page": 4,
            "native_text_chars": 0,
            "embedded_image_count": 0,
            "has_visual_content": False,
            "low_text": True,
        },
    ]
    assert pdf_preflight.validate_pdf_preflight_report(report) == report


def test_preflight_unknown_page_count_does_not_iterate(monkeypatch):
    pages = _UnknownPages()
    monkeypatch.setattr(pdf_extract, "_open_reader", lambda _data: _reader(pages))

    report = pdf_preflight.preflight_pdf(b"pdf", min_text_chars=10)

    assert report["status"] == "unknown"
    assert report["unknown_reason"] == "page_count_unknown"
    assert report["pages"] == []


def test_preflight_page_limit_is_reported_without_page_access(monkeypatch):
    class TooManyPages:
        def __len__(self):
            return pdf_extract.PDF_MAX_PAGES + 1

        def __getitem__(self, _index):
            raise AssertionError("over-limit PDF pages must not be read")

    monkeypatch.setattr(pdf_extract, "_open_reader", lambda _data: _reader(TooManyPages()))

    report = pdf_preflight.preflight_pdf(b"pdf", min_text_chars=10)

    assert report["status"] == "unknown"
    assert report["page_limit_exceeded"] is True
    assert report["total_pages"] == pdf_extract.PDF_MAX_PAGES + 1


def test_preflight_image_limit_is_reported_without_decoding(monkeypatch):
    images = [_NoDataImage()] * (pdf_extract.PDF_MAX_EMBEDDED_IMAGES_PER_PAGE + 1)
    pages = [_Page("enough text for a native page", images)]
    monkeypatch.setattr(pdf_extract, "_open_reader", lambda _data: _reader(pages))

    report = pdf_preflight.preflight_pdf(b"pdf", min_text_chars=10)

    assert report["status"] == "unknown"
    assert report["image_limit_exceeded"] is True
    assert report["unknown_reason"] == "resource_limit"


def test_preflight_corrupt_pdf_and_page_text_failure_are_unknown(monkeypatch):
    monkeypatch.setattr(pdf_extract, "_open_reader", lambda _data: (_ for _ in ()).throw(ValueError("secret")))
    corrupt = pdf_preflight.preflight_pdf(b"pdf", min_text_chars=10)
    assert corrupt["status"] == "unknown"
    assert corrupt["unknown_reason"] == "parse_failed"

    monkeypatch.setattr(pdf_extract, "_open_reader", lambda _data: _reader([_BrokenPage()]))
    broken_page = pdf_preflight.preflight_pdf(b"pdf", min_text_chars=10)
    assert broken_page["status"] == "unknown"
    assert broken_page["unknown_reason"] == "text_extraction_failed"


def test_preflight_validator_rejects_extra_fields_and_inconsistent_mixed_flag(monkeypatch):
    monkeypatch.setattr(pdf_extract, "_open_reader", lambda _data: _reader([_Page("native text")]))
    report = pdf_preflight.preflight_pdf(b"pdf", min_text_chars=5)

    with pytest.raises(ValueError, match="fields"):
        pdf_preflight.validate_pdf_preflight_report({**report, "extra": True})
    with pytest.raises(ValueError, match="mixed-content"):
        pdf_preflight.validate_pdf_preflight_report({**report, "has_mixed_content": True})
def test_page_count_hint_catches_untrusted_page_tree_exceptions():
    class ExplosivePages:
        def __len__(self):
            raise RecursionError("circular page tree")

    class BrokenRootReader:
        @property
        def root_object(self):
            raise IndexError("corrupted xref table")

        pages = ExplosivePages()

    assert pdf_preflight._page_count_hint(BrokenRootReader()) is None
