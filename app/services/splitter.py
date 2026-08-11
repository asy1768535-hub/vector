"""文本切分。

- text / json：RecursiveCharacterTextSplitter（中英文分隔符）
- markdown：先按标题切，再按 chunk_size 切
- none：整段当一个 chunk
"""
from __future__ import annotations

import hashlib

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.services.parser_units import build_parser_unit, parser_provenance, text_range

_SEPARATORS = ["\n\n", "\n", "。", "！", "？", ". ", "? ", "! ", " ", ""]
_HEADERS_TO_SPLIT_ON = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]
_SORTED_HEADERS = sorted(_HEADERS_TO_SPLIT_ON, key=lambda split: len(split[0]), reverse=True)


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
    if splitter == "markdown":
        return _split_markdown_spans(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    pieces = _split_pieces(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, splitter=splitter)
    spans: list[dict] = []
    cursor = 0
    for piece in pieces:
        start, end = _scan_piece(text, piece, cursor)
        spans.append({"text": piece, "start": start, "end": end})
        cursor = max(start + 1, end - chunk_overlap)
    return spans


def _iter_lines_with_offsets(text: str) -> list[tuple[str, int, int]]:
    lines: list[tuple[str, int, int]] = []
    cursor = 0
    for line in text.split("\n"):
        start = cursor
        end = start + len(line)
        lines.append((line, start, end))
        cursor = end + 1
    return lines


def _stripped_line_and_segments(line: str, start: int) -> tuple[str, list[dict]]:
    left = len(line) - len(line.lstrip())
    right = len(line.rstrip())
    chars: list[str] = []
    positions: list[int] = []
    for offset, ch in enumerate(line[left:right], start=left):
        if ch.isprintable():
            chars.append(ch)
            positions.append(start + offset)

    segments: list[dict] = []
    if positions:
        content_start = 0
        source_start = positions[0]
        previous = positions[0]
        for content_index, source_pos in enumerate(positions[1:], start=1):
            if source_pos != previous + 1:
                segments.append({
                    "content_start": content_start,
                    "content_end": content_index,
                    "start": source_start,
                    "end": previous + 1,
                })
                content_start = content_index
                source_start = source_pos
            previous = source_pos
        segments.append({
            "content_start": content_start,
            "content_end": len(positions),
            "start": source_start,
            "end": previous + 1,
        })
    return "".join(chars), segments


def _header_match(stripped_line: str) -> tuple[str, str] | None:
    for sep, name in _SORTED_HEADERS:
        if stripped_line.startswith(sep) and (len(stripped_line) == len(sep) or stripped_line[len(sep)] == " "):
            return sep, name
    return None


def _content_record(content: list[str], ranges: list[list[dict]], metadata: dict) -> dict:
    text = "\n".join(content)
    segments: list[dict] = []
    cursor = 0
    for i, item in enumerate(content):
        for source in ranges[i]:
            segments.append({
                "content_start": cursor + source["content_start"],
                "content_end": cursor + source["content_end"],
                "start": source["start"],
                "end": source["end"],
            })
        cursor += len(item)
        if i < len(content) - 1:
            cursor += 1
    return {"content": text, "metadata": dict(metadata), "segments": segments}


def _markdown_line_records(text: str) -> list[dict]:
    records: list[dict] = []
    current_content: list[str] = []
    current_ranges: list[list[dict]] = []
    current_metadata: dict[str, str] = {}
    initial_metadata: dict[str, str] = {}
    header_stack: list[dict] = []
    in_code_block = False
    opening_fence = ""

    def flush() -> None:
        nonlocal current_content, current_ranges
        if current_content:
            records.append(_content_record(current_content, current_ranges, current_metadata))
            current_content = []
            current_ranges = []

    for raw_line, line_start, _line_end in _iter_lines_with_offsets(text):
        stripped_line, line_segments = _stripped_line_and_segments(raw_line, line_start)
        if not in_code_block:
            if stripped_line.startswith("```") and stripped_line.count("```") == 1:
                in_code_block = True
                opening_fence = "```"
            elif stripped_line.startswith("~~~"):
                in_code_block = True
                opening_fence = "~~~"
        elif stripped_line.startswith(opening_fence):
            in_code_block = False
            opening_fence = ""

        if in_code_block:
            current_content.append(stripped_line)
            current_ranges.append(line_segments)
            continue

        header = _header_match(stripped_line)
        if header:
            sep, name = header
            current_header_level = sep.count("#")
            while header_stack and header_stack[-1]["level"] >= current_header_level:
                popped = header_stack.pop()
                initial_metadata.pop(popped["name"], None)
            header_text = stripped_line[len(sep):].strip()
            header_stack.append({"level": current_header_level, "name": name, "data": header_text})
            initial_metadata[name] = header_text
            flush()
        else:
            if stripped_line:
                current_content.append(stripped_line)
                current_ranges.append(line_segments)
            elif current_content:
                flush()

        current_metadata = initial_metadata.copy()

    flush()
    return records


def _aggregate_markdown_records(records: list[dict]) -> list[dict]:
    aggregated: list[dict] = []
    for record in records:
        if aggregated and aggregated[-1]["metadata"] == record["metadata"]:
            base = len(aggregated[-1]["content"])
            aggregated[-1]["content"] += "  \n" + record["content"]
            for segment in record["segments"]:
                aggregated[-1]["segments"].append({
                    **segment,
                    "content_start": base + 3 + segment["content_start"],
                    "content_end": base + 3 + segment["content_end"],
                })
        else:
            aggregated.append({
                "content": record["content"],
                "metadata": dict(record["metadata"]),
                "segments": [dict(s) for s in record["segments"]],
            })
    return aggregated


def _source_ranges_for_content_span(text: str, segments: list[dict], start: int, end: int) -> list[dict]:
    merged: list[dict] = []
    for segment in segments:
        overlap_start = max(start, segment["content_start"])
        overlap_end = min(end, segment["content_end"])
        if overlap_start >= overlap_end:
            continue
        source_start = segment["start"] + (overlap_start - segment["content_start"])
        source_end = segment["start"] + (overlap_end - segment["content_start"])
        if merged and merged[-1]["end"] == source_start:
            merged[-1]["end"] = source_end
        else:
            merged.append({"start": source_start, "end": source_end})
    return [{"start": r["start"], "end": r["end"], "hash": _span_hash(text[r["start"]:r["end"]])} for r in merged]


def _split_markdown_record(text: str, record: dict, *, chunk_size: int, chunk_overlap: int) -> list[dict]:
    content = record["content"]
    if len(content) <= chunk_size:
        content_spans = [{"text": content, "start": 0, "end": len(content)}]
    else:
        recursive = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=_SEPARATORS,
            keep_separator=True,
            add_start_index=True,
        )
        content_spans = [
            {"text": doc.page_content, "start": int(doc.metadata["start_index"]), "end": int(doc.metadata["start_index"]) + len(doc.page_content)}
            for doc in recursive.create_documents([content])
            if doc.page_content.strip()
        ]
    out: list[dict] = []
    for span in content_spans:
        ranges = _source_ranges_for_content_span(text, record["segments"], span["start"], span["end"])
        if not ranges:
            continue
        out.append({
            "text": span["text"],
            "start": min(r["start"] for r in ranges),
            "end": max(r["end"] for r in ranges),
            "source_ranges": ranges,
        })
    return out


def _split_markdown_spans(text: str, *, chunk_size: int, chunk_overlap: int) -> list[dict]:
    out: list[dict] = []
    for record in _aggregate_markdown_records(_markdown_line_records(text)):
        out.extend(_split_markdown_record(text, record, chunk_size=chunk_size, chunk_overlap=chunk_overlap))
    return out


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

    chunks: list[dict] = []
    for s in spans:
        if not s["text"].strip():
            continue
        ranges = s.get("source_ranges") or [{
            "start": s["start"],
            "end": s["end"],
            "hash": _span_hash(text[s["start"]:s["end"]]),
        }]
        item = {
            "text": s["text"],
            "source_start": s["start"],
            "source_end": s["end"],
            "location": _structured_location(text, s["start"], s["end"], base_location),
            "source_span_hash": _span_hash(text[s["start"]:s["end"]]),
            "source_ranges": ranges,
        }
        chunks.append(item)
    return chunks


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
            "source_ranges": [{
                "start": 0,
                "end": len(flat),
                "hash": _span_hash(flat),
            }],
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
                "source_ranges": [
                    {
                        "start": 0,
                        "end": preamble_len,
                        "hash": _span_hash(flat[:preamble_len]),
                    },
                    {
                        "start": src_start,
                        "end": src_end,
                        "hash": _span_hash(flat[src_start:src_end]),
                    },
                ],
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
            "source_ranges": [
                {
                    "start": 0,
                    "end": preamble_len,
                    "hash": _span_hash(flat[:preamble_len]),
                },
                {
                    "start": src_start,
                    "end": src_end,
                    "hash": _span_hash(flat[src_start:src_end]),
                },
            ],
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
    output_segments: list[dict] = []
    for idx, sr in enumerate(seg_records):
        gbase = offset_map[idx]["global_base"]
        segment = sr["seg"]
        parser = segment.get("parser") or parser_provenance(
            "structured-splitter",
            "v1",
            {"chunk_size": chunk_size, "chunk_overlap": chunk_overlap},
        )
        unit_key = segment.get("unit_key") or f"{segment.get('source_kind', 'text')}:segment:{idx}"
        segment_unit = build_parser_unit(
            source_kind=segment.get("source_kind", "text"),
            unit_kind="table" if segment.get("kind") == "table" else "section",
            ordinal=idx,
            unit_key=unit_key,
            parser=parser,
            location=_segment_location(segment),
            source_text=normalized_text,
            source_start=gbase,
            source_end=gbase + len(sr["flat"]),
            source_ranges=[text_range(normalized_text, gbase, gbase + len(sr["flat"]))],
            parent_key=segment.get("parent_key"),
            section_path=segment.get("section_path"),
            quality=segment.get("quality"),
        )
        output_segment = dict(segment)
        output_segment["unit_key"] = unit_key
        output_segment["parser_unit"] = segment_unit
        output_segments.append(output_segment)
        for rec in sr["chunks"]:
            loc = rec.get("location") or _segment_location(sr["seg"])
            src_start = gbase + rec["source_start"]
            src_end = gbase + rec["source_end"]
            output_chunk = {
                "text": rec["text"],
                "source_start": src_start,
                "source_end": src_end,
                "location": loc,
                "source_span_hash": _span_hash(normalized_text[src_start:src_end]),
            }
            if rec.get("source_ranges"):
                output_chunk["source_ranges"] = [
                    {
                        **item,
                        "start": gbase + item["start"],
                        "end": gbase + item["end"],
                    }
                    for item in rec.get("source_ranges") or []
                ]
            chunks.append(output_chunk)

    return {"normalized_text": normalized_text, "chunks": chunks, "segments": output_segments}
