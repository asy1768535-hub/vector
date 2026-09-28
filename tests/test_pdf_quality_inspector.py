"""Tests for the local PDF candidate quality inspector."""
from __future__ import annotations

import math
from typing import Any

from app.services.pdf_quality_inspector import (
    PdfLocalQualityVerdict,
    inspect_local_pdf_candidate,
)


def _build_valid_ocr_blocks(
    count: int = 10,
    *,
    text_prefix: str = "有效测试文本内容",
    confidence: float = 0.95,
    start_y: float = 50.0,
    line_height: float = 30.0,
    width: float = 200.0,
) -> list[dict[str, Any]]:
    ordinals = [
        "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
        "十一", "十二", "十三", "十四", "十五", "十六",
    ]
    blocks: list[dict[str, Any]] = []
    for i in range(count):
        y0 = start_y + i * (line_height + 10.0)
        y1 = y0 + line_height
        ord_str = ordinals[i] if i < len(ordinals) else "某"
        blocks.append(
            {
                "text": f"{text_prefix}第{ord_str}行完整说明",
                "bbox": [50.0, y0, 50.0 + width, y1],
                "confidence": confidence,
            }
        )
    return blocks


def _build_valid_candidate(
    *,
    ocr_blocks: list[dict[str, Any]] | None = None,
    coverage_status: str = "complete",
    extraction_mode: str = "ocr",
    visual_content_unparsed: bool = False,
    page_text: str | None = None,
    chunks: list[dict[str, Any]] | None = None,
    segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a complete valid candidate dictionary for inspector testing."""
    if ocr_blocks is None:
        ocr_blocks = _build_valid_ocr_blocks(10)

    if page_text is None:
        page_text = "\n".join(b["text"] for b in ocr_blocks)

    if chunks is None:
        chunks = [{"text": page_text, "source_start": 0, "source_end": len(page_text)}]

    if segments is None:
        segments = [
            {
                "ordinal": 0,
                "text": page_text,
                "quality": {
                    "extraction_mode": extraction_mode,
                    "visual_content_unparsed": visual_content_unparsed,
                    "ocr_blocks": ocr_blocks,
                },
            }
        ]

    return {
        "coverage": {
            "status": coverage_status,
            "total_pages": 1,
            "processed_pages": [1],
        },
        "chunks": chunks,
        "segments": segments,
        "normalized_text": page_text,
    }


def test_valid_candidate_is_accepted() -> None:
    candidate = _build_valid_candidate()
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict["accept"] is True
    assert verdict["reason"] == "accepted"


# --- Branch 1: incomplete_or_non_ocr ---


def test_reject_when_source_is_not_mapping() -> None:
    assert inspect_local_pdf_candidate("not a dict") == {  # type: ignore[arg-type]
        "accept": False,
        "reason": "incomplete_or_non_ocr",
    }


def test_reject_when_coverage_incomplete() -> None:
    candidate = _build_valid_candidate(coverage_status="partial")
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict == {"accept": False, "reason": "incomplete_or_non_ocr"}


def test_reject_when_chunks_empty() -> None:
    candidate = _build_valid_candidate(chunks=[])
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict == {"accept": False, "reason": "incomplete_or_non_ocr"}


def test_reject_when_segments_not_exactly_one() -> None:
    candidate = _build_valid_candidate(segments=[])
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict == {"accept": False, "reason": "incomplete_or_non_ocr"}

    candidate_two = _build_valid_candidate(
        segments=[
            {"text": "page 1", "quality": {}},
            {"text": "page 2", "quality": {}},
        ]
    )
    assert inspect_local_pdf_candidate(candidate_two) == {
        "accept": False,
        "reason": "incomplete_or_non_ocr",
    }


def test_reject_when_extraction_mode_is_native() -> None:
    candidate = _build_valid_candidate(extraction_mode="native")
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict == {"accept": False, "reason": "incomplete_or_non_ocr"}


def test_reject_when_visual_content_unparsed() -> None:
    candidate = _build_valid_candidate(visual_content_unparsed=True)
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict == {"accept": False, "reason": "incomplete_or_non_ocr"}


# --- Branch 2: insufficient_or_corrupt_text ---


def test_reject_when_text_has_fewer_than_80_letters() -> None:
    # Build 5 blocks with very short text (< 80 letters total)
    blocks = _build_valid_ocr_blocks(5, text_prefix="短文")
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict == {"accept": False, "reason": "insufficient_or_corrupt_text"}


def test_reject_when_text_contains_ufffd() -> None:
    blocks = _build_valid_ocr_blocks(10)
    blocks[0]["text"] += "\ufffd"
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict == {"accept": False, "reason": "insufficient_or_corrupt_text"}


def test_reject_when_text_contains_c0_c1_control_chars() -> None:
    blocks = _build_valid_ocr_blocks(10)
    blocks[0]["text"] += "\x07"  # Bell char
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict == {"accept": False, "reason": "insufficient_or_corrupt_text"}


# --- Branch 3: invalid_ocr_evidence ---


def test_reject_when_ocr_blocks_fewer_than_5() -> None:
    # 4 blocks with enough letters total
    blocks = _build_valid_ocr_blocks(4, text_prefix="这是一段非常长的中文段落描述以满足字符数要求")
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict == {"accept": False, "reason": "invalid_ocr_evidence"}


def test_reject_when_bbox_has_nan_or_inf() -> None:
    blocks = _build_valid_ocr_blocks(10)
    blocks[2]["bbox"] = [50.0, float("nan"), 250.0, 80.0]
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    verdict = inspect_local_pdf_candidate(candidate)
    assert verdict == {"accept": False, "reason": "invalid_ocr_evidence"}

    blocks[2]["bbox"] = [50.0, 50.0, float("inf"), 80.0]
    candidate2 = _build_valid_candidate(ocr_blocks=blocks)
    assert inspect_local_pdf_candidate(candidate2) == {
        "accept": False,
        "reason": "invalid_ocr_evidence",
    }


def test_reject_when_bbox_is_negative_or_inverted() -> None:
    blocks = _build_valid_ocr_blocks(10)
    blocks[0]["bbox"] = [-1.0, 50.0, 200.0, 80.0]
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    assert inspect_local_pdf_candidate(candidate) == {
        "accept": False,
        "reason": "invalid_ocr_evidence",
    }

    # inverted x: x1 <= x0
    blocks[0]["bbox"] = [200.0, 50.0, 100.0, 80.0]
    candidate2 = _build_valid_candidate(ocr_blocks=blocks)
    assert inspect_local_pdf_candidate(candidate2) == {
        "accept": False,
        "reason": "invalid_ocr_evidence",
    }


def test_reject_when_confidence_out_of_bounds() -> None:
    blocks = _build_valid_ocr_blocks(10)
    blocks[0]["confidence"] = 1.05
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    assert inspect_local_pdf_candidate(candidate) == {
        "accept": False,
        "reason": "invalid_ocr_evidence",
    }


def test_reject_when_block_text_mismatches_page_text() -> None:
    blocks = _build_valid_ocr_blocks(10)
    candidate = _build_valid_candidate(
        ocr_blocks=blocks,
        page_text="此处的页文本与块拼接文本完全不同以触发文本证据不一致校验" * 4,
    )
    assert inspect_local_pdf_candidate(candidate) == {
        "accept": False,
        "reason": "invalid_ocr_evidence",
    }


# --- Branch 4: low_confidence ---


def test_reject_when_median_confidence_below_0_90() -> None:
    blocks = _build_valid_ocr_blocks(10, confidence=0.88)
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    assert inspect_local_pdf_candidate(candidate) == {
        "accept": False,
        "reason": "low_confidence",
    }


def test_reject_when_minimum_confidence_below_0_65() -> None:
    blocks = _build_valid_ocr_blocks(10, confidence=0.95)
    blocks[0]["confidence"] = 0.60
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    assert inspect_local_pdf_candidate(candidate) == {
        "accept": False,
        "reason": "low_confidence",
    }


# --- Branch 5: structured_content_risk ---


def test_reject_when_text_has_arabic_numbers() -> None:
    blocks = _build_valid_ocr_blocks(10)
    blocks[0]["text"] += "金额100元"
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    assert inspect_local_pdf_candidate(candidate) == {
        "accept": False,
        "reason": "structured_content_risk",
    }


def test_reject_when_text_has_table_or_math_symbols() -> None:
    for sym in ["$", "%", "/", "\\", "=", "+", "×", "÷", "<", ">", "½"]:
        blocks = _build_valid_ocr_blocks(10)
        blocks[0]["text"] += f"包含符号{sym}测试"
        candidate = _build_valid_candidate(ocr_blocks=blocks)
        assert inspect_local_pdf_candidate(candidate) == {
            "accept": False,
            "reason": "structured_content_risk",
        }, f"Failed for symbol: {sym}"


def test_reject_when_text_has_multiple_choice_marker() -> None:
    for marker in ["A.", "B.", "C.", "D.", "a.", "b.", "c.", "d."]:
        blocks = _build_valid_ocr_blocks(10)
        blocks[0]["text"] += f" 选项 {marker} 描述测试"
        candidate = _build_valid_candidate(ocr_blocks=blocks)
        assert inspect_local_pdf_candidate(candidate) == {
            "accept": False,
            "reason": "structured_content_risk",
        }, f"Failed for marker: {marker}"


# --- Branch 6: layout_risk ---


def test_reject_when_block_has_fewer_than_2_letters() -> None:
    blocks = _build_valid_ocr_blocks(10)
    blocks[0]["text"] = "中"  # Only 1 letter
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    assert inspect_local_pdf_candidate(candidate) == {
        "accept": False,
        "reason": "layout_risk",
    }


def test_reject_when_horizontal_separation_detected() -> None:
    # 2 blocks in the same horizontal row with gap >= 2 * median_h
    # median height = 30.0 -> gap >= 60.0
    blocks = _build_valid_ocr_blocks(8)
    # Add two blocks on the same horizontal row:
    # Block A: x from 50 to 150, y from 400 to 430 (h=30, >= 5 letters)
    # Block B: x from 250 to 350, y from 405 to 435 (h=30, >= 5 letters)
    # y overlap: min(430, 435) - max(400, 405) = 430 - 405 = 25 >= 30 / 2 = 15.
    # gap: 250 - 150 = 100 >= 2 * 30 = 60.
    blocks.append(
        {
            "text": "左栏长文本内容说明演示",
            "bbox": [50.0, 400.0, 150.0, 430.0],
            "confidence": 0.95,
        }
    )
    blocks.append(
        {
            "text": "右栏长文本内容说明演示",
            "bbox": [250.0, 405.0, 350.0, 435.0],
            "confidence": 0.95,
        }
    )
    candidate = _build_valid_candidate(ocr_blocks=blocks)
    assert inspect_local_pdf_candidate(candidate) == {
        "accept": False,
        "reason": "layout_risk",
    }
