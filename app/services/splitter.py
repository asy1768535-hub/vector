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


def _check_params(chunk_size: int, chunk_overlap: int) -> None:
    """#9 兜底：生效的 overlap >= size 时给出清晰错误（否则 LangChain 抛裸 ValueError）。

    schema 已在建/改库时校验「两值都给」的情况；这里覆盖只改其一、继承默认值后
    才组成非法组合的场景，统一在切分入口拦截。"""
    if chunk_overlap >= chunk_size:
        raise ValueError(f"chunk_overlap（{chunk_overlap}）必须小于 chunk_size（{chunk_size}）")


def split_text(text: str, *, chunk_size: int, chunk_overlap: int, splitter: str = "text") -> list[str]:
    """按 splitter 类型把 text 切成 chunk 列表。空白块会被过滤。"""
    if not text or not text.strip():
        return []
    _check_params(chunk_size, chunk_overlap)
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


def _table_chunks(seg: dict, chunk_size: int) -> list[str]:
    """表格 segment → chunk：整表一块，头部带【章节】【表格】+表头行；超长按行分组重复表头。"""
    rows = seg.get("rows") or []
    if not rows:
        return []
    heading = seg.get("heading") or ""
    caption = seg.get("caption") or ""
    prefix: list[str] = []
    if heading:
        prefix.append(f"【章节】{heading}")
    if caption and caption != heading:
        prefix.append(f"【表格】{caption}")
    header, data = rows[0], rows[1:]
    ctx = prefix + [header]                       # 每个分组都重复：章节/表名/表头行

    def build(group: list[str]) -> str:
        return "\n".join(ctx + group)

    if not data or len(build(data)) <= chunk_size:
        return [build(data)]
    chunks: list[str] = []
    group: list[str] = []
    base = len(build([]))                          # 上下文本身的长度
    cur = base
    for r in data:
        if group and cur + len(r) + 1 > chunk_size:
            chunks.append(build(group))
            group, cur = [], base
        group.append(r)
        cur += len(r) + 1
    if group:
        chunks.append(build(group))
    return chunks


def chunk_segments(segments: list[dict], *, chunk_size: int, chunk_overlap: int) -> list[str]:
    """表格感知组块：散文带【章节】前缀按 chunk_size 切；每个表格整表成块带上下文。

    segment 结构见 app/services/docx_extract.py:extract_docx_segments。
    """
    _check_params(chunk_size, chunk_overlap)
    rec = _recursive(chunk_size, chunk_overlap)
    out: list[str] = []
    for seg in segments:
        if seg.get("kind") == "table":
            out.extend(_table_chunks(seg, chunk_size))
        else:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            heading = seg.get("heading") or ""
            prefix = f"【章节】{heading}\n" if heading else ""
            pieces = rec.split_text(text) if len(text) > chunk_size else [text]
            out.extend(prefix + p for p in pieces if p.strip())
    return [c for c in out if c.strip()]
