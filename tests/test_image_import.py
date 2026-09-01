from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings, settings
from app.services import import_uploads, ocr
from app.services.evidence_write_path import validate_parser_segments
from app.services.import_parsing import ImportResourceLimitError, parse_import_file


IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def _library(*, ocr_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_size=200,
        chunk_overlap=20,
        ocr_enabled=ocr_enabled,
        docx_table_aware=False,
    )


def test_import_configuration_advertises_direct_image_formats() -> None:
    configuration = import_uploads.import_configuration(Settings(_env_file=None))

    assert IMAGE_EXTENSIONS.issubset(configuration["allowed_extensions"])


@pytest.mark.parametrize("suffix", sorted(IMAGE_EXTENSIONS))
def test_direct_image_ocr_builds_chunks_and_bound_image_regions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    suffix: str,
) -> None:
    path = tmp_path / f"diagram{suffix}"
    path.write_bytes(b"image-fixture")
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(
        ocr,
        "ocr_image_blocks",
        lambda _data: [
            {
                "text": "设备编号 IMG-2026-001",
                "bbox": [10, 20, 310, 80],
                "confidence": 0.96,
                "parser": "rapidocr",
            }
        ],
    )

    result = parse_import_file(
        path,
        _library(ocr_enabled=True),
        file_name=f"设备铭牌{suffix}",
    )

    assert result.normalized_text == (
        f"文件名：设备铭牌{suffix}\n\n设备编号 IMG-2026-001"
    )
    assert any(f"设备铭牌{suffix}" in chunk["text"] for chunk in result.chunks)
    assert result.segments[0]["source_kind"] == "image"
    assert result.segments[0]["parser_unit"]["source"]["text"]["start"] == 0
    ocr_segment = next(
        segment for segment in result.segments if segment["unit_key"] == "image:ocr"
    )
    image_region = ocr_segment["structured_units"][0]
    assert image_region["unit_kind"] == "image_region"
    assert image_region["parent_key"] == ocr_segment["unit_key"]
    assert image_region["source"]["bbox"]["coordinate_system"] == "image_pixels"
    assert image_region["source"]["text"]["end"] == len(result.normalized_text)
    assert image_region["quality"]["ocr_confidence"] == 0.96
    validate_parser_segments(result.segments)


def test_direct_image_requires_library_ocr(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    path = tmp_path / "scan.png"
    path.write_bytes(b"image-fixture")
    monkeypatch.setattr(ocr, "is_available", lambda: True)

    with pytest.raises(ValueError, match="enable OCR"):
        parse_import_file(path, _library(ocr_enabled=False))


def test_direct_image_reports_missing_ocr_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "scan.png"
    path.write_bytes(b"image-fixture")
    monkeypatch.setattr(ocr, "is_available", lambda: False)

    with pytest.raises(ValueError, match="dependency is unavailable"):
        parse_import_file(path, _library(ocr_enabled=True))


def test_direct_image_without_ocr_text_is_searchable_by_original_file_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "opaque-staging-file"
    path.write_bytes(b"image-fixture")
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image_blocks", lambda _data: [])

    result = parse_import_file(
        path,
        _library(ocr_enabled=True),
        file_name="现场设备铭牌.png",
    )

    assert result.normalized_text == "文件名：现场设备铭牌.png"
    assert result.chunks[0]["text"] == result.normalized_text
    assert result.segments[0]["parser_unit"]["source"]["file_name"] == "现场设备铭牌.png"
    assert result.segments[0].get("structured_units") == []
    validate_parser_segments(result.segments)


def test_direct_image_rejects_oversized_input_before_ocr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "oversized.png"
    path.write_bytes(b"1234")
    monkeypatch.setattr(settings, "image_ocr_max_input_bytes", 3, raising=False)
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(
        ocr,
        "ocr_image_blocks",
        lambda _data: pytest.fail("OCR must not run for oversized images"),
    )

    with pytest.raises(ImportResourceLimitError):
        parse_import_file(path, _library(ocr_enabled=True))
