"""文本切分。

- text / json：RecursiveCharacterTextSplitter（中英文分隔符）
- markdown：先按标题切，再按 chunk_size 切
- none：整段当一个 chunk
"""
from __future__ import annotations

import hashlib

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

_SEPARATORS = ["\n\n", "\n", "。", "！", "？", ". ", "? ", "! ", " ", ""]
_HEADERS_TO_SPLIT_ON = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]


def _check_params(chunk_size: int, chunk_overlap: int) -> None:
    """overlap >= size 时给出清晰错误（否则 LangChain 抛裸 ValueError）。"""
    if chunk_overlap >= chunk_size:
        raise ValueError(f"chunk_overlap（{chunk_overlap}）必须小于 chunk_size（{chunk_size}）")


def _span_hash(text: str) -> str:
    """原文 span 校验用的短哈希（非加密用途）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _recursive(chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
        keep_separator=True,
    )


def _split_pieces(text: str, *, chunk_size: int, chunk_overlap: int, splitter: str) -> list[str]:
    if not text or not text.strip():
        return []
    if splitter == "none":
        return [text]
    recursive = _recursive(chunk_size, chunk_overlap)
    if splitter == "markdown":
        header = MarkdownHeaderTextSplitter(headers_to_split_on=_HEADERS_TO_SPLIT_ON)
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


def _scan_piece_exact(text: str, piece: str, cursor: int) -> tuple[int, int] | None:
    max_start = len(text) - len(piece)
    start = max(0, min(cursor, len(text)))
    while start <= max_start:
        pos = 0
        while pos < len(piece) and text[start + pos] == piece[pos]:
            pos += 1
        if pos == len(piece):
            return start, start + len(piece)
        start += 1
    return None


def _scan_piece(text: str, piece: str, cursor: int) -> tuple[int, int]:
    exact = _scan_piece_exact(text, piece, cursor)
    if exact is not None:
        return exact

    for variant in (piece.replace("  \n", "\n\n"), piece.replace("  \n", "\n")):
        if variant == piece:
            continue
        match = _scan_piece_exact(text, variant, cursor)
        if match is not None:
            return match
    raise ValueError("structured splitter could not map chunk to source text")


def _split_spans(text: str, *, chunk_size: int, chunk_overlap: int, splitter: str) -> list[dict]:
    pieces = _split_pieces(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, splitter=splitter)
    spans: list[dict] = []
    cursor = 0
    for piece in pieces:
        start, end = _scan_piece(text, piece, cursor)
        spans.append({"text": piece, "start": start, "end": end})
        cursor = max(start + 1, end - chunk_overlap)
    return spans


def split_text(text: str, *, chunk_size: int, chunk_overlap: int, splitter: str = "text") -> list[str]:
    """按 splitter 类型把 text 切成 chunk 列表。空白块会被过滤。"""
    if not text or not text.strip():
        return []
    _check_params(chunk_size, chunk_overlap)
    return _split_pieces(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, splitter=splitter)


# ── 结构化切片 ──────────────────────────────────────────────────

def _line_location(text: str, start: int, end: int) -> dict:
    return {
        "type": "line",
        "start_line": text.count("\n", 0, start) + 1,
        "end_line": text.count("\n", 0, max(start, end - 1)) + 1,
    }


def _structured_location(text: str, start: int, end: int, base_location: dict | None) -> dict:
    if base_location:
        return dict(base_location)
    return _line_location(text, start, end)


def split_structured_text(
    text: str, *, chunk_size: int, chunk_overlap: int, splitter: str = "text", base_location: dict | None = None
) -> list[dict]:
    """结构化切分：chunk 文本与 LangChain 输出一致，并补充 start/end + location。"""
    if not text or not text.strip():
        return []
    _check_params(chunk_size, chunk_overlap)

    spans = _split_spans(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, splitter=splitter)

    return [
        {
            "text": s["text"],
            "source_start": s["start"],
            "source_end": s["end"],
            "location": _structured_location(text, s["start"], s["end"], base_location),
            "source_span_hash": _span_hash(text[s["start"]:s["end"]]),
        }
        for s in spans
        if s["text"].strip()
    ]


# ═══════════════════════════════════════════════════════════════
#  表格感知组块（chunk_segments / build_structured_source）
# ═══════════════════════════════════════════════════════════════

def _segment_flat_text(seg: dict) -> str:
    """segment → flat text（放入 normalized_text 的原文，不含重复表头/前缀）。"""
    if seg.get("kind") == "table":
        heading = seg.get("heading") or ""
        caption = seg.get("caption") or ""
        prefix: list[str] = []
        if heading:
            prefix.append(f"【章节】{heading}")
        if caption and caption != heading:
            prefix.append(f"【表格】{caption}")
        return "\n".join(prefix + list(seg.get("rows") or []))
    # prose
    text = (seg.get("text") or "").strip()
    heading = seg.get("heading") or ""
    if heading:
        text = f"【章节】{heading}\n{text}"
    return text


def _segment_location(seg: dict) -> dict:
    if seg.get("kind") == "table":
        location = dict(seg.get("location") or {})
        if not location:
            heading = seg.get("heading") or seg.get("caption") or ""
            location = {"type": "table", "heading": heading}
        return location
    return dict(seg.get("location") or {"type": "paragraph", "heading": seg.get("heading") or ""})


# ── 表格 chunk（span-native）────────────────────────────────────

def _table_chunk_spans(
    seg: dict, *, chunk_size: int, chunk_overlap: int
) -> list[dict]:
    """表格 segment → chunk span 列表。

    返回每个 chunk 的:
      - text: 检索用文本（含【章节】/【表格】/表头前缀，可能重复）
      - source_start / source_end: 在 flat segment text 中的偏移（指向数据行区域）
      - row_start_idx / row_end_idx: 数据行在 rows 中的索引（0-based，rows[0] 为表头）
      - location: sheet_row / table 位置信息

    每 chunk 的 text = prefix + 表头 + 该组数据行。
    source_start/source_end 指向该组数据行在 flat text 中的位置。
    """
    rows = list(seg.get("rows") or [])
    if not rows:
        return []

    heading = seg.get("heading") or ""
    caption = seg.get("caption") or ""
    prefix: list[str] = []
    if heading:
        prefix.append(f"【章节】{heading}")
    if caption and caption != heading:
        prefix.append(f"【表格】{caption}")

    header = rows[0]
    data_rows = rows[1:]  # 不含表头的数据行
    row_numbers = list(seg.get("row_numbers") or [])
    location = dict(seg.get("location") or {})

    full_prefix = prefix + [header]
    flat = "\n".join(prefix + rows)  # 原文（一段落，无重复）

    def _build_chunk_text(data_group: list[str]) -> str:
        return "\n".join(full_prefix + data_group)

    if not data_rows or len(_build_chunk_text(data_rows)) <= chunk_size:
        # 整个表一个 chunk
        full_text = _build_chunk_text(data_rows)
        return [{
            "text": full_text,
            "source_start": 0,
            "source_end": len(flat),
            "row_start_idx": 0,
            "row_end_idx": max(0, len(data_rows) - 1),
            "location": _table_chunk_location(seg, row_numbers,
                                              data_start_idx=0, data_end_idx=max(0, len(data_rows) - 1)),
        }]

    # 超长：按数据行分组，每组重复前缀 + 表头
    chunks: list[dict] = []
    # 预计算每行在 flat 中的起始位置
    # flat = prefix_lines + \n + header + \n + data_0 + \n + data_1 + ...
    # 我们只需要数据行的位置
    # 前导长度 = len("\n".join(prefix + [header])) + 1  (the \n before first data row)
    preamble_len = len("\n".join(prefix + [header])) + 1 if data_rows else len(flat)

    row_positions: list[int] = []
    pos = preamble_len
    for i, r in enumerate(data_rows):
        row_positions.append(pos)
        if i < len(data_rows) - 1:
            pos += len(r) + 1  # +1 for \n
        else:
            pos += len(r)

    group: list[str] = []
    group_start_idx: int = 0
    cur_len = len("\n".join(full_prefix))

    for idx, r in enumerate(data_rows):
        add_len = (1 if group else 0) + len(r)  # \n separator + row
        if group and cur_len + add_len > chunk_size:
            full_text = _build_chunk_text(group)
            src_start = row_positions[group_start_idx]
            src_end = row_positions[group_start_idx + len(group) - 1] + len(data_rows[group_start_idx + len(group) - 1])
            chunks.append({
                "text": full_text,
                "source_start": src_start,
                "source_end": src_end,
                "row_start_idx": group_start_idx,
                "row_end_idx": group_start_idx + len(group) - 1,
                "location": _table_chunk_location(seg, row_numbers,
                                                  data_start_idx=group_start_idx,
                                                  data_end_idx=group_start_idx + len(group) - 1),
            })
            group_start_idx = idx
            group = []
            cur_len = len("\n".join(full_prefix))

        group.append(r)
        cur_len += add_len

    if group:
        full_text = _build_chunk_text(group)
        src_start = row_positions[group_start_idx]
        src_end = row_positions[group_start_idx + len(group) - 1] + len(data_rows[group_start_idx + len(group) - 1])
        chunks.append({
            "text": full_text,
            "source_start": src_start,
            "source_end": src_end,
            "row_start_idx": group_start_idx,
            "row_end_idx": group_start_idx + len(group) - 1,
            "location": _table_chunk_location(seg, row_numbers,
                                              data_start_idx=group_start_idx,
                                              data_end_idx=group_start_idx + len(group) - 1),
        })

    return chunks


def _table_chunk_location(
    seg: dict, row_numbers: list[int], *, data_start_idx: int, data_end_idx: int
) -> dict:
    """表格 chunk 位置：基于切分时携带的原始行号索引，不用 row in chunk 推测。"""
    location = dict(seg.get("location") or {})

    if location.get("type") == "sheet" and row_numbers:
        # data_start_idx / data_end_idx 是 data_rows 中的索引
        # 映射: data_rows[data_start_idx] 对应 rows[data_start_idx + 1]
        # row_numbers 与 rows 等长，所以 row_numbers[data_start_idx + 1] 是原始行号
        actual_start_idx = data_start_idx + 1  # skip header row
        actual_end_idx = data_end_idx + 1
        if 0 <= actual_start_idx < len(row_numbers) and 0 <= actual_end_idx < len(row_numbers):
            return {
                "type": "sheet_row",
                "sheet": location.get("sheet"),
                "start_row": row_numbers[actual_start_idx],
                "end_row": row_numbers[actual_end_idx],
            }

    if location:
        return location
    return {"type": "table", "heading": seg.get("heading") or seg.get("caption") or ""}


# ── 公开：chunk_segments / build_structured_source_from_segments ─

def chunk_segments(segments: list[dict], *, chunk_size: int, chunk_overlap: int) -> list[str]:
    """表格感知组块（返回 str 列表，用于旧代码路径）。"""
    _check_params(chunk_size, chunk_overlap)
    source = build_structured_source_from_segments(segments, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return [c["text"] for c in source["chunks"] if c["text"].strip()]


def build_structured_source_from_segments(
    segments: list[dict], *, chunk_size: int, chunk_overlap: int
) -> dict:
    """从 segments 构建 {normalized_text, chunks}。

    - normalized_text：按 segment 原顺序拼接 flat text（不重复表头/前缀/overlap 内容）。
    - chunks：每个 chunk 含 text（检索用，可含前缀）+ source_start/source_end（指向
      normalized_text 中命中的数据区域）+ location。
    """
    _check_params(chunk_size, chunk_overlap)

    # ── 第一遍：建每个 segment 的 flat text + chunk 信息 ──
    seg_records: list[dict] = []  # [{flat_text, chunks: [{text, seg_local_start, seg_local_end, ...}]}]
    for seg in segments:
        flat = _segment_flat_text(seg)
        if not flat.strip():
            continue
        if seg.get("kind") == "table":
            recs = _table_chunk_spans(seg, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        else:
            recs = []
            for s in split_structured_text(
                flat,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                splitter="text",
                base_location=_segment_location(seg),
            ):
                recs.append({
                    "text": s["text"],
                    "source_start": s["source_start"],
                    "source_end": s["source_end"],
                    "location": s["location"],
                })
        seg_records.append({"flat": flat, "chunks": recs, "seg": seg})

    # ── 第二遍：拼接 normalized_text + 计算全局 offset ──
    parts: list[str] = []
    offset_map: list[dict] = []  # [{global_base, flat_len}]
    cursor = 0
    for sr in seg_records:
        flat = sr["flat"]
        if parts:
            parts.append("\n\n")
            cursor += 2
        global_base = cursor
        parts.append(flat)
        cursor += len(flat)
        offset_map.append({"global_base": global_base, "flat_len": len(flat)})

    normalized_text = "".join(parts)

    # ── 第三遍：组装最终 chunks ──
    chunks: list[dict] = []
    for idx, sr in enumerate(seg_records):
        gbase = offset_map[idx]["global_base"]
        for rec in sr["chunks"]:
            loc = rec.get("location") or _segment_location(sr["seg"])
            src_start = gbase + rec["source_start"]
            src_end = gbase + rec["source_end"]
            chunks.append({
                "text": rec["text"],
                "source_start": src_start,
                "source_end": src_end,
                "location": loc,
                "source_span_hash": _span_hash(normalized_text[src_start:src_end]),
            })

    return {"normalized_text": normalized_text, "chunks": chunks}
