"""表格感知组块 chunk_segments 单测（纯函数，不连服务）。"""
from __future__ import annotations

from app.services.splitter import chunk_segments


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
