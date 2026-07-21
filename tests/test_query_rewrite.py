"""Query Rewrite 第一版单测：normalize_query / expand_query / load_synonyms。"""
from __future__ import annotations

import json

from app.services import query_rewrite as qr


# ── normalize_query ────────────────────────────────────────────────────────
def test_normalize_fullwidth_to_halfwidth():
    assert qr.normalize_query("ＡＢＣ１２３") == "ABC123"


def test_normalize_fullwidth_space_and_extra_spaces():
    # 全角空格 + 多个半角空格 → 单个半角空格，并 strip
    assert qr.normalize_query("你好　世界") == "你好 世界"
    assert qr.normalize_query("  a    b   ") == "a b"


def test_normalize_book_title_and_quote_marks_dropped():
    assert qr.normalize_query("《劳动合同法》") == "劳动合同法"
    assert qr.normalize_query("「年假」「工伤」") == "年假 工伤".replace(" ", "")  # 引号去掉、无新增空格


def test_normalize_fullwidth_brackets():
    # 全角圆括号 → 半角；中文方括号 → 半角
    assert qr.normalize_query("（试行）") == "(试行)"
    assert qr.normalize_query("【附则】") == "[附则]"


def test_normalize_clause_number_spaces_removed():
    assert qr.normalize_query("第 38 条") == "第38条"
    assert qr.normalize_query("第 3 款 第 2 项") == "第3款 第2项"


def test_normalize_docno_format():
    # 文号：全角括号→半角、内部/尾部空格清理
    assert qr.normalize_query("国发〔2020〕5 号") == "国发[2020]5号"


def test_normalize_empty():
    assert qr.normalize_query("") == ""
    assert qr.normalize_query(None) == ""  # 容错


# ── expand_query ───────────────────────────────────────────────────────────
def test_expand_always_keeps_original_query():
    # 原始 query 必保留（即便没有任何同义词命中）
    out = qr.expand_query("劳动合同法第38条", {}, max_queries=4)
    assert out[0] == "劳动合同法第38条"
    assert "劳动合同法第38条" in out


def test_expand_keeps_raw_original_even_after_normalization():
    # 原始含全角：原始(全角) 必在结果里，规范化形式作为额外 query 追加
    out = qr.expand_query("ＡＢＣ", {}, max_queries=4)
    assert out[0] == "ＡＢＣ"
    assert "ABC" in out


def test_expand_synonym_expansion_applies():
    out = qr.expand_query("劳动合同法第38条", {"劳动合同法": ["劳动法"]}, max_queries=4)
    assert "劳动合同法第38条" in out          # 原始保留
    assert "劳动法第38条" in out              # 同义词替换生效


def test_expand_multiple_synonyms_and_cap():
    syn = {"社保": ["社会保险", "五险一金", "公积金缴纳"]}
    out = qr.expand_query("社保怎么交", syn, max_queries=3)
    assert out[0] == "社保怎么交"
    assert len(out) <= 3                       # 不超过 max_queries
    assert "社会保险怎么交" in out


def test_expand_no_match_returns_only_original():
    out = qr.expand_query("今天天气", {"工伤": ["职业伤害"]}, max_queries=4)
    assert out == ["今天天气"]                 # 无命中、规范化无变化 → 仅原始


def test_expand_dedup():
    # 同义词替换后与原始相同 → 不重复
    out = qr.expand_query("年假", {"年假": ["年假", "年休假"]}, max_queries=4)
    assert out.count("年假") == 1
    assert "年休假" in out


# ── load_synonyms ──────────────────────────────────────────────────────────
def test_load_synonyms_missing_file_returns_empty(tmp_path):
    p = tmp_path / "nope.json"
    assert qr.load_synonyms(str(p), use_cache=False) == {}


def test_load_synonyms_parses_and_coerces(tmp_path):
    p = tmp_path / "syn.json"
    p.write_text(json.dumps({
        "社保": ["社会保险", "五险一金"],
        "年假": "年休假",            # 字符串 → 单元素列表
        "脏数据": 123,                # 非法值 → 丢弃
    }), encoding="utf-8")
    out = qr.load_synonyms(str(p), use_cache=False)
    assert out["社保"] == ["社会保险", "五险一金"]
    assert out["年假"] == ["年休假"]
    assert "脏数据" not in out


def test_load_synonyms_ignores_underscore_keys(tmp_path):
    # 下划线开头的 key 视为注释/元信息，不作为词条加载
    p = tmp_path / "syn.json"
    p.write_text(json.dumps({
        "_comment": ["说明文字", "不应被当词条"],
        "_meta": "版本信息",
        "社保": ["社会保险"],
    }), encoding="utf-8")
    out = qr.load_synonyms(str(p), use_cache=False)
    assert "_comment" not in out
    assert "_meta" not in out
    assert out == {"社保": ["社会保险"]}


def test_load_synonyms_bad_json_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert qr.load_synonyms(str(p), use_cache=False) == {}


def test_example_dictionary_is_valid_json():
    # 仓库内置示例词典必须能被加载器吃下（CI 防回归）
    out = qr.load_synonyms("config/query_synonyms.example.json", use_cache=False)
    assert isinstance(out, dict) and out.get("劳动合同法")
