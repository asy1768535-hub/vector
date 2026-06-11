"""文本切分。

- text / json：RecursiveCharacterTextSplitter（中英文分隔符）
- markdown：先按标题切，再按 chunk_size 切
- none：整段当一个 chunk
"""
from __future__ import annotations

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

_SEPARATORS = ["\n\n", "\n", "。", "！", "？", ". ", "? ", "! ", " ", ""]
_MARKDOWN_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]


def _recursive(chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
        keep_separator=True,
    )


def split_text(text: str, *, chunk_size: int, chunk_overlap: int, splitter: str = "text") -> list[str]:
    """按 splitter 类型把 text 切成 chunk 列表。空白块会被过滤。"""
    if not text or not text.strip():
        return []
    if splitter == "none":
        return [text]
    if splitter == "markdown":
        header = MarkdownHeaderTextSplitter(headers_to_split_on=_MARKDOWN_HEADERS)
        recursive = _recursive(chunk_size, chunk_overlap)
        sections = header.split_text(text)
        out: list[str] = []
        for section in sections:
            content = section.page_content
            if not content.strip():
                continue
            if len(content) <= chunk_size:
                out.append(content)
            else:
                out.extend(recursive.split_text(content))
        return [c for c in out if c.strip()]
    # default: text / json
    return [c for c in _recursive(chunk_size, chunk_overlap).split_text(text) if c.strip()]
