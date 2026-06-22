"""图片 OCR：用 RapidOCR(onnxruntime) 抽图片里的文字（中文友好、纯 pip、无系统依赖）。

用于把 docx 内嵌截图 / 扫描件里的文字也纳入检索（默认按库关闭，需在库上开 ocr_enabled）。
设计：引擎懒加载且只建一次（初始化较重）；任何失败都返回空串、绝不抛，不阻断入库。
RapidOCR 未安装时 is_available() 返回 False，调用方据此决定是否走 OCR。
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_engine = None
_lock = threading.Lock()


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
                _engine = RapidOCR()
    return _engine


def ocr_image(data: bytes) -> str:
    """OCR 一张图片字节流，返回识别文本（按行换行拼接）。失败返回空串，绝不抛。"""
    try:
        import cv2
        import numpy as np

        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return ""
        result, _ = _get_engine()(arr)
        if not result:
            return ""
        return "\n".join(text for _box, text, _score in result)
    except Exception as exc:  # noqa: BLE001
        log.warning("ocr_image failed: %s", exc)
        return ""
