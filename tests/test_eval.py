"""检索评测纯逻辑单测（不连 DB / 网络）。"""
from __future__ import annotations

import pytest

from app.services.eval import (
    EvalItem,
    Retrieved,
    aggregate,
    first_hit_rank,
    format_table,
    is_hit,
    normalize,
)


def _item(**kw) -> EvalItem:
    kw.setdefault("query", "q")
    return EvalItem(**kw)


# ---- is_hit ----

def test_is_hit_by_doc_id():
    item = _item(expected_doc_ids=["d1", "d2"])
    assert is_hit("d2", "无关正文", item) is True
    assert is_hit("d9", "无关正文", item) is False


def test_is_hit_by_text_substring_normalized():
    item = _item(expected_text=["Hello World"])
    # 大小写 + 多空白都应归一化后匹配
    assert is_hit("", "...  hello   WORLD ...", item) is True
    assert is_hit("", "no match here", item) is False


def test_is_hit_empty_doc_id_does_not_match():
    item = _item(expected_doc_ids=["d1"])  # 无 expected_text
    assert is_hit("", "anything", item) is False


def test_normalize():
    assert normalize("  A  b\tC\n") == "a b c"


# ---- first_hit_rank ----

def test_first_hit_rank_returns_first_position():
    item = _item(expected_doc_ids=["d3"])
    recs = [Retrieved("d1", ""), Retrieved("d3", ""), Retrieved("d3", "")]
    assert first_hit_rank(recs, item) == 2


def test_first_hit_rank_none_when_no_match():
    item = _item(expected_doc_ids=["dX"])
    recs = [Retrieved("d1", ""), Retrieved("d2", "")]
    assert first_hit_rank(recs, item) is None


# ---- aggregate ----

def test_aggregate_hit_at_k_boundary_and_mrr():
    # 名次分别 1 / 3 / None；ks=(1,3,5)
    ranks = [1, 3, None]
    agg = aggregate(ranks, ks=(1, 3, 5))
    assert agg["n"] == 3
    assert agg["hit@1"] == pytest.approx(1 / 3)        # 只有名次1
    assert agg["hit@3"] == pytest.approx(2 / 3)        # 名次3 恰好 = k 算命中
    assert agg["hit@5"] == pytest.approx(2 / 3)        # None 不计
    assert agg["mrr"] == pytest.approx((1 / 1 + 1 / 3 + 0) / 3)


def test_aggregate_recall_averages_only_valid():
    agg = aggregate([1, 2], recalls=[0.5, None, 1.0])
    assert agg["recall"] == pytest.approx(0.75)        # None 被剔除


def test_aggregate_empty():
    agg = aggregate([], ks=(1, 3, 5))
    assert agg["n"] == 0 and agg["hit@1"] == 0.0 and agg["mrr"] == 0.0 and agg["recall"] is None


# ---- format_table ----

def test_format_table_plain_has_header_and_one_row_per_config():
    rows = [
        {"config": "dense", "hit@1": 0.5, "hit@3": 0.75, "hit@5": 1.0, "mrr": 0.66, "recall": 0.8, "n": 4, "errors": 0},
        {"config": "rerank", "hit@1": 0.75, "hit@3": 1.0, "hit@5": 1.0, "mrr": 0.85, "recall": None, "n": 4, "errors": 1},
    ]
    txt = format_table(rows)
    lines = txt.splitlines()
    assert "config" in lines[0] and "MRR" in lines[0] and "Recall@5" in lines[0]
    assert len(lines) == 2 + len(rows)        # 表头 + 分隔线 + 数据行
    assert "50.0%" in txt and "0.660" in txt  # 百分比 + MRR 三位小数
    assert "-" in lines[-1]                    # recall=None → "-"


def test_format_table_markdown():
    rows = [{"config": "dense", "hit@1": 1.0, "hit@3": 1.0, "hit@5": 1.0, "mrr": 1.0, "recall": 1.0, "n": 1, "errors": 0}]
    md = format_table(rows, markdown=True)
    assert md.startswith("| config |")
    assert "| --- |" in md


# ---- EvalItem.from_dict ----

def test_from_dict_coerces_scalar_to_list_and_validates():
    item = EvalItem.from_dict({"query": "hi", "expected_doc_ids": "d1", "expected_text": "frag"})
    assert item.expected_doc_ids == ["d1"] and item.expected_text == ["frag"]


def test_from_dict_requires_query():
    with pytest.raises(ValueError):
        EvalItem.from_dict({"expected_doc_ids": ["d1"]})


def test_from_dict_requires_some_expectation():
    with pytest.raises(ValueError):
        EvalItem.from_dict({"query": "hi"})


# ---- 表格类样本（3D）：期望片段命中 'header | value' 渲染文本 ----

def test_is_hit_table_segment_expected_text():
    """xlsx/docx 表格切块渲染成 '表头 | 值' 行；表格类评测样本用 expected_text 命中其中一行。"""
    item = _item(expected_text=["数据库 | MySQL"])
    chunk = "【章节】技术选型\n类别 | 方案\n数据库 | MySQL\n缓存 | Redis"
    assert is_hit("anydoc", chunk, item) is True
    # 同表但缺该行 → 不命中
    assert is_hit("anydoc", "【章节】技术选型\n缓存 | Redis", item) is False


def test_table_sample_from_dict_roundtrip():
    """dataset.example.jsonl 里的表格类样本能被 from_dict 正常解析（含 library/note）。"""
    item = EvalItem.from_dict({
        "query": "技术选型里数据库用的什么？",
        "library": "construction",
        "expected_text": "数据库 | MySQL",
        "note": "表格类",
    })
    assert item.query.startswith("技术选型")
    assert item.library == "construction"
    assert item.expected_text == ["数据库 | MySQL"]
