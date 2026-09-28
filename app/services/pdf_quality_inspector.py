"""Deterministic quality gating inspector for local PDF OCR candidates."""
from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections.abc import Mapping
from typing import Any, Literal, TypedDict

PdfLocalQualityReason = Literal[
    "accepted",
    "incomplete_or_non_ocr",
    "insufficient_or_corrupt_text",
    "invalid_ocr_evidence",
    "low_confidence",
    "structured_content_risk",
    "layout_risk",
]


class PdfLocalQualityVerdict(TypedDict):
    accept: bool
    reason: PdfLocalQualityReason


_STRUCTURED_SYMBOLS: frozenset[str] = frozenset(
    {"$", "%", "/", "\\", "=", "+", "×", "÷", "<", ">", "⁄"}
)
_MULTIPLE_CHOICE_PATTERN = re.compile(r"(?i)(?<![a-zA-Z0-9])[a-d]\.")


def _is_corrupt_control_char(ch: str) -> bool:
    if ch == "\ufffd":
        return True
    code = ord(ch)
    if (0x00 <= code <= 0x1F and ch not in ("\n", "\r", "\t")) or code == 0x7F:
        return True
    if 0x80 <= code <= 0x9F:
        return True
    return False


def _has_fraction_char(ch: str) -> bool:
    if ch in _STRUCTURED_SYMBOLS:
        return True
    if unicodedata.category(ch) == "No":
        return True
    name = unicodedata.name(ch, "")
    return "FRACTION" in name


def inspect_local_pdf_candidate(source: Mapping[str, Any]) -> PdfLocalQualityVerdict:
    """Inspect one local single-page scanned PDF candidate without side effects.

    Returns the first rejection reason according to the cascading order defined
    in docs/roadmap/pdf-cascading-quality-gating-plan-20260922.zh-CN.md Section 3.
    """
    # -------------------------------------------------------------------------
    # Branch 1: incomplete_or_non_ocr
    # -------------------------------------------------------------------------
    if not isinstance(source, Mapping):
        return {"accept": False, "reason": "incomplete_or_non_ocr"}

    coverage = source.get("coverage")
    if not isinstance(coverage, Mapping) or coverage.get("status") != "complete":
        return {"accept": False, "reason": "incomplete_or_non_ocr"}

    chunks = source.get("chunks")
    if not isinstance(chunks, list) or len(chunks) == 0:
        return {"accept": False, "reason": "incomplete_or_non_ocr"}

    segments = source.get("segments")
    if not isinstance(segments, list) or len(segments) != 1:
        return {"accept": False, "reason": "incomplete_or_non_ocr"}

    page_segment = segments[0]
    if not isinstance(page_segment, Mapping):
        return {"accept": False, "reason": "incomplete_or_non_ocr"}

    quality = page_segment.get("quality")
    if not isinstance(quality, Mapping):
        return {"accept": False, "reason": "incomplete_or_non_ocr"}

    if quality.get("extraction_mode") != "ocr":
        return {"accept": False, "reason": "incomplete_or_non_ocr"}

    if quality.get("visual_content_unparsed") is not False:
        return {"accept": False, "reason": "incomplete_or_non_ocr"}

    # -------------------------------------------------------------------------
    # Branch 2: insufficient_or_corrupt_text
    # -------------------------------------------------------------------------
    page_text = page_segment.get("text")
    if not isinstance(page_text, str):
        return {"accept": False, "reason": "insufficient_or_corrupt_text"}

    letter_count = sum(1 for ch in page_text if ch.isalpha())
    if not (80 <= letter_count <= 20000):
        return {"accept": False, "reason": "insufficient_or_corrupt_text"}

    if any(_is_corrupt_control_char(ch) for ch in page_text):
        return {"accept": False, "reason": "insufficient_or_corrupt_text"}

    # -------------------------------------------------------------------------
    # Branch 3: invalid_ocr_evidence
    # -------------------------------------------------------------------------
    ocr_blocks = quality.get("ocr_blocks")
    if not isinstance(ocr_blocks, list) or not (5 <= len(ocr_blocks) <= 128):
        return {"accept": False, "reason": "invalid_ocr_evidence"}

    for block in ocr_blocks:
        if not isinstance(block, Mapping):
            return {"accept": False, "reason": "invalid_ocr_evidence"}

        b_text = block.get("text")
        if not isinstance(b_text, str) or len(b_text) == 0:
            return {"accept": False, "reason": "invalid_ocr_evidence"}

        bbox = block.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return {"accept": False, "reason": "invalid_ocr_evidence"}

        x0, y0, x1, y1 = bbox
        if not all(
            isinstance(v, (int, float)) and math.isfinite(v) and v >= 0.0
            for v in (x0, y0, x1, y1)
        ):
            return {"accept": False, "reason": "invalid_ocr_evidence"}

        if not (x1 > x0 and y1 > y0):
            return {"accept": False, "reason": "invalid_ocr_evidence"}

        conf = block.get("confidence")
        if (
            not isinstance(conf, (int, float))
            or not math.isfinite(conf)
            or not (0.0 <= conf <= 1.0)
        ):
            return {"accept": False, "reason": "invalid_ocr_evidence"}

    block_text_stripped = "".join(
        ch
        for b in ocr_blocks
        if isinstance(b.get("text"), str)
        for ch in b["text"]
        if not ch.isspace()
    )
    page_text_stripped = "".join(ch for ch in page_text if not ch.isspace())
    if block_text_stripped != page_text_stripped:
        return {"accept": False, "reason": "invalid_ocr_evidence"}

    # -------------------------------------------------------------------------
    # Branch 4: low_confidence
    # -------------------------------------------------------------------------
    confidences = [float(b["confidence"]) for b in ocr_blocks]
    median_conf = statistics.median(confidences)
    min_conf = min(confidences)
    if median_conf < 0.90 or min_conf < 0.65:
        return {"accept": False, "reason": "low_confidence"}

    # -------------------------------------------------------------------------
    # Branch 5: structured_content_risk
    # -------------------------------------------------------------------------
    # Arabic numbers 0-9
    if any("0" <= ch <= "9" for ch in page_text):
        return {"accept": False, "reason": "structured_content_risk"}

    # Table or math symbols & fraction characters
    if any(ch in _STRUCTURED_SYMBOLS or _has_fraction_char(ch) for ch in page_text):
        return {"accept": False, "reason": "structured_content_risk"}

    # Multiple choice marker: A./B./C./D. case-insensitive
    if _MULTIPLE_CHOICE_PATTERN.search(page_text):
        return {"accept": False, "reason": "structured_content_risk"}

    # -------------------------------------------------------------------------
    # Branch 6: layout_risk
    # -------------------------------------------------------------------------
    for block in ocr_blocks:
        block_letters = sum(1 for ch in block["text"] if ch.isalpha())
        if block_letters < 2:
            return {"accept": False, "reason": "layout_risk"}

    heights = [float(b["bbox"][3] - b["bbox"][1]) for b in ocr_blocks]
    median_h = statistics.median(heights)

    for i in range(len(ocr_blocks)):
        b1 = ocr_blocks[i]
        letters1 = sum(1 for ch in b1["text"] if ch.isalpha())
        if letters1 < 5:
            continue
        x0_1, y0_1, x1_1, y1_1 = b1["bbox"]
        h1 = y1_1 - y0_1

        for j in range(i + 1, len(ocr_blocks)):
            b2 = ocr_blocks[j]
            letters2 = sum(1 for ch in b2["text"] if ch.isalpha())
            if letters2 < 5:
                continue
            x0_2, y0_2, x1_2, y1_2 = b2["bbox"]
            h2 = y1_2 - y0_2

            y_overlap = min(y1_1, y1_2) - max(y0_1, y0_2)
            if y_overlap < min(h1, h2) / 2.0:
                continue

            if x1_1 <= x0_2:
                gap = x0_2 - x1_1
            elif x1_2 <= x0_1:
                gap = x0_1 - x1_2
            else:
                continue

            if gap >= 2.0 * median_h:
                return {"accept": False, "reason": "layout_risk"}

    return {"accept": True, "reason": "accepted"}
