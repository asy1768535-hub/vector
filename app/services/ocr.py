"""图片 OCR：用 RapidOCR(onnxruntime) 抽图片里的文字（中文友好、纯 pip、无系统依赖）。

用于把 docx 内嵌截图 / 扫描件里的文字也纳入检索（默认按库关闭，需在库上开 ocr_enabled）。
设计：引擎懒加载且只建一次（初始化较重）；任何失败都返回空串、绝不抛，不阻断入库。
RapidOCR 未安装时 is_available() 返回 False，调用方据此决定是否走 OCR。
"""
from __future__ import annotations

import io
import logging
import re
import threading
import warnings
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

_engine = None
_lock = threading.Lock()
_run_lock = threading.Lock()

# Module-local limits keep OCR safety independent from import configuration.
OCR_MAX_IMAGE_PIXELS = 25_000_000
OCR_MAX_IMAGE_WIDTH = 10_000
OCR_MAX_IMAGE_HEIGHT = 10_000
OCR_MAX_BLOCKS = 4_096
OCR_MAX_TEXT_CHARS = 200_000
# 仅针对独立的数字 OCR 块规整小数点后空格（如 '66. 02' -> '66.02'），不触碰包含文字的段落或编号（如 '1. 2'、'1. 2024'）
_ISOLATED_DECIMAL_NUMBER_RE = re.compile(
    r"^[+-]?\s*(?:\d{1,3}(?:,\d{3})+|\d{2,}|0)\.\s+\d+[%‰]?$"
)


class OcrResourceLimitError(ValueError):
    """Decoded image or OCR output exceeded the local resource budget."""


def _resource_limit(name: str) -> None:
    raise OcrResourceLimitError(f"OCR resource limit exceeded: {name}")


def is_available() -> bool:
    """RapidOCR 是否可用（依赖已安装）。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _get_engine():
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR
                try:
                    import rapidocr_onnxruntime.utils as ocr_utils
                    has_session_options = hasattr(ocr_utils, "SessionOptions")
                except Exception:
                    ocr_utils = None
                    has_session_options = False

                if has_session_options and ocr_utils is not None:
                    orig_session_options = ocr_utils.SessionOptions

                    def _configured_session_options():
                        opts = orig_session_options()
                        if settings.ocr_intra_op_num_threads is not None:
                            opts.intra_op_num_threads = int(settings.ocr_intra_op_num_threads)
                        if settings.ocr_inter_op_num_threads is not None:
                            opts.inter_op_num_threads = int(settings.ocr_inter_op_num_threads)
                        return opts

                    ocr_utils.SessionOptions = _configured_session_options
                    try:
                        _engine = RapidOCR(
                            intra_op_num_threads=settings.ocr_intra_op_num_threads,
                            inter_op_num_threads=settings.ocr_inter_op_num_threads,
                        )
                    finally:
                        ocr_utils.SessionOptions = orig_session_options
                else:
                    _engine = RapidOCR(
                        intra_op_num_threads=settings.ocr_intra_op_num_threads,
                        inter_op_num_threads=settings.ocr_inter_op_num_threads,
                    )
    return _engine


def _box_to_bbox(box: Any) -> list[float] | None:
    try:
        points = [(float(point[0]), float(point[1])) for point in box]
    except (TypeError, ValueError, IndexError):
        return None
    if not points:
        return None
    xs, ys = zip(*points)
    return [min(xs), min(ys), max(xs), max(ys)]


def _image_dimensions(image: Any) -> tuple[int, int] | None:
    try:
        shape = image.shape
        height, width = int(shape[0]), int(shape[1])
    except (AttributeError, TypeError, ValueError, IndexError):
        return None
    if height <= 0 or width <= 0:
        return None
    return width, height


def _check_image_dimensions(dimensions: tuple[int, int] | None) -> None:
    if dimensions is None:
        return
    width, height = dimensions
    if width > OCR_MAX_IMAGE_WIDTH:
        _resource_limit("image width")
    if height > OCR_MAX_IMAGE_HEIGHT:
        _resource_limit("image height")
    if width * height > OCR_MAX_IMAGE_PIXELS:
        _resource_limit("image pixels")


def _header_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read image dimensions without decoding the full raster."""
    try:
        from PIL import Image as PILImage
        from PIL import UnidentifiedImageError
    except ImportError:
        return None

    try:

        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(io.BytesIO(data)) as image:
                dimensions = tuple(int(value) for value in image.size)
    except (PILImage.DecompressionBombError, PILImage.DecompressionBombWarning):
        raise OcrResourceLimitError("OCR resource limit exceeded: image pixels") from None
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    return dimensions


def ocr_image_blocks(data: bytes) -> list[dict[str, Any]]:
    """Return OCR lines with text, bounding box, and confidence metadata."""
    try:
        _check_image_dimensions(_header_image_dimensions(data))

        import cv2
        import numpy as np

        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return []
        dimensions = _image_dimensions(arr)
        _check_image_dimensions(dimensions)
        with _run_lock:
            result, _ = _get_engine()(arr)
        if not result:
            return []
        try:
            if len(result) > OCR_MAX_BLOCKS:
                _resource_limit("output blocks")
        except TypeError:
            pass
        blocks = []
        text_chars = 0
        for index, (box, text, score) in enumerate(result):
            if index >= OCR_MAX_BLOCKS:
                _resource_limit("output blocks")
            value = str(text).strip()
            if not value:
                continue
            if _ISOLATED_DECIMAL_NUMBER_RE.fullmatch(value):
                value = re.sub(r"(?<=\d)\.\s+(?=\d)", ".", value)
            text_chars += len(value)
            if text_chars > OCR_MAX_TEXT_CHARS:
                _resource_limit("output text")
            try:
                confidence = float(score)
            except (TypeError, ValueError):
                confidence = None
            blocks.append(
                {
                    "text": value,
                    "bbox": _box_to_bbox(box),
                    "confidence": confidence,
                    "parser": "rapidocr",
                }
            )
        return blocks
    except OcrResourceLimitError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("ocr_image_blocks failed: %s", exc)
        return []


def ocr_image(data: bytes) -> str:
    """OCR 一张图片字节流，返回识别文本（按行换行拼接）。失败返回空串，绝不抛。"""
    return "\n".join(block["text"] for block in ocr_image_blocks(data))
