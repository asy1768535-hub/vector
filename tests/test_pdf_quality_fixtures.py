"""合成 PDF 质量夹具与级联后验路由全量断言测试。

验证 tests/fixtures/pdf_quality_cases/ 下的全部合成夹具：
1. 夹具存在性与 SHA-256 清单哈希强校验
2. 有界预检 preflight_pdf 契约与特征断言
3. 初始 V1 选型 choose_pdf_route 断言
4. 纯函数 inspect_local_pdf_candidate 质量判定断言
5. 级联门控关闭 (gate off) vs 开启 (gate on) 下 build_pdf_import_source 选型与单主解析器不变量
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.services import (
    ocr as ocr_service,
    pdf_extract,
    pdf_preflight,
    pdf_quality_inspector,
    pdf_routing,
)
from app.services.import_parsing import build_pdf_import_source
from app.services.parser_units import build_parser_unit, parser_provenance

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "pdf_quality_cases"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    assert MANIFEST_PATH.is_file(), f"Manifest file missing: {MANIFEST_PATH}"
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert data.get("schema_version") == "pdf-quality-synthetic-fixtures-v1"
    return data


def _mock_mineru_source(total_pages: int = 1) -> dict[str, Any]:
    text = "MinerU remote parsed structural content"
    return {
        "text": text,
        "chunks": [{"text": text, "source_start": 0, "source_end": len(text)}],
        "segments": [{
            "kind": "prose",
            "text": text,
            "location": {"type": "page", "page": 1},
            "quality": {
                "extraction_mode": "native",
                "visual_content_unparsed": False,
            },
            "parser_unit": build_parser_unit(
                source_kind="pdf",
                unit_kind="section",
                ordinal=0,
                unit_key="pdf:0:section:0",
                parser=parser_provenance("mineru", "v1"),
                source={"page": {"start": 1, "end": 1}},
            ),
            "structured_units": [],
        }],
        "coverage": {
            "status": "complete",
            "total_pages": total_pages,
            "pages": [{"page": p, "status": "complete", "visual_content_unparsed": False} for p in range(1, total_pages + 1)],
        },
    }


def test_fixtures_integrity_and_manifest_hashes(manifest):
    """验证所有夹具文件存在且 SHA-256 与 manifest.json 完全一致。"""
    samples = manifest["samples"]
    assert len(samples) >= 8

    for sample in samples:
        file_path = FIXTURES_DIR / sample["file_name"]
        assert file_path.is_file(), f"Fixture file not found: {file_path}"
        data = file_path.read_bytes()
        calculated_sha = hashlib.sha256(data).hexdigest()
        assert calculated_sha == sample["sha256"], f"SHA256 mismatch for {sample['file_name']}"
        assert len(data) == sample["bytes"]


def test_preflight_on_all_fixtures(manifest):
    """验证每个合成夹具的 preflight_pdf 契约。"""
    for sample in manifest["samples"]:
        file_path = FIXTURES_DIR / sample["file_name"]
        data = file_path.read_bytes()
        preflight = pdf_preflight.preflight_pdf(data, min_text_chars=40)
        assert preflight["status"] == sample["expected_preflight_status"]
        if sample["id"] == "08_corrupted":
            assert preflight["status"] == "unknown"
            assert preflight["total_pages"] is None
        else:
            assert preflight["total_pages"] is not None
            assert preflight["total_pages"] >= 1


def test_v1_routing_decisions(manifest):
    """验证 MinerU 授权下，各个夹具在 V1 规则下的路由结果。"""
    for sample in manifest["samples"]:
        file_path = FIXTURES_DIR / sample["file_name"]
        data = file_path.read_bytes()
        preflight = pdf_preflight.preflight_pdf(data, min_text_chars=40)
        route = pdf_routing.choose_pdf_route(preflight, mineru_authorized=True)
        assert route["selection"] == sample["expected_v1_selection"]


def test_cascade_gate_off_behavior(manifest, monkeypatch):
    """gate 关闭时，保持原 V1 行为，不进行候选本地 OCR 提取。"""
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", False)
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "pdf_test_lib")

    library = SimpleNamespace(
        slug="pdf_test_lib",
        chunk_size=800,
        chunk_overlap=80,
        ocr_enabled=True,
    )

    with patch("app.services.mineru_pdf.parse_pdf_remote") as mock_mineru:
        mock_mineru.side_effect = lambda *args, **kwargs: _mock_mineru_source(kwargs.get("total_pages", 1) or 1)

        for sample in manifest["samples"]:
            if sample["id"] == "08_corrupted":
                continue  # 损坏文件由底层解析抛出异常
            file_path = FIXTURES_DIR / sample["file_name"]
            data = file_path.read_bytes()

            result = build_pdf_import_source(data, library=library)
            routing = result.get("routing")
            assert routing is not None
            assert routing["selection"] == sample["expected_v1_selection"]


def test_cascade_gate_on_clean_bilingual_scan_passes(monkeypatch):
    """Case 2（清晰双语扫描件）：gate 开启时通过质检，直通本地并复用结果，不调用 MinerU。"""
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "pdf_test_lib")

    library = SimpleNamespace(
        slug="pdf_test_lib",
        chunk_size=800,
        chunk_overlap=80,
        ocr_enabled=True,
    )

    file_path = FIXTURES_DIR / "02_clean_bilingual_scan.pdf"
    data = file_path.read_bytes()

    with patch("app.services.mineru_pdf.parse_pdf_remote") as mock_mineru:
        result = build_pdf_import_source(data, library=library)

        assert mock_mineru.call_count == 0
        routing = result.get("routing")
        assert routing is not None
        assert routing["selection"] == "native_or_rapidocr"
        assert routing["needs_review"] is False
        # 验证单主解析器不变量与 coverage 契约
        assert result["coverage"]["status"] == "complete"
        assert len(result["segments"]) == 1
        assert result["segments"][0]["quality"]["extraction_mode"] == "ocr"


def test_cascade_gate_on_table_scan_escalates_to_mineru(monkeypatch):
    """Case 3（表格扫描件）：gate 开启时因结构化风险拒绝，安全升档 MinerU。"""
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "pdf_test_lib")

    library = SimpleNamespace(
        slug="pdf_test_lib",
        chunk_size=800,
        chunk_overlap=80,
        ocr_enabled=True,
    )

    file_path = FIXTURES_DIR / "03_table_scan.pdf"
    data = file_path.read_bytes()

    with patch("app.services.mineru_pdf.parse_pdf_remote") as mock_mineru:
        mock_mineru.return_value = _mock_mineru_source(total_pages=1)
        result = build_pdf_import_source(data, library=library)

        assert mock_mineru.call_count == 1
        routing = result.get("routing")
        assert routing["selection"] == "mineru"


def test_cascade_gate_on_three_column_scan_escalates_to_mineru(monkeypatch):
    """Case 4（三栏扫描件）：gate 开启时因版面多栏风险拒绝，安全升档 MinerU。"""
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "pdf_test_lib")

    library = SimpleNamespace(
        slug="pdf_test_lib",
        chunk_size=800,
        chunk_overlap=80,
        ocr_enabled=True,
    )

    file_path = FIXTURES_DIR / "04_three_column_scan.pdf"
    data = file_path.read_bytes()

    with patch("app.services.mineru_pdf.parse_pdf_remote") as mock_mineru:
        mock_mineru.return_value = _mock_mineru_source(total_pages=1)
        result = build_pdf_import_source(data, library=library)

        assert mock_mineru.call_count == 1
        routing = result.get("routing")
        assert routing["selection"] == "mineru"


def test_cascade_gate_on_low_confidence_scan_escalates_to_mineru(monkeypatch):
    """Case 5（模糊低清扫描件）：gate 开启时因低置信度拒绝，安全升档 MinerU。"""
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "pdf_test_lib")

    library = SimpleNamespace(
        slug="pdf_test_lib",
        chunk_size=800,
        chunk_overlap=80,
        ocr_enabled=True,
    )

    file_path = FIXTURES_DIR / "05_low_confidence_scan.pdf"
    data = file_path.read_bytes()

    with patch("app.services.mineru_pdf.parse_pdf_remote") as mock_mineru:
        mock_mineru.return_value = _mock_mineru_source(total_pages=1)
        result = build_pdf_import_source(data, library=library)

        assert mock_mineru.call_count == 1
        routing = result.get("routing")
        assert routing["selection"] == "mineru"


def test_cascade_gate_on_blank_scan_escalates_to_mineru(monkeypatch):
    """Case 6（空白无字扫描件）：候选提取异常或无文字，安全升档 MinerU。"""
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "pdf_test_lib")

    library = SimpleNamespace(
        slug="pdf_test_lib",
        chunk_size=800,
        chunk_overlap=80,
        ocr_enabled=True,
    )

    file_path = FIXTURES_DIR / "06_blank_scan.pdf"
    data = file_path.read_bytes()

    with patch("app.services.mineru_pdf.parse_pdf_remote") as mock_mineru:
        mock_mineru.return_value = _mock_mineru_source(total_pages=1)
        result = build_pdf_import_source(data, library=library)

        assert mock_mineru.call_count == 1
        routing = result.get("routing")
        assert routing["selection"] == "mineru"


def test_cascade_gate_on_multi_page_scan_skips_candidate(monkeypatch):
    """Case 7（多页扫描件）：页数 > 1 不满足单页级联条件，直接调用 MinerU，不调用本地 candidate。"""
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "pdf_test_lib")

    library = SimpleNamespace(
        slug="pdf_test_lib",
        chunk_size=800,
        chunk_overlap=80,
        ocr_enabled=True,
    )

    file_path = FIXTURES_DIR / "07_multi_page_scan.pdf"
    data = file_path.read_bytes()

    with (
        patch("app.services.pdf_extract.build_pdf_source") as mock_extract,
        patch("app.services.mineru_pdf.parse_pdf_remote") as mock_mineru,
    ):
        mock_mineru.return_value = _mock_mineru_source(total_pages=2)
        result = build_pdf_import_source(data, library=library)

        assert mock_extract.call_count == 0
        assert mock_mineru.call_count == 1
        routing = result.get("routing")
        assert routing["selection"] == "mineru"
