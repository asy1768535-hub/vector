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
        extract_pdf_text(b"x", **_kw())


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
