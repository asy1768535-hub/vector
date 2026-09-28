"""Real PDF containers exercise batching; OCR is replaced at its expensive boundary."""
import io
import math
from pathlib import Path

import pypdf
import pytest
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.services import pdf_extract
from app.services.evidence_write_path import validate_parser_segments


def make_pdf(count, *, text=True, width=595, height=842):
    writer = pypdf.PdfWriter()
    for index in range(count):
        page = writer.add_blank_page(width=width, height=height)
        if text:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({
                NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 30 60 Td (page-{index + 1:04d} native content) Tj ET'.encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    writer.close()
    return out.getvalue()


def build(data, **kwargs):
    options = dict(chunk_size=100, chunk_overlap=0, ocr_enabled=False, ocr=None,
                   min_text_chars=1, render_dpi=200, max_ocr_pages=50,
                   resource_recovery=True)
    options.update(kwargs)
    return pdf_extract.build_pdf_source(data, **options)


def test_recovery_processes_51_ocr_pages_with_original_page_numbers(monkeypatch):
    paths = []
    def render(data, index, dpi):
        paths.append(Path(data))
        return b'image'
    monkeypatch.setattr(pdf_extract, '_render_page_png', render)
    calls = []
    def ocr(_):
        calls.append(1)
        return f'recognized page {len(calls)}'
    source = build(make_pdf(51, text=False), ocr_enabled=True, ocr=ocr)
    assert len(calls) == 51
    assert [c['location']['page'] for c in source['chunks']] == list(range(1, 52))
    assert source['coverage']['total_pages'] == 51
    assert 'recognized page 51' in source['normalized_text']
    assert len(set(paths)) == 3 and not any(p.exists() for p in paths)
    validate_parser_segments(source['segments'])


def test_recovery_over_500_pages_keeps_source_ranges_and_default_limit():
    data = make_pdf(501)
    with pytest.raises(pdf_extract.PdfResourceLimitError, match='page count'):
        build(data, resource_recovery=False)
    source = build(data)
    assert len(source['chunks']) == 501
    for number, chunk in enumerate(source['chunks'], 1):
        assert chunk['location']['page'] == number
        assert f'page-{number:04d}' in chunk['text']
        assert source['normalized_text'][chunk['source_start']:chunk['source_end']] == chunk['text']
    validate_parser_segments(source['segments'])


def test_recovery_large_page_stays_within_pixel_budget(monkeypatch):
    observed = []
    def render(data, index, dpi):
        page = pypdf.PdfReader(data).pages[index]
        pixels = math.ceil(float(page.mediabox.width) * dpi / 72) * math.ceil(float(page.mediabox.height) * dpi / 72)
        observed.append((dpi, pixels))
        return b'image'
    monkeypatch.setattr(pdf_extract, '_render_page_png', render)
    source = build(make_pdf(1, text=False, width=2580, height=2455),
                   ocr_enabled=True, ocr=lambda _: 'recognized text')
    assert len(observed) == 1
    dpi, pixels = observed[0]
    assert 72 <= dpi < 200 and pixels <= pdf_extract.PDF_MAX_RENDER_PIXELS
    assert source['segments'][0]['quality']['render_dpi'] == dpi


def test_recovery_blank_batch_does_not_hide_later_text():
    writer = pypdf.PdfWriter()
    writer.append(io.BytesIO(make_pdf(25, text=False)))
    writer.append(io.BytesIO(make_pdf(1)))
    data = io.BytesIO()
    writer.write(data)
    source = build(data.getvalue())
    assert len(source['chunks']) == 1 and source['chunks'][0]['location']['page'] == 26
    assert source['coverage']['total_pages'] == 26


def test_recovery_keeps_document_text_budget_across_batches(monkeypatch):
    monkeypatch.setattr(pdf_extract, 'PDF_MAX_NORMALIZED_TEXT_CHARS', 800)
    with pytest.raises(pdf_extract.PdfResourceLimitError, match='normalized text'):
        build(make_pdf(51))


def test_recovery_later_batch_failure_does_not_return_partial_result(monkeypatch):
    paths = []
    def render(data, index, dpi):
        paths.append(Path(data))
        return b'image'
    monkeypatch.setattr(pdf_extract, '_render_page_png', render)
    calls = 0
    def ocr(_):
        nonlocal calls
        calls += 1
        if calls == 26:
            raise RuntimeError('OCR unavailable')
        return 'recognized text'
    with pytest.raises(pdf_extract.PdfExtractError):
        build(make_pdf(51, text=False), ocr_enabled=True, ocr=ocr)
    assert calls == 26 and not any(p.exists() for p in paths)


@pytest.mark.parametrize('recovery,limit,ceiling,expected_dpis', [
    (True, 'rendered_png_bytes', 144, [200, 160, 128]),
    (True, 'rendered_png_bytes', 0, [200, 160, 128, 102, 81, 72]),
    (False, 'rendered_png_bytes', 144, [200]),
    (True, 'embedded_image_bytes', 144, [200]),
])
def test_recovery_png_bytes(monkeypatch, recovery, limit, ceiling, expected_dpis):
    observed = []
    ocr_calls = []

    def render(data, index, dpi):
        observed.append(dpi)
        if dpi > ceiling:
            pdf_extract._resource_limit(limit)
        return b'bounded-image'

    def ocr(data):
        ocr_calls.append(data)
        return 'recognized content'

    monkeypatch.setattr(pdf_extract, '_render_page_png', render)
    options = dict(ocr_enabled=True, ocr=ocr, resource_recovery=recovery)
    if recovery and limit == 'rendered_png_bytes' and ceiling:
        source = build(make_pdf(1, text=False), **options)
        assert source['segments'][0]['quality']['render_dpi'] == 128
        assert source['coverage']['processed_pages'] == [1]
        assert ocr_calls == [b'bounded-image']
    else:
        with pytest.raises(pdf_extract.PdfResourceLimitError):
            build(make_pdf(1, text=False), **options)
        assert ocr_calls == []
    assert observed == expected_dpis


@pytest.mark.parametrize("recovery,ocr_enabled,error", [
    (True, True, None),
    (False, True, UnicodeEncodeError),
    (True, False, pdf_extract.PdfExtractError),
])
def test_recovery_invalid_native_unicode(monkeypatch, recovery, ocr_enabled, error):
    broken = "native " + chr(0xD800) + " text"
    monkeypatch.setattr(pypdf._page.PageObject, "extract_text", lambda self: broken)
    monkeypatch.setattr(pdf_extract, "_render_page_png", lambda *args: b"original-page-image")
    calls = []
    ocr = lambda data: calls.append(data) or "synthetic original-page OCR"
    data = make_pdf(1)
    options = dict(resource_recovery=recovery, ocr_enabled=ocr_enabled, ocr=ocr)
    if error is not None:
        with pytest.raises(error):
            build(data, **options)
        assert calls == []
    else:
        source = build(data, **options)
        assert calls == [b"original-page-image"]
        assert source["normalized_text"] == "【第 1 页】\nsynthetic original-page OCR"
        assert source["normalized_text"].encode("utf-8")
        assert source["segments"][0]["quality"]["extraction_mode"] == "ocr"
        assert source["coverage"]["processed_pages"] == [1]
