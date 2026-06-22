"""OCR 服务单测：失败路径优雅降级（不依赖真模型）。"""
from __future__ import annotations

from app.services import ocr


def test_is_available_returns_bool():
    assert isinstance(ocr.is_available(), bool)


def test_ocr_image_garbage_returns_empty_not_raise():
    # 非图片字节：cv2.imdecode 解析失败 → 返回空串、不抛、也不初始化重引擎
    assert ocr.ocr_image(b"this is not an image") == ""


def test_ocr_image_empty_bytes_returns_empty():
    assert ocr.ocr_image(b"") == ""
