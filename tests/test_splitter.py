"""文本切分单元测试（span-native splitter 行为测试）。"""
from __future__ import annotations

import random

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.services.splitter import (
    _line_location,
    _SEPARATORS,
    split_structured_text,
    split_text,
)


def _reference_chunks(text: str, *, chunk_size: int, chunk_overlap: int, splitter: str = "text") -> list[str]:
    if not text or not text.strip():
        return []
    if splitter == "none":
        return [text]
    recursive = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
        keep_separator=True,
    )
    if splitter == "markdown":
        header = MarkdownHeaderTextSplitter(
            headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]
        )
        out: list[str] = []
        for section in header.split_text(text):
            content = section.page_content
            if not content.strip():
                continue
            if len(content) <= chunk_size:
                out.append(content)
            else:
                out.extend(recursive.split_text(content))
        return [c for c in out if c.strip()]
    return [c for c in recursive.split_text(text) if c.strip()]


def test_splitter_text_returns_chunks_around_size():
    text = "段一。\n\n" + ("很长很长的句子。" * 200)
    chunks = split_text(text, chunk_size=200, chunk_overlap=20, splitter="text")
    assert len(chunks) > 1
    assert all(len(c) <= 400 for c in chunks)


def test_splitter_none_returns_single_chunk():
    text = "abcdef" * 500
    chunks = split_text(text, chunk_size=100, chunk_overlap=10, splitter="none")
    assert chunks == [text]


def test_splitter_markdown_respects_headers():
    md = (
        "# 第一章\n\n这是第一章正文。\n\n"
        "## 第二节\n\n这是第二节正文。\n\n"
        "# 第二章\n\n更多内容。"
    )
    chunks = split_text(md, chunk_size=2000, chunk_overlap=0, splitter="markdown")
    assert len(chunks) >= 3
    assert all(c.strip() for c in chunks)


def test_splitter_empty_returns_empty():
    assert split_text("", chunk_size=100, chunk_overlap=10) == []
    assert split_text("   \n  ", chunk_size=100, chunk_overlap=10) == []


def test_split_structured_text_offsets_match_source_slices():
    text = "第一行\n第二行很长" * 30
    chunks = split_structured_text(text, chunk_size=80, chunk_overlap=10, splitter="text")
    assert [chunk["text"] for chunk in chunks] == split_text(
        text, chunk_size=80, chunk_overlap=10, splitter="text"
    )
    assert chunks
    for chunk in chunks:
        assert text[chunk["source_start"]:chunk["source_end"]] == chunk["text"]
        assert chunk["location"]["type"] == "line"
        assert chunk["location"]["start_line"] >= 1
        # span hash 验证原文一致性
        assert "source_span_hash" in chunk
        assert len(chunk["source_span_hash"]) == 16


def test_split_structured_text_markdown_preserves_split_text_output():
    text = "# A\n\n" + ("alpha beta gamma\n" * 20) + "\n## B\n\n" + ("delta epsilon\n" * 20)
    chunks = split_structured_text(text, chunk_size=80, chunk_overlap=10, splitter="markdown")

    assert [chunk["text"] for chunk in chunks] == split_text(
        text, chunk_size=80, chunk_overlap=10, splitter="markdown"
    )
    for chunk in chunks:
        assert text[chunk["source_start"]:chunk["source_end"]] == chunk["text"]
        assert "source_span_hash" in chunk


def _assert_matches_reference(text: str, *, chunk_size: int, chunk_overlap: int, splitter: str = "text") -> None:
    expected = _reference_chunks(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, splitter=splitter)
    structured = split_structured_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, splitter=splitter)

    assert split_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, splitter=splitter) == expected
    assert [chunk["text"] for chunk in structured] == expected
    for chunk in structured:
        assert text[chunk["source_start"]:chunk["source_end"]] == chunk["text"]


def test_split_text_matches_langchain_reference_for_chinese_punctuation():
    _assert_matches_reference("第一段。第二句。第三句。第四句。", chunk_size=8, chunk_overlap=2)


def test_split_text_matches_langchain_reference_for_english_spaces():
    _assert_matches_reference("alpha beta gamma delta epsilon zeta eta theta", chunk_size=16, chunk_overlap=4)


def test_markdown_matches_reference_and_does_not_add_headers_to_chunks():
    text = "# A\n\nalpha beta gamma\n\n## B\n\ndelta epsilon zeta\n\n### C\n\neta theta"
    expected = _reference_chunks(text, chunk_size=20, chunk_overlap=4, splitter="markdown")

    _assert_matches_reference(text, chunk_size=20, chunk_overlap=4, splitter="markdown")
    assert expected
    assert all(not chunk.lstrip().startswith("#") for chunk in expected)


def test_overlap_merge_keeps_chunks_within_size_when_possible():
    text = "aa aa aa bbbbbbbb"
    expected = _reference_chunks(text, chunk_size=10, chunk_overlap=4)

    _assert_matches_reference(text, chunk_size=10, chunk_overlap=4)
    assert all(len(chunk) <= 10 for chunk in expected)
    assert all(len(chunk) <= 10 for chunk in split_text(text, chunk_size=10, chunk_overlap=4))


def test_random_texts_match_langchain_reference_and_source_slices():
    alphabet = "abcde     。！？\n"
    rng = random.Random(20260706)
    for _ in range(25):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(20, 120)))
        if not text.strip():
            continue
        _assert_matches_reference(text, chunk_size=18, chunk_overlap=5)


def test_split_structured_text_none_returns_full_span():
    text = "alpha\nbeta"
    chunks = split_structured_text(text, chunk_size=100, chunk_overlap=0, splitter="none")
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk["text"] == text
    assert chunk["source_start"] == 0
    assert chunk["source_end"] == len(text)
    assert chunk["location"] == {"type": "line", "start_line": 1, "end_line": 2}
    assert "source_span_hash" in chunk


# ── Task 1: 删除源码检查测试，改为真实行为测试 ──

def test_repeated_text_maps_to_correct_positions():
    """重复段落必须各自映射到正确的出现位置。"""
    para = "重复段落ABC。"
    text = para + "\n\n" + "独有内容一。\n\n" + para + "\n\n" + "独有内容二。"
    chunks = split_structured_text(text, chunk_size=20, chunk_overlap=0, splitter="text")

    # 找到两个包含重复内容的 chunk
    hits = [c for c in chunks if "重复段落ABC" in c["text"]]
    assert len(hits) == 2, f"应该有两个 chunk 包含重复段落，实际: {[c['source_start'] for c in hits]}"
    # 验证各自指向原文正确位置
    pos0 = text.index("重复段落ABC")
    pos1 = text.index("重复段落ABC", pos0 + 1)
    assert hits[0]["source_start"] <= pos0 < hits[0]["source_end"]
    assert hits[1]["source_start"] <= pos1 < hits[1]["source_end"]
    assert hits[0]["source_start"] != hits[1]["source_start"], "两个 chunk 必须映射到不同位置"


def test_overlap_chunks_have_correct_boundaries():
    """overlap 边界正确：chunk 的 start/end 准确反映在原文中的位置。"""
    text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # 26 chars
    chunks = split_structured_text(text, chunk_size=10, chunk_overlap=4, splitter="text")
    assert len(chunks) >= 2
    for chunk in chunks:
        assert text[chunk["source_start"]:chunk["source_end"]] == chunk["text"]
        assert 0 <= chunk["source_start"] < chunk["source_end"] <= len(text)
    # overlap 意味着 chunk[i] 的结尾部分与 chunk[i+1] 的开头部分重合
    for i in range(len(chunks) - 1):
        # 有 overlap 时，chunk[i].end > chunk[i+1].start
        assert chunks[i]["source_end"] > chunks[i + 1]["source_start"]


def test_span_native_splitter_produces_consistent_start_end_with_split_text():
    """split_text 和 split_structured_text 对同一输入产生一致的文本和位置。"""
    text = "第一章\n\n第二章\n\n第三章\n\n" + ("内容。" * 100)
    for sp in ("text", "markdown", "none"):
        flat = split_text(text, chunk_size=100, chunk_overlap=10, splitter=sp)
        structured = split_structured_text(text, chunk_size=100, chunk_overlap=10, splitter=sp)
        assert [c["text"] for c in structured] == flat
        for c in structured:
            assert 0 <= c["source_start"] < c["source_end"] <= len(text)


def test_line_location_correct():
    """_line_location 正确计算行号。"""
    text = "line1\nline2\nline3"
    loc = _line_location(text, 6, 11)  # "line2"
    assert loc == {"type": "line", "start_line": 2, "end_line": 2}

    loc2 = _line_location(text, 0, 5)  # "line1"
    assert loc2 == {"type": "line", "start_line": 1, "end_line": 1}

    loc3 = _line_location(text, 0, len(text))  # whole
    assert loc3 == {"type": "line", "start_line": 1, "end_line": 3}


def test_split_text_no_find_index_in_structured_path():
    """结构化切分路径不使用 find/index/regex 回查已生成的 chunk 来定位。"""
    # 这个测试验证 split_structured_text 返回的 start/end 直接来自 _do_split
    # （即 split_text 和 split_structured_text 产生相同文本，位置一致）。
    text = "段落A。\n\n段落B。\n\n段落C。" * 10
    flat = split_text(text, chunk_size=40, chunk_overlap=5, splitter="text")
    structured = split_structured_text(text, chunk_size=40, chunk_overlap=5, splitter="text")
    assert [c["text"] for c in structured] == flat
    for c in structured:
        assert text[c["source_start"]:c["source_end"]] == c["text"]
        assert "source_span_hash" in c
