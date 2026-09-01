"""OCR 服务单测：失败路径优雅降级（不依赖真模型）。"""
from __future__ import annotations

import sys
import io

import pytest
from PIL import Image

from app.services import ocr


def test_is_available_returns_bool():
    assert isinstance(ocr.is_available(), bool)


def test_engine_uses_bounded_onnx_thread_configuration(monkeypatch):
    captured = {}

    class RapidOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "rapidocr_onnxruntime",
        type("RapidModule", (), {"RapidOCR": RapidOCR}),
    )
    monkeypatch.setattr(ocr, "_engine", None)
    monkeypatch.setattr(ocr.settings, "ocr_intra_op_num_threads", 12)
    monkeypatch.setattr(ocr.settings, "ocr_inter_op_num_threads", 1)

    ocr._get_engine()

    assert captured == {
        "intra_op_num_threads": 12,
        "inter_op_num_threads": 1,
    }


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


def test_ocr_rejects_oversized_decoded_pixels_before_engine(monkeypatch):
    class _Image:
        shape = (200, 300, 3)

    class _Numpy:
        uint8 = object()

        @staticmethod
        def frombuffer(data, _dtype):
            return data

    class _Cv2:
        IMREAD_COLOR = 1

        @staticmethod
        def imdecode(data, _mode):
            return _Image()

    called = False

    def engine():
        nonlocal called
        called = True
        return object()

    monkeypatch.setitem(sys.modules, "numpy", _Numpy)
    monkeypatch.setitem(sys.modules, "cv2", _Cv2)
    monkeypatch.setattr(ocr, "OCR_MAX_IMAGE_PIXELS", 10)
    monkeypatch.setattr(ocr, "_get_engine", engine)

    with pytest.raises(ocr.OcrResourceLimitError, match="image pixels"):
        ocr.ocr_image_blocks(b"image")
    assert called is False


def test_ocr_header_budget_rejects_before_cv2_decode(monkeypatch):
    image_bytes = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image_bytes, format="PNG")

    decode_calls = 0

    class _Cv2:
        IMREAD_COLOR = 1

        @staticmethod
        def imdecode(_data, _mode):
            nonlocal decode_calls
            decode_calls += 1
            return object()

    class _Numpy:
        uint8 = object()

        @staticmethod
        def frombuffer(data, _dtype):
            return data

    monkeypatch.setitem(sys.modules, "numpy", _Numpy)
    monkeypatch.setitem(sys.modules, "cv2", _Cv2)
    monkeypatch.setattr(ocr, "OCR_MAX_IMAGE_PIXELS", 10)

    with pytest.raises(ocr.OcrResourceLimitError, match="image pixels"):
        ocr.ocr_image_blocks(image_bytes.getvalue())
    assert decode_calls == 0


def test_ocr_rejects_output_text_budget(monkeypatch):
    class _Numpy:
        uint8 = object()

        @staticmethod
        def frombuffer(data, _dtype):
            return data

    class _Cv2:
        IMREAD_COLOR = 1

        @staticmethod
        def imdecode(data, _mode):
            return object()

    class _Engine:
        def __call__(self, _image):
            return [([], "too long", 0.9)], None

    monkeypatch.setitem(sys.modules, "numpy", _Numpy)
    monkeypatch.setitem(sys.modules, "cv2", _Cv2)
    monkeypatch.setattr(ocr, "OCR_MAX_TEXT_CHARS", 3)
    monkeypatch.setattr(ocr, "_get_engine", lambda: _Engine())

    with pytest.raises(ocr.OcrResourceLimitError, match="output text"):
        ocr.ocr_image_blocks(b"image")
