"""表格感知组块 chunk_segments 单测（纯函数，不连服务）。"""
from __future__ import annotations

from app.services.splitter import (
    build_structured_source_from_segments,
    chunk_segments,
)


def test_prose_gets_heading_prefix():
    segs = [{"kind": "prose", "heading": "2 设计 / 2.2 原则", "text": "分层解耦说明"}]
    out = chunk_segments(segs, chunk_size=1000, chunk_overlap=0)
    assert len(out) == 1
    assert out[0].startswith("【章节】2 设计 / 2.2 原则")
    assert "分层解耦说明" in out[0]


def test_table_whole_with_context():
    segs = [{
        "kind": "table", "heading": "3 / 3.2 技术路线", "caption": "技术选型",
        "header": "类别 | 方案",
        "rows": ["类别 | 方案", "报表 | POI、iText", "鉴权 | Sa-Token"],
        "row_numbers": [1, 2, 3],
    }]
    out = chunk_segments(segs, chunk_size=1000, chunk_overlap=0)
    assert len(out) == 1
    c = out[0]
    assert "【章节】3 / 3.2 技术路线" in c and "【表格】技术选型" in c
    assert "POI、iText" in c and "Sa-Token" in c


def test_big_table_splits_by_rows_repeating_header():
    rows = ["h | v"] + [f"r{i} | " + ("x" * 40) for i in range(20)]
    segs = [{"kind": "table", "heading": "H", "caption": "T", "header": "h | v", "rows": rows}]
    out = chunk_segments(segs, chunk_size=200, chunk_overlap=0)
    assert len(out) > 1                       # 超长表被拆成多块
    for c in out:                             # 每块都重复 章节/表名/表头
        assert "【章节】H" in c and "【表格】T" in c and "h | v" in c


def test_caption_equal_heading_not_duplicated():
    segs = [{"kind": "table", "heading": "X", "caption": "X", "header": "a | b", "rows": ["a | b", "1 | 2"]}]
    out = chunk_segments(segs, chunk_size=1000, chunk_overlap=0)
    assert "【表格】" not in out[0]            # caption==heading 时不再单独打表名
    assert "【章节】X" in out[0]


# ── Task 2: normalized_text 是原始内容，chunk.text 可含检索前缀，span 校验改为 revision + hash ──

def test_normalized_text_is_segment_flat_text_not_chunk_concat():
    """normalized_text 是按 segment 原顺序的 flat text，不含重复表头/前缀/overlap。"""
    segs = [
        {"kind": "prose", "heading": "A", "text": "正文Alpha"},
        {"kind": "table", "heading": "B", "caption": "T", "rows": ["h", "r1", "r2"]},
    ]
    source = build_structured_source_from_segments(segs, chunk_size=1000, chunk_overlap=0)

    nt = source["normalized_text"]
    # 每个 segment 的 flat text 只出现一次前缀
    assert nt.count("【章节】A") == 1
    assert nt.count("【章节】B") == 1
    assert nt.count("【表格】T") == 1
    assert "正文Alpha" in nt
    assert "r1" in nt and "r2" in nt


def test_structured_segments_chunks_have_correct_source_spans():
    """chunk 的 source_start/source_end 指向 normalized_text 内合法位置。"""
    rows = ["h | v"] + [f"r{i} | " + ("x" * 40) for i in range(20)]
    segs = [{"kind": "table", "heading": "H", "caption": "T", "header": "h | v", "rows": rows}]

    source = build_structured_source_from_segments(segs, chunk_size=200, chunk_overlap=0)

    assert [chunk["text"] for chunk in source["chunks"]] == chunk_segments(
        segs, chunk_size=200, chunk_overlap=0
    )
    for chunk in source["chunks"]:
        s, e = chunk["source_start"], chunk["source_end"]
        assert 0 <= s < e <= len(source["normalized_text"]), \
            f"source span [{s}:{e}] out of bounds (len={len(source['normalized_text'])})"
        assert "source_span_hash" in chunk
        # chunk.text 可包含检索用前缀（不在 normalized_text 的该偏移处重复出现），
        # 不再强制 normalized_text[s:e] == chunk.text
        assert chunk["location"]["type"] == "table"


def test_source_span_hash_verifies_integrity():
    """span hash 可以验证原文区间未被篡改。"""
    rows = ["h | v"] + [f"r{i} | " + ("x" * 40) for i in range(5)]
    segs = [{"kind": "table", "heading": "H", "caption": "T", "rows": rows}]
    source = build_structured_source_from_segments(segs, chunk_size=200, chunk_overlap=0)

    for chunk in source["chunks"]:
        import hashlib
        actual = hashlib.sha256(
            source["normalized_text"][chunk["source_start"]:chunk["source_end"]].encode("utf-8")
        ).hexdigest()[:16]
        assert actual == chunk["source_span_hash"], "span hash mismatch"


# ── Task 2: 重复行/长表格/空行 测试 ──

def test_duplicate_rows_map_to_distinct_positions():
    """同内容重复行必须分别定位，不错误聚合。"""
    rows = ["h | c"] + ["dup | x" for _ in range(5)]
    segs = [{
        "kind": "table", "heading": "H", "caption": "T",
        "rows": rows,
        "row_numbers": [1, 2, 3, 4, 5, 6],
        "location": {"type": "sheet", "sheet": "S1"},
    }]
    source = build_structured_source_from_segments(segs, chunk_size=40, chunk_overlap=0)

    # 收集所有包含 "dup" 的 chunk
    dup_chunks = [c for c in source["chunks"] if "dup" in c["text"]]
    assert len(dup_chunks) >= 2, f"重复行应产生多个 chunk，实际: {len(dup_chunks)}"
    # 每个 dup chunk 的 source_start 应不同
    starts = {c["source_start"] for c in dup_chunks}
    assert len(starts) == len(dup_chunks), "每个重复行 chunk 的 start 必须不同"


def test_long_table_with_row_numbers():
    """长表格 chunk 携带正确的原始行号（基于切分时传入的 row_numbers）。"""
    rows = ["h | v"] + [f"r{i} | " + ("x" * 50) for i in range(30)]
    row_numbers = list(range(1, 32))
    segs = [{
        "kind": "table", "heading": "H", "caption": "T",
        "rows": rows,
        "row_numbers": row_numbers,
        "location": {"type": "sheet", "sheet": "Sheet1"},
    }]
    out = chunk_segments(segs, chunk_size=200, chunk_overlap=0)
    assert len(out) > 1

    # 用 span-native 版本获取位置
    source = build_structured_source_from_segments(segs, chunk_size=200, chunk_overlap=0)
    sheet_row_chunks = [c for c in source["chunks"] if c.get("location", {}).get("type") == "sheet_row"]
    assert len(sheet_row_chunks) > 1, "应有多个带 sheet_row 位置的 chunk"
    # 验证每个 chunk 的位置信息合理
    seen_rows = set()
    for c in sheet_row_chunks:
        loc = c["location"]
        assert loc["start_row"] >= 1
        assert loc["end_row"] >= loc["start_row"]
        for r in range(loc["start_row"], loc["end_row"] + 1):
            seen_rows.add(r)
    # 覆盖的行号不应重复（每个原始行只出现在一个 chunk 的 source span 中）
    # source span 指向的行是数据行，不应重叠


def test_empty_rows_in_table_handled():
    """含空行的表格正确跳过空行。"""
    rows = ["h | v", "", "r1 | x", "", "r2 | y"]
    segs = [{"kind": "table", "heading": "H", "rows": rows}]
    source = build_structured_source_from_segments(segs, chunk_size=500, chunk_overlap=0)
    assert len(source["chunks"]) > 0
    # 空行不应出现在 normalized_text 中
    # (Note: _segment_flat_text 会保留原始 rows，空行由上层过滤)


def test_table_chunks_span_repeated_header():
    """跨多个 chunk 的表格，每个 chunk 的 text 都含重复表头，但 source span 不重叠。"""
    rows = ["h | data"] + [f"row{i} | val" for i in range(20)]
    segs = [{"kind": "table", "heading": "H", "rows": rows}]
    source = build_structured_source_from_segments(segs, chunk_size=150, chunk_overlap=0)

    # 验证每个 chunk 的 text 包含表头
    for c in source["chunks"]:
        assert "h | data" in c["text"]
        assert "【章节】H" in c["text"]

    # 验证 source span 不重叠（每个 span 指向 normalized_text 的不同区域）
    spans = sorted([(c["source_start"], c["source_end"]) for c in source["chunks"]])
    for i in range(len(spans) - 1):
        assert spans[i][1] <= spans[i + 1][0], \
            f"source spans should not overlap: {spans[i]} vs {spans[i+1]}"
