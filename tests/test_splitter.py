"""文本切分单元测试。"""
from __future__ import annotations

import pytest

from app.services.splitter import split_text


def test_splitter_text_returns_chunks_around_size():
    text = "段一。\n\n" + ("很长很长的句子。" * 200)
    chunks = split_text(text, chunk_size=200, chunk_overlap=20, splitter="text")
    assert len(chunks) > 1
    # 每个 chunk 不应远超 chunk_size（允许少量浮动，LangChain 在分隔符边界可能略超）
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
    # 至少切出 3 个章节
    assert len(chunks) >= 3
    # 每个 chunk 应该非空
    assert all(c.strip() for c in chunks)


def test_splitter_empty_returns_empty():
    assert split_text("", chunk_size=100, chunk_overlap=10) == []
    assert split_text("   \n  ", chunk_size=100, chunk_overlap=10) == []
