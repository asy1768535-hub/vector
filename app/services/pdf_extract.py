"""PDF 逐页文字提取（文字层优先，图片页可选 OCR）。

设计（见 docs/23）：
- `pypdf` 读文字层；单页非空白字符数 >= min_text_chars → 直接用文字层。
- 低文字页：OCR 关 → 跳过；OCR 开但引擎不可用（ocr=None）→ 抛 PdfOcrUnavailableError；
  OCR 开 → 仅对该页用 `pypdfium2` 渲染 PNG，交给现有 `ocr_image` 识别。
- 输出按页码合并，带 `【第 N 页】` 来源标记，保持原页序。
- 只对低文字页渲染，且每次只持有一页 PNG；OCR 页数超 max_ocr_pages 即快速失败，不再渲染。

错误一律转 PdfExtractError（消息不含正文/密钥/内部栈）。
"""
from __future__ import annotations

import io
import logging
import math
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import pypdf

from app.services.pdf_coverage import (
    attach_pdf_coverage_unit,
    create_pdf_coverage_report,
)
from app.services.parser_units import (
    build_ocr_parser_units,
    build_parser_unit,
    ocr_result_text_and_blocks,
    parser_provenance,
)

log = logging.getLogger(__name__)
PdfSource = bytes | Path

# Module-local budgets keep PDF parsing safe without expanding the import/config API.
PDF_MAX_PAGES = 500
PDF_MAX_EMBEDDED_IMAGES_PER_PAGE = 32
PDF_MAX_EMBEDDED_IMAGES = 512
PDF_MAX_EMBEDDED_IMAGE_BYTES = 64 * 1024 * 1024
PDF_MAX_RENDER_PIXELS = 25_000_000
PDF_MAX_RENDERED_PNG_BYTES = 16 * 1024 * 1024
PDF_MAX_NORMALIZED_TEXT_CHARS = 10_000_000
PDF_MAX_OCR_BLOCKS_TOTAL = 50_000
PDF_LOW_RES_IMAGE_MAX_DIMENSION = 350
PDF_LOW_RES_IMAGE_MAX_PIXELS = 80_000
PDF_LOW_RES_RENDER_DPI = 72


class PdfExtractError(ValueError):
    """PDF 提取失败（损坏/加密/无可提取文本/超限等），调用方映射为 400。"""


class PdfResourceLimitError(PdfExtractError):
    """PDF 资源预算超限，错误消息不包含输入内容。"""


class PdfOcrUnavailableError(PdfExtractError):
    """需要 OCR 但 OCR 依赖未安装（库开了 ocr_enabled 但引擎不可用）。"""


def _meaningful_char_count(text: str) -> int:
    """非空白字符数——用于判断一页是否含有效文字层。"""
    return len("".join(text.split()))


def _page_has_visual_content(page: object, *, embedded_image_count: int | None = None) -> bool:
    """Return whether pypdf exposes an image or XObject on the page."""
    if embedded_image_count is None or embedded_image_count > 0:
        return True
    try:
        resources = page.get("/Resources")
        return bool(resources and resources.get("/XObject"))
    except Exception:  # noqa: BLE001 - test doubles and malformed resources are text-only
        return False


def _open_reader(data: PdfSource):
    """打开 PDF（独立函数便于测试 monkeypatch）。"""
    return pypdf.PdfReader(data if isinstance(data, Path) else io.BytesIO(data))


def _resource_limit(kind: str) -> None:
    messages = {
        "page_count": "PDF resource limit exceeded: page count",
        "embedded_image_count": "PDF resource limit exceeded: embedded image count",
        "embedded_image_bytes": "PDF resource limit exceeded: embedded image bytes",
        "page_dimensions": "PDF resource limit exceeded: page dimensions",
        "render_pixels": "PDF resource limit exceeded: rendered pixels",
        "rendered_png_bytes": "PDF resource limit exceeded: rendered image bytes",
        "normalized_text": "PDF resource limit exceeded: normalized text",
        "ocr_blocks": "PDF resource limit exceeded: OCR blocks",
    }
    raise PdfResourceLimitError(messages.get(kind, "PDF resource limit exceeded"))

def _validate_ocr_output_budget(
    value: object,
    *,
    remaining_chars: int,
    remaining_blocks: int,
) -> None:
    if isinstance(value, str):
        if len(value) > remaining_chars:
            _resource_limit("normalized_text")
        return
    if not isinstance(value, list):
        return
    if len(value) > remaining_blocks:
        _resource_limit("ocr_blocks")
    text_chars = 0
    for item in value:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if isinstance(text, str):
            text_chars += len(text)
            if text_chars > remaining_chars:
                _resource_limit("normalized_text")



def _page_dimensions_points(page: object) -> tuple[float, float] | None:
    for box_name in ("mediabox", "cropbox"):
        try:
            box = getattr(page, box_name)
            width = abs(float(box.width))
            height = abs(float(box.height))
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0:
            return width, height
    return None


def _check_render_budget(
    page_dimensions: tuple[float, float] | None,
    dpi: int,
) -> None:
    if page_dimensions is None:
        _resource_limit("page_dimensions")
    if (
        not isinstance(dpi, (int, float))
        or isinstance(dpi, bool)
        or not math.isfinite(dpi)
        or dpi <= 0
    ):
        _resource_limit("render_pixels")
    try:
        width_points, height_points = page_dimensions
    except (TypeError, ValueError):
        _resource_limit("page_dimensions")
    try:
        dimensions_valid = (
            math.isfinite(width_points)
            and math.isfinite(height_points)
            and width_points > 0
            and height_points > 0
        )
    except (TypeError, ValueError, OverflowError):
        dimensions_valid = False
    if not dimensions_valid:
        _resource_limit("page_dimensions")
    try:
        width_pixels = math.ceil(width_points * dpi / 72.0)
        height_pixels = math.ceil(height_points * dpi / 72.0)
    except (OverflowError, ValueError, TypeError):
        _resource_limit("render_pixels")
    if (
        width_pixels <= 0
        or height_pixels <= 0
        or width_pixels * height_pixels > PDF_MAX_RENDER_PIXELS
    ):
        _resource_limit("render_pixels")


class _BoundedBytesIO(io.BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > PDF_MAX_RENDERED_PNG_BYTES:
            _resource_limit("rendered_png_bytes")
        return super().write(data)


def _render_page_png_once(data: PdfSource, page_index: int, dpi: int) -> bytes:
    """用 pypdfium2 把单页渲染成 PNG 字节（懒加载，只渲染该页）。

    缺依赖（pypdfium2/Pillow 未装）→ PdfOcrUnavailableError；
    渲染本身失败 → PdfExtractError。两者都不向外暴露裸 ImportError/内部栈。
    """
    try:
        import pypdfium2 as pdfium  # 懒加载：未装 OCR extra 时不影响基础服务
    except ImportError:
        raise PdfOcrUnavailableError(
            "PDF 渲染依赖未安装（pypdfium2/Pillow）；请执行 pip install -e \".[ocr]\""
        ) from None

    try:
        pdf = pdfium.PdfDocument(str(data) if isinstance(data, Path) else data)
        try:
            page = pdf[page_index]
            get_size = getattr(page, "get_size", None)
            if not callable(get_size):
                _resource_limit("page_dimensions")
            try:
                dimensions = tuple(float(value) for value in get_size())
            except (TypeError, ValueError):
                _resource_limit("page_dimensions")
            if len(dimensions) != 2:
                _resource_limit("page_dimensions")
            page_dimensions = (dimensions[0], dimensions[1])
            _check_render_budget(page_dimensions, dpi)
            bitmap = page.render(scale=dpi / 72.0)
            pil_image = bitmap.to_pil()
            buf = _BoundedBytesIO()
            pil_image.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            pdf.close()
    except ImportError:
        # 渲染期才暴露的缺依赖（如 Pillow 未装，to_pil 失败）同样按“需装 OCR”处理
        raise PdfOcrUnavailableError(
            "PDF 渲染依赖未安装（pypdfium2/Pillow）；请执行 pip install -e \".[ocr]\""
        ) from None
    except PdfResourceLimitError:
        raise
    except Exception:  # noqa: BLE001  渲染失败转用户可读错误（消息不含内部栈/正文）
        raise PdfExtractError(f"PDF 第 {page_index + 1} 页渲染失败") from None


def _render_page_png(data: PdfSource, page_index: int, dpi: int) -> bytes:
    try:
        return _render_page_png_once(data, page_index, dpi)
    except (PdfOcrUnavailableError, PdfResourceLimitError):
        raise
    except PdfExtractError:
        return _render_page_png_once(data, page_index, dpi)


def _page_images(page: object) -> Iterable[Any]:
    try:
        images = getattr(page, "images", ())
    except Exception:  # noqa: BLE001 - malformed image resources do not block text extraction
        return ()
    if images is None:
        return ()
    try:
        iter(images)
    except Exception:  # noqa: BLE001 - malformed image resources do not block text extraction
        return ()
    return images


def _positive_image_size(value: object) -> int | None:
    try:
        if hasattr(value, "get_object"):
            value = value.get_object()
        if isinstance(value, bool):
            return None
        size = int(value)  # type: ignore[arg-type]
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return size if size > 0 else None


def _embedded_image_size_hint(image: object) -> int | None:
    for attribute in ("encoded_size", "stream_length", "data_length"):
        try:
            size = _positive_image_size(getattr(image, attribute, None))
        except Exception:  # noqa: BLE001 - optional metadata must not block extraction
            size = None
        if size is not None:
            return size
    try:
        reference = getattr(image, "indirect_reference", None)
        stream = reference.get_object() if reference is not None else None
        if stream is None:
            return None
        size = _positive_image_size(stream.get("/Length"))
        if size is not None:
            return size
        raw_data = getattr(stream, "_data", None)
        return len(raw_data) if isinstance(raw_data, bytes) and raw_data else None
    except Exception:  # noqa: BLE001 - malformed stream metadata falls back to actual bytes
        return None

def _embedded_image_dimensions(image: object) -> tuple[int, int] | None:
    try:
        reference = getattr(image, "indirect_reference", None)
        stream = reference.get_object() if reference is not None else None
        if stream is None and hasattr(image, "get"):
            stream = image
        if stream is None:
            return None
        width = _positive_image_size(stream.get("/Width"))
        height = _positive_image_size(stream.get("/Height"))
        if width is not None and height is not None:
            return width, height
    except Exception:  # noqa: BLE001 - malformed image metadata falls back safely
        return None
    return None


def _resolve_page_render_dpi(page: object, configured_dpi: int) -> int:
    if (
        not isinstance(configured_dpi, (int, float))
        or isinstance(configured_dpi, bool)
        or not math.isfinite(configured_dpi)
        or configured_dpi <= PDF_LOW_RES_RENDER_DPI
    ):
        return int(configured_dpi) if (
            isinstance(configured_dpi, (int, float))
            and not isinstance(configured_dpi, bool)
            and math.isfinite(configured_dpi)
        ) else configured_dpi  # type: ignore[return-value]

    try:
        images = _page_images(page)
        iterator = iter(images)
    except Exception:  # noqa: BLE001 - page image access failures fall back to configured DPI
        return int(configured_dpi)

    seen_images = 0
    while True:
        try:
            image = next(iterator)
        except StopIteration:
            break
        except Exception:  # noqa: BLE001 - malformed resources fall back safely
            return int(configured_dpi)
        seen_images += 1
        if seen_images > PDF_MAX_EMBEDDED_IMAGES_PER_PAGE:
            return int(configured_dpi)
        dims = _embedded_image_dimensions(image)
        if dims is None:
            return int(configured_dpi)
        width, height = dims
        if (
            width > PDF_LOW_RES_IMAGE_MAX_DIMENSION
            or height > PDF_LOW_RES_IMAGE_MAX_DIMENSION
            or (width * height) > PDF_LOW_RES_IMAGE_MAX_PIXELS
        ):
            return int(configured_dpi)

    if seen_images == 0:
        return int(configured_dpi)

    return PDF_LOW_RES_RENDER_DPI


def _scan_embedded_images(
    images: Iterable[Any],
    totals: list[int],
    on_image: Callable[[bytes], None] | None = None,
) -> int:
    try:
        iterator = iter(images)
    except Exception:  # noqa: BLE001 - malformed image resources do not block text extraction
        return 0

    seen_on_page = 0
    while True:
        try:
            image = next(iterator)
        except StopIteration:
            return seen_on_page
        except Exception:  # noqa: BLE001 - malformed image resources do not block text extraction
            return seen_on_page
        seen_on_page += 1
        if seen_on_page > PDF_MAX_EMBEDDED_IMAGES_PER_PAGE:
            _resource_limit("embedded_image_count")
        if totals[0] >= PDF_MAX_EMBEDDED_IMAGES:
            _resource_limit("embedded_image_count")
        totals[0] += 1
        if on_image is None:
            continue
        size_hint = _embedded_image_size_hint(image)
        if (
            size_hint is not None
            and size_hint > PDF_MAX_EMBEDDED_IMAGE_BYTES - totals[1]
        ):
            _resource_limit("embedded_image_bytes")
        try:
            image_data = getattr(image, "data", None)
        except Exception:  # noqa: BLE001 - malformed image resources do not block text extraction
            return seen_on_page
        if not isinstance(image_data, bytes) or not image_data:
            continue
        if len(image_data) > PDF_MAX_EMBEDDED_IMAGE_BYTES - totals[1]:
            _resource_limit("embedded_image_bytes")
        totals[1] += len(image_data)
        on_image(image_data)


def _reader_page_items(reader: object) -> object:
    try:
        pages = getattr(reader, "pages")
    except Exception:  # noqa: BLE001 - do not expose parser internals
        raise PdfExtractError("PDF 解析失败（文件可能损坏或加密）") from None

    page_count = None
    try:
        root = getattr(reader, "root_object")
        page_tree = root.get("/Pages")
        if page_tree is not None and hasattr(page_tree, "get_object"):
            page_tree = page_tree.get_object()
        count = page_tree.get("/Count") if page_tree is not None else None
        if count is not None:
            page_count = int(count)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        page_count = None

    if page_count is None:
        try:
            page_count = len(pages)  # type: ignore[arg-type]
        except (TypeError, AttributeError):
            page_count = None

    if page_count is None:
        def bounded_items():
            try:
                iterator = iter(pages)
                for index, page in enumerate(iterator):
                    if index >= PDF_MAX_PAGES:
                        _resource_limit("page_count")
                    yield index, page
            except PdfResourceLimitError:
                raise
            except Exception:  # noqa: BLE001 - do not expose parser internals
                raise PdfExtractError("PDF 解析失败（文件可能损坏或加密）") from None

        return bounded_items()

    if page_count < 0:
        raise PdfExtractError("PDF 解析失败（文件可能损坏或加密）")
    if page_count > PDF_MAX_PAGES:
        _resource_limit("page_count")

    def indexed_items():
        for index in range(page_count):
            try:
                yield index, pages[index]  # type: ignore[index]
            except Exception:  # noqa: BLE001 - do not expose parser internals
                raise PdfExtractError("PDF 解析失败（文件可能损坏或加密）") from None

    return indexed_items()


def _extract_pdf_parts(
    data: PdfSource,
    *,
    ocr_enabled: bool,
    ocr: Callable[[bytes], Any] | None,
    min_text_chars: int,
    render_dpi: int,
    max_ocr_pages: int,
    coverage_state: dict[str, Any] | None = None,
) -> list[tuple[int, str, str, list[dict[str, Any]]]]:
    try:
        reader = _open_reader(data)
        page_items = _reader_page_items(reader)
    except PdfResourceLimitError:
        raise
    except Exception:  # noqa: BLE001  —— 不向外暴露内部细节（可能含路径/正文）
        raise PdfExtractError("PDF 解析失败（文件可能损坏或加密）") from None

    parts: list[tuple[int, str, str, list[dict[str, Any]]]] = []
    ocr_pages_used = 0
    image_totals = [0, 0]
    text_chars = 0
    block_count = 0
    visited_pages = 0
    unprocessed_visual_pages: list[int] = []

    def append_part(index: int, value: str, mode: str, blocks: list[dict[str, Any]]) -> None:
        nonlocal text_chars, block_count
        text_chars += len(value)
        block_count += len(blocks)
        if text_chars > PDF_MAX_NORMALIZED_TEXT_CHARS:
            _resource_limit("normalized_text")
        if block_count > PDF_MAX_OCR_BLOCKS_TOTAL:
            _resource_limit("ocr_blocks")
        parts.append((index, value, mode, blocks))
        if mode == "native_visual_unparsed":
            unprocessed_visual_pages.append(index + 1)


    for idx, page in page_items:
        visited_pages = idx + 1
        try:
            text = (page.extract_text() or "").replace("\x00", "").strip()
        except Exception:  # noqa: BLE001  单页抽取失败按图片页处理，不连累整份
            text = ""

        if len(text) > PDF_MAX_NORMALIZED_TEXT_CHARS - text_chars:
            _resource_limit("normalized_text")
        meaningful_chars = _meaningful_char_count(text)
        image_values = _page_images(page)
        image_texts: list[str] = []
        ocr_blocks: list[dict[str, Any]] = []
        embedded_ocr_started = False
        embedded_images_attempted = 0
        embedded_images_with_text = 0

        def ocr_embedded_image(image_data: bytes) -> None:
            nonlocal embedded_ocr_started, ocr_pages_used
            nonlocal embedded_images_attempted, embedded_images_with_text
            if not embedded_ocr_started:
                if ocr_pages_used >= max_ocr_pages:
                    raise PdfExtractError(
                        f"PDF visual OCR pages exceed limit {max_ocr_pages}; processing stopped"
                    )
                ocr_pages_used += 1
                embedded_ocr_started = True
            embedded_images_attempted += 1
            try:
                raw_result = ocr(image_data)  # type: ignore[misc]
                _validate_ocr_output_budget(
                    raw_result,
                    remaining_chars=(
                        PDF_MAX_NORMALIZED_TEXT_CHARS
                        - text_chars
                        - sum(map(len, image_texts))
                    ),
                    remaining_blocks=(
                        PDF_MAX_OCR_BLOCKS_TOTAL - block_count - len(ocr_blocks)
                    ),
                )
                value, blocks = ocr_result_text_and_blocks(raw_result)
            except PdfResourceLimitError:
                raise
            except Exception:  # noqa: BLE001 - provider details must not enter job errors
                raise PdfExtractError("PDF OCR 识别失败") from None
            value = value.replace("\x00", "")
            if value and _meaningful_char_count(value) > 0:
                image_texts.append(value)
                ocr_blocks.extend(blocks)
                embedded_images_with_text += 1

        should_try_embedded_ocr = (
            meaningful_chars >= min_text_chars
            and ocr_enabled
            and ocr is not None
        )
        image_count = _scan_embedded_images(
            image_values,
            image_totals,
            ocr_embedded_image if should_try_embedded_ocr else None,
        )
        has_visual_content = _page_has_visual_content(
            page,
            embedded_image_count=image_count,
        )
        embedded_images_fully_covered = (
            embedded_ocr_started
            and embedded_images_attempted == image_count
            and embedded_images_with_text == image_count
        )

        if meaningful_chars >= min_text_chars:
            if not has_visual_content:
                append_part(idx, text, "native", [])
                continue
            if not embedded_images_fully_covered:
                append_part(idx, "\n".join([text, *image_texts]), "native_visual_unparsed", ocr_blocks)
                continue
            merged = "\n".join([text, *image_texts])
            append_part(
                idx, merged,
                "mixed" if image_texts else "native_visual_unparsed",
                ocr_blocks,
            )
            continue

        # 低文字页：可能是扫描图片页，也可能只有少量文字（如“审批通过”）——短文字不能丢
        if not ocr_enabled:
            if text:  # 保留已有短文字；纯空白/图片页才跳过
                mode = "native_visual_unparsed" if has_visual_content else "native_short"
                append_part(idx, text, mode, [])
            elif has_visual_content:
                unprocessed_visual_pages.append(idx + 1)
            continue
        if ocr is None:
            raise PdfOcrUnavailableError(
                "PDF 含扫描页需要 OCR，但 OCR 依赖未安装；请执行 pip install -e \".[ocr]\""
            )
        if ocr_pages_used >= max_ocr_pages:
            raise PdfExtractError(
                f"PDF 需 OCR 的扫描页超过上限 {max_ocr_pages} 页，已停止处理"
            )

        page_dimensions = _page_dimensions_points(page)
        effective_render_dpi = _resolve_page_render_dpi(page, render_dpi)
        if page_dimensions is not None:
            _check_render_budget(page_dimensions, effective_render_dpi)
        png = _render_page_png(data, idx, effective_render_dpi)
        ocr_pages_used += 1
        try:
            raw_result = ocr(png)
            _validate_ocr_output_budget(
                raw_result,
                remaining_chars=PDF_MAX_NORMALIZED_TEXT_CHARS - text_chars - len(text),
                remaining_blocks=PDF_MAX_OCR_BLOCKS_TOTAL - block_count,
            )
            ocr_text, ocr_blocks = ocr_result_text_and_blocks(raw_result)
        except PdfResourceLimitError:
            raise
        except Exception:  # noqa: BLE001 - provider details must not enter job errors
            raise PdfExtractError("PDF OCR 识别失败") from None
        ocr_text = ocr_text.replace("\x00", "")
        del png
        # 合并原短文字与 OCR 结果；OCR 为空也保留原文字，不让短文字页丢内容
        merged = "\n".join(
            t for t in (text, ocr_text) if _meaningful_char_count(t) > 0
        )
        if merged:
            mode = (
                "mixed" if text and _meaningful_char_count(ocr_text) > 0
                else "native_visual_unparsed" if text and has_visual_content
                else "native_short" if text
                else "ocr"
            )
            append_part(idx, merged, mode, ocr_blocks)
        elif has_visual_content:
            unprocessed_visual_pages.append(idx + 1)

    if not parts:
        if ocr_enabled:
            raise PdfExtractError("PDF OCR 后仍无可识别文本")
        raise PdfExtractError("PDF 无可提取文本；如为扫描件，请在知识库开启图片 OCR")

    if coverage_state is not None:
        coverage_state["total_pages"] = visited_pages or None
        coverage_state["unprocessed_visual_pages"] = sorted(set(unprocessed_visual_pages))
    return parts


def extract_pdf_text(
    data: PdfSource,
    *,
    ocr_enabled: bool,
    ocr: Callable[[bytes], Any] | None,
    min_text_chars: int,
    render_dpi: int,
    max_ocr_pages: int,
) -> str:
    """逐页提取并合并 PDF 文本。返回带 `【第 N 页】` 标记的正文。

    抛 PdfOcrUnavailableError（需 OCR 但引擎缺）/ PdfExtractError（损坏、超限、最终无文本）。
    """
    parts = _extract_pdf_parts(
        data,
        ocr_enabled=ocr_enabled,
        ocr=ocr,
        min_text_chars=min_text_chars,
        render_dpi=render_dpi,
        max_ocr_pages=max_ocr_pages,
    )

    return "\n\n".join(
        f"【第 {idx + 1} 页】\n{text}" for idx, text, _mode, _blocks in parts
    )


def build_pdf_source(
    data: PdfSource,
    *,
    chunk_size: int,
    chunk_overlap: int,
    ocr_enabled: bool,
    ocr: Callable[[bytes], Any] | None,
    min_text_chars: int,
    render_dpi: int,
    max_ocr_pages: int,
    preflight_report: Mapping[str, Any] | None = None,
) -> dict:
    from app.services.splitter import split_structured_text

    coverage_state: dict[str, Any] = {}
    preflight_total_pages = (
        preflight_report.get("total_pages")
        if isinstance(preflight_report, Mapping)
        else None
    )
    has_complete_preflight = (
        isinstance(preflight_report, Mapping)
        and preflight_report.get("status") == "complete"
        and preflight_report.get("page_count_known") is True
        and isinstance(preflight_total_pages, int)
        and not isinstance(preflight_total_pages, bool)
        and preflight_total_pages >= 1
    )
    if has_complete_preflight:
        coverage_state["preflight_complete"] = True
        coverage_state["preflight_total_pages"] = preflight_total_pages
    parts = _extract_pdf_parts(
        data,
        ocr_enabled=ocr_enabled,
        ocr=ocr,
        min_text_chars=min_text_chars,
        render_dpi=render_dpi,
        max_ocr_pages=max_ocr_pages,
        coverage_state=coverage_state,
    )
    rendered_segments = [
        f"【第 {page_index + 1} 页】\n{page_text}"
        for page_index, page_text, _mode, _blocks in parts
    ]
    normalized_text = "\n\n".join(rendered_segments)
    base_offsets: list[int] = []
    cursor = 0
    for index, segment_text in enumerate(rendered_segments):
        if index:
            cursor += 2
        base_offsets.append(cursor)
        cursor += len(segment_text)
    chunks: list[dict] = []
    segments: list[dict] = []
    parser = parser_provenance(
        "pypdf",
        "v1",
        {
            "min_text_chars": min_text_chars,
            "render_dpi": render_dpi,
            "max_ocr_pages": max_ocr_pages,
        },
    )
    for (
        (page_index, page_text, extraction_mode, ocr_blocks),
        segment,
        base_offset,
    ) in zip(parts, rendered_segments, base_offsets, strict=True):
        text_offset = base_offset + len(segment) - len(page_text)
        parser_extraction_mode = (
            "unparsed" if extraction_mode == "native_visual_unparsed"
            else "native" if extraction_mode == "native_short"
            else extraction_mode
        )
        quality = {
            "extraction_mode": parser_extraction_mode,
            "native_text_present": bool(page_text.strip()) and extraction_mode != "ocr",
            "visual_content_unparsed": extraction_mode == "native_visual_unparsed",
            "ocr_blocks": ocr_blocks,
        }
        if extraction_mode == "native_visual_unparsed":
            quality["unparsed_reason"] = "native_visual_content_without_ocr"
        location = {
            "type": "page",
            "page": page_index + 1,
            "extraction_mode": extraction_mode,
        }
        segment_record = {
            "kind": "prose",
            "text": page_text,
            "source_kind": "pdf",
            "ordinal": len(segments),
            "unit_key": f"pdf:page:{page_index + 1}",
            "parser": parser,
            "location": location,
            "quality": quality,
        }
        segment_record["parser_unit"] = build_parser_unit(
            source_kind="pdf",
            unit_kind="section",
            ordinal=segment_record["ordinal"],
            unit_key=segment_record["unit_key"],
            parser=parser,
            location=location,
            source_text=normalized_text,
            source_start=text_offset,
            source_end=text_offset + len(page_text),
            quality={
                "extraction_mode": parser_extraction_mode,
                "native_text_present": quality["native_text_present"],
                "visual_content_unparsed": quality["visual_content_unparsed"],
                **(
                    {"unparsed_reason": quality["unparsed_reason"]}
                    if "unparsed_reason" in quality else {}
                ),
            },
        )
        if ocr_blocks and extraction_mode in {"ocr", "mixed"}:
            segment_record["structured_units"] = build_ocr_parser_units(
                ocr_blocks,
                source_kind="pdf",
                parser=parser,
                parent_key=segment_record["unit_key"],
                extraction_mode=extraction_mode,
                unit_text=page_text,
                normalized_text=normalized_text,
                normalized_offset=text_offset,
                page=page_index + 1,
            )
        segments.append(segment_record)
        for chunk in split_structured_text(
            segment,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            splitter="text",
            base_location={
                "type": "page",
                "page": page_index + 1,
                "extraction_mode": extraction_mode,
            },
        ):
            output_chunk = {
                **chunk,
                "source_start": base_offset + chunk["source_start"],
                "source_end": base_offset + chunk["source_end"],
            }
            if chunk.get("source_ranges"):
                output_chunk["source_ranges"] = [
                    {
                        **source_range,
                        "start": base_offset + source_range["start"],
                        "end": base_offset + source_range["end"],
                    }
                    for source_range in chunk["source_ranges"]
                ]
            chunks.append(output_chunk)
    processed_pages = [page_index + 1 for page_index, *_rest in parts]
    preflight_total_pages = coverage_state.get("preflight_total_pages")
    total_pages = (
        preflight_total_pages
        if coverage_state.get("preflight_complete")
        else coverage_state.get("total_pages")
    )
    unprocessed_visual_pages = coverage_state.get("unprocessed_visual_pages") or []
    if unprocessed_visual_pages:
        coverage_status = "partial"
    elif (
        coverage_state.get("preflight_complete")
        and isinstance(total_pages, int)
        and processed_pages == list(range(1, total_pages + 1))
    ):
        coverage_status = "complete"
    else:
        coverage_status = "unknown"
    coverage = create_pdf_coverage_report(
        status=coverage_status,
        total_pages=total_pages,
        processed_pages=processed_pages,
        unprocessed_visual_pages=unprocessed_visual_pages,
        skipped_visual_block_count=len(unprocessed_visual_pages),
        reasons=(
            ["visual_content_without_ocr"] if unprocessed_visual_pages else []
        ),
    )
    attach_pdf_coverage_unit(segments, coverage)
    return {
        "normalized_text": normalized_text,
        "chunks": chunks,
        "segments": segments,
        "coverage": coverage,
    }
