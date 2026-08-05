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
from collections.abc import Callable
from pathlib import Path

import pypdf

log = logging.getLogger(__name__)
PdfSource = bytes | Path


class PdfExtractError(ValueError):
    """PDF 提取失败（损坏/加密/无可提取文本/超限等），调用方映射为 400。"""


class PdfOcrUnavailableError(PdfExtractError):
    """需要 OCR 但 OCR 依赖未安装（库开了 ocr_enabled 但引擎不可用）。"""


def _meaningful_char_count(text: str) -> int:
    """非空白字符数——用于判断一页是否含有效文字层。"""
    return len("".join(text.split()))


def _page_has_visual_content(page: object) -> bool:
    """Return whether pypdf exposes an image or XObject on the page."""
    try:
        if bool(getattr(page, "images", ())):
            return True
    except Exception:  # noqa: BLE001 - malformed page resources must not block text extraction
        pass
    try:
        resources = page.get("/Resources")
        return bool(resources and resources.get("/XObject"))
    except Exception:  # noqa: BLE001 - test doubles and malformed resources are text-only
        return False


def _open_reader(data: PdfSource):
    """打开 PDF（独立函数便于测试 monkeypatch）。"""
    return pypdf.PdfReader(data if isinstance(data, Path) else io.BytesIO(data))


def _render_page_png(data: PdfSource, page_index: int, dpi: int) -> bytes:
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
            bitmap = page.render(scale=dpi / 72.0)
            pil_image = bitmap.to_pil()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            pdf.close()
    except ImportError:
        # 渲染期才暴露的缺依赖（如 Pillow 未装，to_pil 失败）同样按“需装 OCR”处理
        raise PdfOcrUnavailableError(
            "PDF 渲染依赖未安装（pypdfium2/Pillow）；请执行 pip install -e \".[ocr]\""
        ) from None
    except Exception:  # noqa: BLE001  渲染失败转用户可读错误（消息不含内部栈/正文）
        raise PdfExtractError(f"PDF 第 {page_index + 1} 页渲染失败") from None


def _extract_pdf_parts(
    data: PdfSource,
    *,
    ocr_enabled: bool,
    ocr: Callable[[bytes], str] | None,
    min_text_chars: int,
    render_dpi: int,
    max_ocr_pages: int,
) -> str:
    try:
        reader = _open_reader(data)
        pages = list(reader.pages)
    except Exception:  # noqa: BLE001  —— 不向外暴露内部细节（可能含路径/正文）
        raise PdfExtractError("PDF 解析失败（文件可能损坏或加密）") from None

    parts: list[tuple[int, str, str]] = []
    ocr_pages_used = 0

    for idx, page in enumerate(pages):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001  单页抽取失败按图片页处理，不连累整份
            text = ""

        meaningful_chars = _meaningful_char_count(text)
        embedded_images: list[bytes] = []
        try:
            embedded_images = [
                image.data
                for image in getattr(page, "images", ())
                if isinstance(getattr(image, "data", None), bytes)
                and image.data
            ]
        except Exception:  # noqa: BLE001 - malformed image resources do not block text extraction
            embedded_images = []

        has_visual_content = _page_has_visual_content(page)
        if meaningful_chars >= min_text_chars and not has_visual_content:
            parts.append((idx, text, "native"))
            continue

        if meaningful_chars >= min_text_chars:
            if not ocr_enabled or ocr is None or not embedded_images:
                parts.append((idx, text, "native_visual_unparsed"))
                continue
            if ocr_pages_used >= max_ocr_pages:
                raise PdfExtractError(
                    f"PDF visual OCR pages exceed limit {max_ocr_pages}; processing stopped"
                )
            ocr_pages_used += 1
            image_texts = [
                value
                for image_data in embedded_images
                if (value := (ocr(image_data) or "").strip())
                and _meaningful_char_count(value) > 0
            ]
            merged = "\n".join([text, *image_texts])
            parts.append(
                (
                    idx,
                    merged,
                    "mixed" if image_texts else "native_visual_unparsed",
                )
            )
            continue

        # 低文字页：可能是扫描图片页，也可能只有少量文字（如“审批通过”）——短文字不能丢
        if not ocr_enabled:
            if text:  # 保留已有短文字；纯空白/图片页才跳过
                mode = "native_visual_unparsed" if has_visual_content else "native_short"
                parts.append((idx, text, mode))
            continue
        if ocr is None:
            raise PdfOcrUnavailableError(
                "PDF 含扫描页需要 OCR，但 OCR 依赖未安装；请执行 pip install -e \".[ocr]\""
            )
        if ocr_pages_used >= max_ocr_pages:
            raise PdfExtractError(
                f"PDF 需 OCR 的扫描页超过上限 {max_ocr_pages} 页，已停止处理"
            )

        png = _render_page_png(data, idx, render_dpi)
        ocr_pages_used += 1
        ocr_text = (ocr(png) or "").strip()
        del png  # 立即释放该页 PNG 引用
        # 合并原短文字与 OCR 结果；OCR 为空也保留原文字，不让短文字页丢内容
        merged = "\n".join(
            t for t in (text, ocr_text) if _meaningful_char_count(t) > 0
        )
        if merged:
            mode = "mixed" if text else "ocr"
            parts.append((idx, merged, mode))

    if not parts:
        if ocr_enabled:
            raise PdfExtractError("PDF OCR 后仍无可识别文本")
        raise PdfExtractError("PDF 无可提取文本；如为扫描件，请在知识库开启图片 OCR")

    return parts


def extract_pdf_text(
    data: PdfSource,
    *,
    ocr_enabled: bool,
    ocr: Callable[[bytes], str] | None,
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
        f"【第 {idx + 1} 页】\n{text}" for idx, text, _mode in parts
    )


def build_pdf_source(
    data: PdfSource,
    *,
    chunk_size: int,
    chunk_overlap: int,
    ocr_enabled: bool,
    ocr: Callable[[bytes], str] | None,
    min_text_chars: int,
    render_dpi: int,
    max_ocr_pages: int,
) -> dict:
    from app.services.splitter import split_structured_text

    parts = _extract_pdf_parts(
        data,
        ocr_enabled=ocr_enabled,
        ocr=ocr,
        min_text_chars=min_text_chars,
        render_dpi=render_dpi,
        max_ocr_pages=max_ocr_pages,
    )
    text_parts: list[str] = []
    chunks: list[dict] = []
    cursor = 0
    for page_index, page_text, extraction_mode in parts:
        segment = f"【第 {page_index + 1} 页】\n{page_text}"
        if text_parts:
            text_parts.append("\n\n")
            cursor += 2
        base_offset = cursor
        text_parts.append(segment)
        cursor += len(segment)
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
            chunks.append({
                **chunk,
                "source_start": base_offset + chunk["source_start"],
                "source_end": base_offset + chunk["source_end"],
            })
    return {"normalized_text": "".join(text_parts), "chunks": chunks}
