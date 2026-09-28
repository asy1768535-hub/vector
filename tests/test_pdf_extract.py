"""pdf_extract 单元测试（TDD）：逐页文字层/OCR 选择、页序、限制与错误。

不依赖真实 OCR 模型或真实 PDF 渲染：monkeypatch `_open_reader`(替 pypdf)、
`_render_page_png`(替 pypdfium2) 与 ocr callback，只断言调用页、顺序与错误消息。
"""
from __future__ import annotations

import types
import sys

import pytest

from app.services import pdf_extract
from app.services.pdf_extract import (
    PdfExtractError,
    PdfOcrUnavailableError,
    PdfResourceLimitError,
    build_pdf_source,
    extract_pdf_text,
)
from app.services.evidence_write_path import validate_parser_segments


def _fake_reader(page_texts):
    """构造一个假的 PdfReader：pages 为若干带 extract_text() 的假页。"""
    pages = [types.SimpleNamespace(extract_text=(lambda t=t: t)) for t in page_texts]
    return types.SimpleNamespace(pages=pages)


@pytest.fixture
def patch_reader(monkeypatch):
    """返回一个 setter：给定每页文字，patch _open_reader。"""
    def _set(page_texts):
        monkeypatch.setattr(pdf_extract, "_open_reader", lambda data: _fake_reader(page_texts))
    return _set


@pytest.fixture
def render_spy(monkeypatch):
    """记录 _render_page_png 被以哪些 page_index 调用；返回占位 PNG 字节。"""
    calls = []

    def _fake_render(data, page_index, dpi):
        calls.append(page_index)
        return b"PNG:%d" % page_index

    monkeypatch.setattr(pdf_extract, "_render_page_png", _fake_render)
    return calls


def _kw(**over):
    base = dict(ocr_enabled=False, ocr=None, min_text_chars=20, render_dpi=200, max_ocr_pages=50)
    base.update(over)
    return base


def test_text_pages_do_not_call_ocr(patch_reader, render_spy):
    patch_reader(["这是第一页足够长的正文内容应当直接使用文字层", "第二页同样是文字层内容不需要任何识别处理"])
    ocr_calls = []
    out = extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=lambda b: ocr_calls.append(b) or "OCR"))
    assert render_spy == []            # 没有渲染
    assert ocr_calls == []             # 没有 OCR
    assert "第一页" in out and "第二页" in out


def test_pdf_native_text_removes_postgres_nul_bytes(patch_reader, render_spy):
    patch_reader(["有效\x00正文内容足够长"])

    source = build_pdf_source(
        b"x",
        chunk_size=80,
        chunk_overlap=0,
        **_kw(ocr_enabled=False, min_text_chars=1),
    )

    assert "\x00" not in source["normalized_text"]
    assert all("\x00" not in chunk["text"] for chunk in source["chunks"])


def test_text_page_with_embedded_image_uses_ocr_when_enabled(monkeypatch, render_spy):
    page = types.SimpleNamespace(
        extract_text=lambda: "native text with enough characters for the fast path",
        images=[types.SimpleNamespace(data=b"embedded image")],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda data: types.SimpleNamespace(pages=[page]),
    )

    ocr_calls = []
    out = extract_pdf_text(
        b"x",
        **_kw(
            ocr_enabled=True,
            ocr=lambda data: ocr_calls.append(data) or "image region text",
        ),
    )

    assert render_spy == []
    assert ocr_calls == [b"embedded image"]
    assert "image region text" in out


def test_build_pdf_source_records_page_locations(patch_reader, render_spy):
    patch_reader(["第一页足够长的正文内容用于定位", "第二页足够长的正文内容用于定位"])

    source = build_pdf_source(
        b"x", chunk_size=20, chunk_overlap=0, **_kw(ocr_enabled=False, min_text_chars=1)
    )

    assert source["normalized_text"].strip()
    assert source["chunks"]
    pages = {chunk["location"]["page"] for chunk in source["chunks"]}
    assert pages == {1, 2}
    for chunk in source["chunks"]:
        assert chunk["location"]["type"] == "page"
        assert chunk["location"]["extraction_mode"] == "native"
        assert source["normalized_text"][chunk["source_start"]:chunk["source_end"]] == chunk["text"]
        assert "".join(
            source["normalized_text"][item["start"]:item["end"]]
            for item in chunk["source_ranges"]
        ) == chunk["text"]



def test_complete_preflight_promotes_full_native_coverage(patch_reader, render_spy):
    patch_reader(["第一页是完整的文字层内容", "第二页是完整的文字层内容"])

    source = build_pdf_source(
        b"x",
        chunk_size=80,
        chunk_overlap=0,
        preflight_report={
            "status": "complete",
            "page_count_known": True,
            "total_pages": 2,
        },
        **_kw(ocr_enabled=False, min_text_chars=1),
    )

    assert render_spy == []
    assert source["coverage"]["status"] == "complete"
    assert source["coverage"]["total_pages"] == 2
    assert source["coverage"]["processed_pages"] == [1, 2]
    assert source["coverage"]["unprocessed_visual_pages"] == []
def test_scanned_page_uses_ocr_when_enabled(patch_reader, render_spy):
    patch_reader([""])                 # 单张图片页（无文字层）
    out = extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=lambda b: "扫描识别出的文字"))
    assert render_spy == [0]           # 渲染了第 0 页
    assert "扫描识别出的文字" in out


def test_build_pdf_source_records_mixed_extraction_mode(monkeypatch, render_spy):
    page = types.SimpleNamespace(
        extract_text=lambda: "native text with enough characters for the fast path",
        images=[types.SimpleNamespace(data=b"embedded image")],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda data: types.SimpleNamespace(pages=[page]),
    )

    source = build_pdf_source(
        b"x",
        chunk_size=80,
        chunk_overlap=0,
        **_kw(ocr_enabled=True, ocr=lambda b: "image region text"),
    )

    assert render_spy == []
    assert {chunk["location"]["extraction_mode"] for chunk in source["chunks"]} == {"mixed"}


def test_native_visual_page_without_image_regions_is_marked_unparsed(monkeypatch, render_spy):
    page = types.SimpleNamespace(
        extract_text=lambda: "native text with enough characters for the fast path",
        images=[object()],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda data: types.SimpleNamespace(pages=[page]),
    )

    source = build_pdf_source(
        b"x",
        chunk_size=80,
        chunk_overlap=0,
        **_kw(ocr_enabled=True, ocr=lambda _data: "unused"),
    )

    assert render_spy == []
    assert {chunk["location"]["extraction_mode"] for chunk in source["chunks"]} == {
        "native_visual_unparsed"
    }
    assert source["coverage"] == {
        "contract_version": "pdf-coverage-v1",
        "status": "partial",
        "total_pages": 1,
        "processed_pages": [1],
        "unprocessed_visual_pages": [1],
        "skipped_visual_block_count": 1,
        "reasons": ["visual_content_without_ocr"],
    }
    validate_parser_segments(source["segments"])


def test_broken_images_getter_keeps_pure_text_native(monkeypatch, render_spy):
    ocr_calls = []

    class Page:
        def extract_text(self):
            return "native text with enough characters for the fast path"

        @property
        def images(self):
            raise RuntimeError("malformed image resources")

    page = Page()
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )

    assert pdf_extract._scan_embedded_images(pdf_extract._page_images(page), [0, 0]) == 0
    source = build_pdf_source(
        b"x",
        chunk_size=80,
        chunk_overlap=0,
        **_kw(
            ocr_enabled=True,
            ocr=lambda data: ocr_calls.append(data) or "unused",
        ),
    )

    assert render_spy == []
    assert ocr_calls == []
    assert {chunk["location"]["extraction_mode"] for chunk in source["chunks"]} == {"native"}
    assert source["segments"][0]["quality"]["visual_content_unparsed"] is False
    assert source["coverage"]["status"] == "unknown"
    assert source["coverage"]["processed_pages"] == [1]
    assert source["coverage"]["unprocessed_visual_pages"] == []


def test_text_plus_unread_visual_page_reports_partial(monkeypatch, render_spy):
    pages = [
        types.SimpleNamespace(
            extract_text=lambda: "native text with enough characters for the fast path",
            images=[],
        ),
        types.SimpleNamespace(extract_text=lambda: "", images=[object()]),
    ]
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=pages),
    )

    source = build_pdf_source(
        b"x", chunk_size=80, chunk_overlap=0, **_kw(ocr_enabled=False)
    )

    assert render_spy == []
    assert source["coverage"]["status"] == "partial"
    assert source["coverage"]["total_pages"] == 2
    assert source["coverage"]["processed_pages"] == [1]
    assert source["coverage"]["unprocessed_visual_pages"] == [2]


def test_ocr_callback_error_is_sanitized(patch_reader, render_spy):
    patch_reader([""])

    def fail(_data):
        raise RuntimeError("secret provider /private/path")

    with pytest.raises(PdfExtractError, match="PDF OCR") as exc_info:
        extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=fail))
    assert "secret" not in str(exc_info.value)
    assert "/private/path" not in str(exc_info.value)


def test_pdf_normalized_text_budget_is_cumulative(monkeypatch, patch_reader, render_spy):
    monkeypatch.setattr(pdf_extract, "PDF_MAX_NORMALIZED_TEXT_CHARS", 10)
    patch_reader(["abcdef", "ghijkl"])
    with pytest.raises(PdfResourceLimitError, match="normalized text"):
        extract_pdf_text(b"x", **_kw(min_text_chars=1))


def test_pdf_ocr_block_budget_is_cumulative(monkeypatch):
    monkeypatch.setattr(pdf_extract, "PDF_MAX_OCR_BLOCKS_TOTAL", 1)
    page = types.SimpleNamespace(
        extract_text=lambda: "native text with enough characters",
        images=[types.SimpleNamespace(data=b"image")],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )
    blocks = [
        {"text": "one", "bbox": [0, 0, 1, 1]},
        {"text": "two", "bbox": [0, 1, 1, 2]},
    ]
    monkeypatch.setattr(
        pdf_extract,
        "ocr_result_text_and_blocks",
        lambda _value: pytest.fail("OCR output must be rejected before normalization"),
    )
    with pytest.raises(PdfResourceLimitError, match="OCR blocks"):
        extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=lambda _data: blocks))




def test_mixed_pdf_keeps_text_and_ocr_in_page_order(patch_reader, render_spy):
    patch_reader(["第一页是文字层的正文内容足够长可直接使用", ""])
    out = extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=lambda b: "第二页图片识别文字"))
    assert render_spy == [1]           # 只渲染图片页（第 1 页）
    i1, i2 = out.index("第一页"), out.index("第二页图片识别文字")
    assert i1 < i2                     # 保持页序
    assert "【第 1 页】" in out and "【第 2 页】" in out


def test_scanned_pdf_without_ocr_reports_enable_hint(patch_reader, render_spy):
    patch_reader(["", ""])             # 全是图片页，OCR 关闭
    with pytest.raises(PdfExtractError) as ei:
        extract_pdf_text(b"x", **_kw(ocr_enabled=False))
    assert "OCR" in str(ei.value)      # 提示去开启 OCR
    assert render_spy == []            # 关闭时不渲染


def test_ocr_enabled_but_engine_missing_reports_dependency_error(patch_reader, render_spy):
    patch_reader([""])                 # 图片页，OCR 开启但引擎不可用（ocr=None）
    with pytest.raises(PdfOcrUnavailableError):
        extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=None))


def test_blank_page_does_not_fail_other_pages(patch_reader, render_spy):
    patch_reader(["", "第二页有正常文字层内容应当被保留下来用于检索"])
    out = extract_pdf_text(b"x", **_kw(ocr_enabled=False))   # 空白页跳过，文字页保留
    assert "第二页" in out
    assert "【第 2 页】" in out


def test_all_pages_empty_after_ocr_is_error(patch_reader, render_spy):
    patch_reader(["", ""])             # 图片页，OCR 开启但识别全空
    with pytest.raises(PdfExtractError) as ei:
        extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=lambda b: ""))
    assert "无可识别" in str(ei.value) or "仍无" in str(ei.value)


def test_max_ocr_pages_stops_before_rendering_extra_page(patch_reader, render_spy):
    patch_reader(["", "", ""])         # 3 张图片页，但上限 2
    with pytest.raises(PdfExtractError):
        extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=lambda b: "图", max_ocr_pages=2))
    assert len(render_spy) == 2        # 第 3 页未被渲染（提前停）


def test_short_text_page_is_kept_when_ocr_off(patch_reader, render_spy):
    patch_reader(["审批通过"])           # 仅 4 字，低于 min_text_chars，但是真实文字
    out = extract_pdf_text(b"x", **_kw(ocr_enabled=False))
    assert "审批通过" in out             # 短文字页不能被当图片页丢弃
    assert render_spy == []             # OCR 关，不渲染


def test_short_text_page_kept_even_if_ocr_returns_empty(patch_reader, render_spy):
    patch_reader(["审批通过"])
    out = extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=lambda b: ""))
    assert render_spy == [0]            # 仍会尝试 OCR（页面可能含图）
    assert "审批通过" in out             # OCR 为空也保留原短文字


def test_short_text_with_visual_page_and_empty_ocr_is_partial(monkeypatch, render_spy):
    page = types.SimpleNamespace(
        extract_text=lambda: "审批通过",
        images=[object()],
    )
    monkeypatch.setattr(
        pdf_extract, "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )

    source = build_pdf_source(
        b"x", chunk_size=80, chunk_overlap=0,
        **_kw(ocr_enabled=True, ocr=lambda _data: ""),
    )
    assert render_spy == [0]
    assert "审批通过" in source["normalized_text"]
    assert source["coverage"]["status"] == "partial"
    assert source["coverage"]["unprocessed_visual_pages"] == [1]


def test_short_text_merges_with_ocr_result(patch_reader, render_spy):
    patch_reader(["审批通过"])
    out = extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=lambda b: "盖章区识别文字"))
    assert "审批通过" in out and "盖章区识别文字" in out  # 两者合并，互不丢失


def test_render_missing_dependency_maps_to_unavailable(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "pypdfium2", None)  # 模拟未安装 → import 抛 ImportError
    with pytest.raises(PdfOcrUnavailableError):
        pdf_extract._render_page_png(b"x", 0, 200)


def test_render_failure_maps_to_extract_error(monkeypatch):
    import sys

    class _BoomDoc:
        def __init__(self, data):
            raise RuntimeError("native pdfium crashed with /secret/ path")

    fake = types.SimpleNamespace(PdfDocument=_BoomDoc)
    monkeypatch.setitem(sys.modules, "pypdfium2", fake)
    with pytest.raises(PdfExtractError) as ei:
        pdf_extract._render_page_png(b"x", 0, 200)
    assert not isinstance(ei.value, PdfOcrUnavailableError)  # 渲染失败 ≠ 缺依赖
    assert "secret" not in str(ei.value)                     # 不泄漏内部栈


def test_corrupt_pdf_is_clear_error(monkeypatch, render_spy):
    def _boom(data):
        raise ValueError("internal pypdf parse blew up with /secret/ path")
    monkeypatch.setattr(pdf_extract, "_open_reader", _boom)
    with pytest.raises(PdfExtractError) as ei:
        extract_pdf_text(b"x", **_kw())
    # 错误消息不泄漏内部细节
    assert "secret" not in str(ei.value)


def test_page_limit_rejects_before_accessing_any_page(monkeypatch):
    accesses = []

    class Pages:
        def __len__(self):
            return pdf_extract.PDF_MAX_PAGES + 1

        def __iter__(self):
            raise AssertionError("page iteration must not happen")

        def __getitem__(self, index):
            accesses.append(index)
            raise AssertionError("page access must not happen")

    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=Pages()),
    )

    with pytest.raises(PdfResourceLimitError, match="page count") as exc_info:
        extract_pdf_text(b"secret input", **_kw())

    assert accesses == []
    assert "secret input" not in str(exc_info.value)


def test_page_limit_bounds_iteration_when_page_count_is_unavailable(monkeypatch):
    yielded = []

    class Pages:
        def __iter__(self):
            for index in range(pdf_extract.PDF_MAX_PAGES + 2):
                yielded.append(index)
                yield types.SimpleNamespace(
                    extract_text=lambda: "page text with enough characters"
                )

    monkeypatch.setattr(pdf_extract, "PDF_MAX_PAGES", 2)
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=Pages()),
    )

    with pytest.raises(PdfResourceLimitError, match="page count"):
        extract_pdf_text(b"x", **_kw())

    assert yielded == [0, 1, 2]


def test_embedded_images_per_page_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(pdf_extract, "PDF_MAX_EMBEDDED_IMAGES_PER_PAGE", 1)
    page = types.SimpleNamespace(
        extract_text=lambda: "native text with enough characters",
        images=[types.SimpleNamespace(data=b"a"), types.SimpleNamespace(data=b"b")],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )

    with pytest.raises(PdfResourceLimitError, match="embedded image count"):
        extract_pdf_text(b"x", **_kw())


def test_embedded_images_total_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(pdf_extract, "PDF_MAX_EMBEDDED_IMAGES", 1)
    pages = [
        types.SimpleNamespace(
            extract_text=lambda: "native text with enough characters",
            images=[types.SimpleNamespace(data=b"a")],
        ),
        types.SimpleNamespace(
            extract_text=lambda: "native text with enough characters",
            images=[types.SimpleNamespace(data=b"b")],
        ),
    ]
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=pages),
    )

    with pytest.raises(PdfResourceLimitError, match="embedded image count"):
        extract_pdf_text(b"x", **_kw())


def test_embedded_image_bytes_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(pdf_extract, "PDF_MAX_EMBEDDED_IMAGE_BYTES", 3)
    page = types.SimpleNamespace(
        extract_text=lambda: "native text with enough characters",
        images=[types.SimpleNamespace(data=b"abcd")],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )

    with pytest.raises(PdfResourceLimitError, match="embedded image bytes"):
        extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=lambda _data: "unused"))


def test_embedded_image_size_hint_rejects_before_data_access(monkeypatch):
    class Image:
        encoded_size = 4

        @property
        def data(self):
            raise AssertionError("image data must not be decoded before budget check")

    monkeypatch.setattr(pdf_extract, "PDF_MAX_EMBEDDED_IMAGE_BYTES", 3)
    page = types.SimpleNamespace(
        extract_text=lambda: "native text with enough characters",
        images=[Image()],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )

    with pytest.raises(PdfResourceLimitError, match="embedded image bytes"):
        extract_pdf_text(b"x", **_kw(ocr_enabled=True, ocr=lambda _data: "unused"))


def test_partially_recognized_embedded_images_mark_page_unparsed(monkeypatch):
    page = types.SimpleNamespace(
        extract_text=lambda: "native text with enough characters",
        images=[
            types.SimpleNamespace(data=b"recognized"),
            types.SimpleNamespace(data=b"empty"),
        ],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )

    source = build_pdf_source(
        b"x",
        chunk_size=200,
        chunk_overlap=0,
        **_kw(
            ocr_enabled=True,
            ocr=lambda data: "recognized image text" if data == b"recognized" else "",
        ),
    )

    assert "recognized image text" in source["normalized_text"]
    assert source["segments"][0]["location"]["extraction_mode"] == "native_visual_unparsed"
    assert source["coverage"]["status"] == "partial"
    assert source["coverage"]["unprocessed_visual_pages"] == [1]


def test_embedded_image_bytes_are_not_decoded_when_ocr_is_disabled(monkeypatch):
    class Image:
        @property
        def data(self):
            raise AssertionError("image bytes must not be decoded")

    page = types.SimpleNamespace(
        extract_text=lambda: "native text with enough characters",
        images=[Image()],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )

    result = extract_pdf_text(b"x", **_kw(ocr_enabled=False))
    assert "native text" in result


def test_render_pixel_limit_rejects_before_render(monkeypatch):
    monkeypatch.setattr(pdf_extract, "PDF_MAX_RENDER_PIXELS", 100)
    render_calls = []
    page = types.SimpleNamespace(
        extract_text=lambda: "",
        mediabox=types.SimpleNamespace(width=11, height=11),
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )
    monkeypatch.setattr(
        pdf_extract,
        "_render_page_png",
        lambda *args, **kwargs: render_calls.append((args, kwargs)) or b"png",
    )

    with pytest.raises(PdfResourceLimitError, match="rendered pixels"):
        extract_pdf_text(
            b"x",
            **_kw(ocr_enabled=True, ocr=lambda _data: "text", render_dpi=72),
        )

    assert render_calls == []


def test_rendered_png_bytes_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(pdf_extract, "PDF_MAX_RENDERED_PNG_BYTES", 3)

    class Image:
        def save(self, output, format):
            output.write(b"toolong")

    class Bitmap:
        def to_pil(self):
            return Image()

    class Page:
        def get_size(self):
            return 72, 72

        def render(self, *, scale):
            return Bitmap()

    class Document:
        def __getitem__(self, index):
            return Page()

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        types.SimpleNamespace(PdfDocument=lambda _data: Document()),
    )

    with pytest.raises(PdfResourceLimitError, match="rendered image bytes"):
        pdf_extract._render_page_png(
            b"x",
            0,
            72,
        )


def test_pdf_resource_limits_allow_normal_page_and_image_path(monkeypatch):
    image_ocr_calls = []
    page = types.SimpleNamespace(
        extract_text=lambda: "native text with enough characters",
        images=[types.SimpleNamespace(data=b"embedded image")],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )

    result = extract_pdf_text(
        b"x",
        **_kw(
            ocr_enabled=True,
            ocr=lambda data: image_ocr_calls.append(data) or "image text",
        ),
    )

    assert image_ocr_calls == [b"embedded image"]
    assert "image text" in result


def test_positive_image_size_rejects_booleans_and_non_positives():
    assert pdf_extract._positive_image_size(True) is None
    assert pdf_extract._positive_image_size(False) is None
    assert pdf_extract._positive_image_size(0) is None
    assert pdf_extract._positive_image_size(-10) is None
    assert pdf_extract._positive_image_size(100) == 100
    assert pdf_extract._positive_image_size("100") == 100
    assert pdf_extract._positive_image_size(types.SimpleNamespace(get_object=lambda: True)) is None


def test_adaptive_render_dpi_selection():
    small_img = types.SimpleNamespace(
        indirect_reference=types.SimpleNamespace(
            get_object=lambda: {"/Width": 195, "/Height": 102}
        )
    )
    page_small = types.SimpleNamespace(images=[small_img])
    assert pdf_extract._resolve_page_render_dpi(page_small, 200) == 72

    large_img = types.SimpleNamespace(
        indirect_reference=types.SimpleNamespace(
            get_object=lambda: {"/Width": 1700, "/Height": 2178}
        )
    )
    page_large = types.SimpleNamespace(images=[large_img])
    assert pdf_extract._resolve_page_render_dpi(page_large, 200) == 200

    page_mixed = types.SimpleNamespace(images=[small_img, large_img])
    assert pdf_extract._resolve_page_render_dpi(page_mixed, 200) == 200

    page_no_img = types.SimpleNamespace(images=[])
    assert pdf_extract._resolve_page_render_dpi(page_no_img, 200) == 200

    assert pdf_extract._resolve_page_render_dpi(page_small, 60) == 60
    assert pdf_extract._resolve_page_render_dpi(page_small, 72) == 72

    page_corrupt = types.SimpleNamespace(images=[types.SimpleNamespace()])
    assert pdf_extract._resolve_page_render_dpi(page_corrupt, 200) == 200

    # Dual-constraint: 350x200 (70k px <= 80k px) resolves to 72
    img_350x200 = types.SimpleNamespace(
        indirect_reference=types.SimpleNamespace(
            get_object=lambda: {"/Width": 350, "/Height": 200}
        )
    )
    assert pdf_extract._resolve_page_render_dpi(types.SimpleNamespace(images=[img_350x200]), 200) == 72

    # Dual-constraint: 350x350 (122.5k px > 80k px) falls back to configured DPI
    img_350x350 = types.SimpleNamespace(
        indirect_reference=types.SimpleNamespace(
            get_object=lambda: {"/Width": 350, "/Height": 350}
        )
    )
    assert pdf_extract._resolve_page_render_dpi(types.SimpleNamespace(images=[img_350x350]), 200) == 200

    # Dimension threshold: 351x100 (351 > 350) falls back to configured DPI
    img_351x100 = types.SimpleNamespace(
        indirect_reference=types.SimpleNamespace(
            get_object=lambda: {"/Width": 351, "/Height": 100}
        )
    )
    assert pdf_extract._resolve_page_render_dpi(types.SimpleNamespace(images=[img_351x100]), 200) == 200

    # Boolean dimensions fallback to configured DPI
    bool_img = types.SimpleNamespace(
        indirect_reference=types.SimpleNamespace(
            get_object=lambda: {"/Width": True, "/Height": 100}
        )
    )
    assert pdf_extract._resolve_page_render_dpi(types.SimpleNamespace(images=[bool_img]), 200) == 200

    # Embedded image count exceeding limit falls back to configured DPI
    too_many_imgs = [small_img] * (pdf_extract.PDF_MAX_EMBEDDED_IMAGES_PER_PAGE + 1)
    assert pdf_extract._resolve_page_render_dpi(types.SimpleNamespace(images=too_many_imgs), 200) == 200

    # Non-positive / invalid configured_dpi passes through without silent coercion to 72
    assert pdf_extract._resolve_page_render_dpi(page_small, 0) == 0
    assert pdf_extract._resolve_page_render_dpi(page_small, -10) == -10
    assert pdf_extract._resolve_page_render_dpi(page_small, True) is True


def test_render_dpi_budget_rejects_non_positive_and_invalid(monkeypatch):
    page = types.SimpleNamespace(
        extract_text=lambda: "",
        images=[],
        mediabox=types.SimpleNamespace(width=595, height=842),
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )
    for bad_dpi in (0, -10, True):
        with pytest.raises(PdfResourceLimitError, match="rendered pixels"):
            extract_pdf_text(
                b"x",
                **_kw(ocr_enabled=True, ocr=lambda _data: "text", render_dpi=bad_dpi),
            )

def test_adaptive_render_dpi_calls_render_with_resolved_dpi(monkeypatch):
    small_img = types.SimpleNamespace(
        indirect_reference=types.SimpleNamespace(
            get_object=lambda: {"/Width": 195, "/Height": 102}
        )
    )
    page = types.SimpleNamespace(
        extract_text=lambda: "",
        images=[small_img],
        mediabox=types.SimpleNamespace(width=595, height=842),
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda _data: types.SimpleNamespace(pages=[page]),
    )
    rendered_dpis = []
    monkeypatch.setattr(
        pdf_extract,
        "_render_page_png",
        lambda _data, _idx, dpi: rendered_dpis.append(dpi) or b"fake_png",
    )
    res = extract_pdf_text(
        b"x",
        **_kw(ocr_enabled=True, ocr=lambda _data: "recognized text", render_dpi=200),
    )
    assert rendered_dpis == [72]
    assert "recognized text" in res


def test_structure_scanned_table_synthetic_grid():
    import cv2
    import numpy as np
    from app.services.pdf_extract import _structure_scanned_table

    # 1. Non-grid image returns None
    blank = np.full((300, 300), 255, dtype=np.uint8)
    _, blank_png = cv2.imencode(".png", blank)
    assert _structure_scanned_table(blank_png.tobytes(), []) is None

    # 2. Corrupt png returns None
    assert _structure_scanned_table(b"corrupt", []) is None

    # 3. Create a synthetic image with a clean 4x4 grid (5 rows, 4 columns of lines)
    img = np.full((500, 600), 255, dtype=np.uint8)
    y_lines = [50, 100, 150, 200, 250]
    x_lines = [50, 180, 310, 440]
    for y in y_lines:
        cv2.line(img, (x_lines[0], y), (x_lines[-1], y), 0, 2)
    for x in x_lines:
        cv2.line(img, (x, y_lines[0]), (x, y_lines[-1]), 0, 2)

    _, png = cv2.imencode(".png", img)
    png_bytes = png.tobytes()

    blocks = [
        {"text": "Header A", "bbox": [60, 60, 120, 80], "confidence": 0.95},
        {"text": "Header B", "bbox": [190, 60, 250, 80], "confidence": 0.95},
        {"text": "Header C", "bbox": [320, 60, 380, 80], "confidence": 0.95},
        {"text": "Row1 ValA", "bbox": [60, 110, 130, 130], "confidence": 0.92},
        # Crossing block spanning from col 1 across boundary into col 2
        {"text": "ABC123XYZ", "bbox": [200, 110, 370, 130], "confidence": 0.88},
    ]

    res = _structure_scanned_table(png_bytes, blocks)
    assert res is not None
    text, ordered_blocks = res
    assert "Header A | Header B | Header C" in text
    # Crossing block is kept intact with cross_column and uncertain flags, not split by heuristics
    assert any(b.get("text") == "ABC123XYZ" and b.get("cross_column") is True and b.get("uncertain") is True for b in ordered_blocks)
    assert len(ordered_blocks) == 5
    assert "ABC123XYZ" in text


def test_structure_scanned_table_variable_length_and_uncertain_crossing():
    import cv2
    import numpy as np
    from app.services.pdf_extract import _structure_scanned_table

    # 4 rows, 3 columns grid
    img = np.full((500, 600), 255, dtype=np.uint8)
    y_lines = [50, 120, 190, 260, 330]
    x_lines = [50, 200, 380, 560]
    for y in y_lines:
        cv2.line(img, (x_lines[0], y), (x_lines[-1], y), 0, 2)
    for x in x_lines:
        cv2.line(img, (x, y_lines[0]), (x, y_lines[-1]), 0, 2)
    _, png = cv2.imencode(".png", img)
    png_bytes = png.tobytes()

    blocks = [
        {"text": "产权人", "bbox": [60, 60, 180, 90], "confidence": 0.95},
        {"text": "产权证号", "bbox": [210, 60, 360, 90], "confidence": 0.95},
        {"text": "建筑面积m", "bbox": [390, 60, 540, 90], "confidence": 0.95},
        # Row 1: crossing certificate and area (14 chars)
        {"text": "谢列平", "bbox": [60, 130, 180, 160], "confidence": 0.95},
        {"text": "20140062969.59", "bbox": [210, 130, 420, 160], "confidence": 0.90},
        # Row 2: normal length certificate (9 chars)
        {"text": "李四", "bbox": [60, 200, 180, 230], "confidence": 0.95},
        {"text": "201400630", "bbox": [210, 200, 350, 230], "confidence": 0.95},
        {"text": "66.02", "bbox": [390, 200, 500, 230], "confidence": 0.95},
        # Row 3: hyphenated variable-length certificate (11 chars) and blank owner
        {"text": "2014-0011-2", "bbox": [210, 270, 370, 300], "confidence": 0.95},
        {"text": "44.68", "bbox": [390, 270, 500, 300], "confidence": 0.95},
    ]

    res = _structure_scanned_table(png_bytes, blocks)
    assert res is not None
    table_text, ordered_blocks = res

    # 1. Row 1 crossing certificate 20140062969.59 must NOT be split by median length
    assert "20140062969.59" in table_text
    assert "201400629 | 69.59" not in table_text
    crossing_block = next(b for b in ordered_blocks if b.get("text") == "20140062969.59")
    assert crossing_block.get("cross_column") is True
    assert crossing_block.get("uncertain") is True

    # 2. Adjacent area cell in Row 1 has clear reference note rather than empty or hallucinated
    lines = [l.strip() for l in table_text.splitlines() if "|" in l]
    row1_line = [l for l in lines if "谢列平" in l][0]
    assert "20140062969.59 [跨列不确定归属: 跨第2列-第3列, 原图位置: [210, 130, 420, 160]]" in row1_line
    assert "[跨列不确定: 参见第2列]" in row1_line
    assert row1_line.count("20140062969.59") == 1
    # 3. Row 3 has blank owner cell preserved (| 2014-0011-2 | 44.68)
    row3_line = [l for l in lines if "2014-0011-2" in l][0]
    assert row3_line == "| 2014-0011-2 | 44.68"


def test_structure_scanned_table_missing_and_invalid_coordinates():
    import cv2
    import numpy as np
    from app.services.pdf_extract import _structure_scanned_table

    img = np.full((400, 500), 255, dtype=np.uint8)
    for y in [40, 100, 160, 220]:
        cv2.line(img, (40, y), (460, y), 0, 2)
    for x in [40, 180, 320, 460]:
        cv2.line(img, (x, 40), (x, 220), 0, 2)
    _, png = cv2.imencode(".png", img)
    png_bytes = png.tobytes()

    blocks = [
        {"text": "H1", "bbox": [50, 50, 120, 80]},
        {"text": "H2", "bbox": [190, 50, 260, 80]},
        {"text": "H3", "bbox": [330, 50, 400, 80]},
        {"text": "NormalVal", "bbox": [50, 110, 120, 140]},
        # Invalid coordinate types and missing boxes
        {"text": "NoBbox"},
        {"text": "ShortBbox", "bbox": [10, 20]},
        {"text": "NonNumBbox", "bbox": ["a", "b", "c", "d"]},
        {"text": "InvertedBbox", "bbox": [100, 100, 50, 50]},
    ]

    res = _structure_scanned_table(png_bytes, blocks)
    assert res is not None
    text, ordered_blocks = res

    # Zero text loss: all unpositioned and invalid coordinate blocks MUST be preserved
    assert "NoBbox" in text
    assert "ShortBbox" in text
    assert "NonNumBbox" in text
    assert "InvertedBbox" in text
    ordered_texts = [b.get("text") for b in ordered_blocks]
    for expected in ["NoBbox", "ShortBbox", "NonNumBbox", "InvertedBbox"]:
        assert expected in ordered_texts


def test_structure_scanned_table_empty_result_fallback():
    from app.services.pdf_extract import _structure_scanned_table

    # Non-grid image
    import cv2, numpy as np
    blank = np.full((300, 300), 255, dtype=np.uint8)
    _, png = cv2.imencode(".png", blank)
    assert _structure_scanned_table(png.tobytes(), [{"text": "hello", "bbox": [10, 10, 50, 30]}]) is None

    # Empty blocks
    assert _structure_scanned_table(png.tobytes(), []) is None

    # Corrupt png bytes
    assert _structure_scanned_table(b"corrupt_data", [{"text": "hello"}]) is None


def test_build_pdf_source_end_to_end_preserves_cross_column_uncertainty_in_chunks(monkeypatch):
    import cv2
    import numpy as np
    from app.services import pdf_extract
    from app.services.pdf_extract import build_pdf_source

    # 1. Synthetic grid image: 4 rows, 3 columns
    img = np.full((500, 600), 255, dtype=np.uint8)
    y_lines = [50, 120, 190, 260, 330]
    x_lines = [50, 200, 380, 560]
    for y in y_lines:
        cv2.line(img, (x_lines[0], y), (x_lines[-1], y), 0, 2)
    for x in x_lines:
        cv2.line(img, (x, y_lines[0]), (x, y_lines[-1]), 0, 2)
    _, png = cv2.imencode(".png", img)
    png_bytes = png.tobytes()

    # 2. Mock PDF reader with single scanned page
    page = types.SimpleNamespace(
        extract_text=lambda: "",
        images=[],
    )
    monkeypatch.setattr(
        pdf_extract,
        "_open_reader",
        lambda data: types.SimpleNamespace(pages=[page]),
    )
    monkeypatch.setattr(
        pdf_extract,
        "_render_page_png",
        lambda *args, **kwargs: png_bytes,
    )

    # 3. Mock OCR returning structured blocks with crossing certificate and area
    mock_ocr_blocks = [
        {"text": "产权人", "bbox": [60, 60, 180, 90], "confidence": 0.95},
        {"text": "产权证号", "bbox": [210, 60, 360, 90], "confidence": 0.95},
        {"text": "建筑面积m", "bbox": [390, 60, 540, 90], "confidence": 0.95},
        {"text": "谢列平", "bbox": [60, 130, 180, 160], "confidence": 0.95},
        {"text": "20140062969.59", "bbox": [210, 130, 420, 160], "confidence": 0.90},
        {"text": "李四", "bbox": [60, 200, 180, 230], "confidence": 0.95},
        {"text": "201400630", "bbox": [210, 200, 350, 230], "confidence": 0.95},
        {"text": "66.02", "bbox": [390, 200, 500, 230], "confidence": 0.95},
    ]
    source = build_pdf_source(
        b"dummy_pdf_bytes",
        chunk_size=800,
        chunk_overlap=80,
        **_kw(ocr_enabled=True, ocr=lambda b: mock_ocr_blocks),
    )

    # Verify chunks: final retrievable content MUST preserve raw string, coordinates, and uncertainty
    chunks = source["chunks"]
    assert len(chunks) > 0
    all_chunk_text = "\n".join(c["text"] for c in chunks)

    # A. Raw string 20140062969.59 is preserved intact (not split to 201400629 | 69.59)
    assert "20140062969.59" in all_chunk_text
    assert "201400629 | 69.59" not in all_chunk_text

    # B. Original image coordinates are explicitly preserved
    assert "[210, 130, 420, 160]" in all_chunk_text

    # C. Clear uncertainty attribution is preserved in chunk text
    # C. Clear uncertainty attribution is preserved in chunk text and raw string appears only ONCE
    assert "跨列不确定归属" in all_chunk_text
    assert "跨第2列-第3列" in all_chunk_text
    assert "[跨列不确定: 参见第2列]" in all_chunk_text
    assert all_chunk_text.count("20140062969.59") == 1
    assert source["normalized_text"].count("20140062969.59") == 1
    # D. Must NOT present 20140062969.59 as a certain single-column value without notice
    assert "谢列平 | 20140062969.59 |" not in all_chunk_text
