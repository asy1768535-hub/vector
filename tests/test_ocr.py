"""OCR 服务单测：失败路径优雅降级（不依赖真模型）。"""
from __future__ import annotations

import sys

from app.services import ocr


def test_is_available_returns_bool():
    assert isinstance(ocr.is_available(), bool)


def test_ocr_image_garbage_returns_empty_not_raise():
    # 非图片字节：cv2.imdecode 解析失败 → 返回空串、不抛、也不初始化重引擎
    assert ocr.ocr_image(b"this is not an image") == ""


def test_ocr_image_empty_bytes_returns_empty():
    assert ocr.ocr_image(b"") == ""


def test_ocr_image_blocks_preserves_bbox_and_confidence(monkeypatch):
    class _Numpy:
        uint8 = object()

        @staticmethod
        def frombuffer(data, _dtype):
            return data

    class _Cv2:
        IMREAD_COLOR = 1

        @staticmethod
        def imdecode(data, _mode):
            return data

    class _Engine:
        def __call__(self, _image):
            return [([[10, 20], [30, 20], [30, 40], [10, 40]], "Region", 0.91)], None

    monkeypatch.setitem(sys.modules, "numpy", _Numpy)
    monkeypatch.setitem(sys.modules, "cv2", _Cv2)
    monkeypatch.setattr(ocr, "_get_engine", lambda: _Engine())

    assert ocr.ocr_image_blocks(b"image") == [
        {
            "text": "Region",
            "bbox": [10.0, 20.0, 30.0, 40.0],
            "confidence": 0.91,
            "parser": "rapidocr",
        }
    ]
