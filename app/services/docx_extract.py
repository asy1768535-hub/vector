r"""docx 结构化抽取：按文档顺序产出「段落 / 表格」segment，并维护章节路径。

为什么结构化：表格里常有关键信息（技术选型、人员签认…），若拍平进大段落 chunk 会被
稀释、检索排不上（见评测 #8）。这里把内容拆成 segment，交给 splitter.chunk_segments
做表格感知切分——每个表格单独成块并带上「章节 / 表名 / 表头」上下文。

segment 结构（普通 dict）：
  - {"kind":"prose","heading":<章节路径>,"text":...}     段落 + 图片 OCR 文本
  - {"kind":"table","heading":...,"caption":...,"header":<首行>,"rows":[<"a | b | c">...]}

heading 检测双信号：段落样式名含「标题」/「Heading」 或 文本匹配 `^\d+(\.\d+)*\s+`。
"""
from __future__ import annotations

import io
import re
from typing import Any
from pathlib import Path
from typing import Callable, Optional

from docx import Document
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.services.parser_units import (
    build_ocr_parser_units,
    build_parser_unit,
    excel_column_name,
    ocr_result_text_and_blocks,
    parser_provenance,
)

_HEADING_NUM = re.compile(r"^\d+(\.\d+)*\s+\S")
_LEVEL_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}


def _iter_block_items(parent):
    """按文档顺序产出 Paragraph / Table（python-docx 默认把两者分到不同集合）。"""
    body = parent.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def _table_row_cells(table: Table) -> list[tuple[int, list[tuple[int, str]], str]]:
    records: list[tuple[int, list[tuple[int, str]], str]] = []
    for row_number, row in enumerate(table.rows, start=1):
        dedup: list[tuple[int, str]] = []
        for column, cell in enumerate(row.cells, start=1):
            value = cell.text.strip()
            if not dedup or dedup[-1][1] != value:
                dedup.append((column, value))
        line = " | ".join(value for _column, value in dedup).strip(" |")
        if line:
            records.append((row_number, dedup, line))
    return records


def _table_rows(table: Table) -> list[str]:
    """表格转行列表：行内 ' | ' 拼接，去相邻重复单元格（合并单元格），跳过空行。"""
    return [line for _row_number, _cells, line in _table_row_cells(table)]


def _table_parser_units(
    table: Table,
    *,
    table_key: str,
    table_index: int,
    parser: dict[str, str],
) -> list[dict]:
    table_source = {"table": {"index": max(0, table_index - 1)}}
    units: list[dict] = []
    for row_number, cells, line in _table_row_cells(table):
        row_key = f"{table_key}:row:{row_number}"
        row_source = {
            **table_source,
            "row": {"start": row_number, "end": row_number},
        }
        row_unit = build_parser_unit(
            source_kind="docx",
            unit_kind="row",
            ordinal=len(units),
            unit_key=row_key,
            parser=parser,
            source=row_source,
            parent_key=table_key,
        )
        row_unit["text"] = line
        units.append(row_unit)
        for column, value in cells:
            cell_name = f"{excel_column_name(column)}{row_number}"
            cell_key = f"{row_key}:cell:{cell_name}"
            cell_unit = build_parser_unit(
                source_kind="docx",
                unit_kind="cell",
                ordinal=len(units),
                unit_key=cell_key,
                parser=parser,
                source={
                    **row_source,
                    "column": {"start": column, "end": column},
                    "cell": {"start": cell_name, "end": cell_name},
                },
                parent_key=row_key,
            )
            cell_unit["value"] = value
            cell_unit["text"] = value
            units.append(cell_unit)
    return units


def _table_to_text(table: Table) -> str:
    return "\n".join(_table_rows(table))


def _paragraph_image_blobs(paragraph: Paragraph) -> list[bytes]:
    """取段落里内嵌图片的字节流（按出现顺序）。无图返回空列表。"""
    blobs: list[bytes] = []
    for blip in paragraph._p.findall(".//" + qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if not rid:
            continue
        try:
            blobs.append(paragraph.part.related_parts[rid].blob)
        except KeyError:
            continue
    return blobs


def _heading_info(paragraph: Paragraph) -> Optional[tuple[str, int]]:
    """是标题则返回 (标题文本, 层级)，否则 None。"""
    txt = paragraph.text.strip()
    if not txt:
        return None
    style = (paragraph.style.name if paragraph.style else "") or ""
    is_style = ("标题" in style) or ("heading" in style.lower())
    m = _HEADING_NUM.match(txt)
    if not (is_style or m):
        return None
    if m:
        level = txt.split()[0].count(".") + 1      # "2.2 …" → 2 段 → level 2
    elif "标题" in style:
        level = next((v for k, v in _LEVEL_CN.items() if k in style), 1)
    else:
        level = 1
    return txt, level


def extract_docx_segments(
    data: bytes | Path, ocr: Optional[Callable[[bytes], Any]] = None
) -> list[dict]:
    """按文档顺序抽成 prose/table segment 列表，并维护章节路径。"""
    doc = Document(data if isinstance(data, Path) else io.BytesIO(data))
    segs: list[dict] = []
    heading_stack: list[str] = []          # heading_stack[i] = 第 i+1 级标题
    prose_buf: list[str] = []
    prose_ocr_blocks: list[dict[str, Any]] = []
    prose_has_native_text = False
    prose_has_ocr_text = False
    prose_start: int | None = None
    prose_end: int | None = None
    paragraph_index = 0
    table_index = 0
    parser = parser_provenance("python-docx", "v1", {"ocr_enabled": ocr is not None})

    def cur_heading() -> str:
        return " / ".join(h for h in heading_stack if h)

    def flush_prose() -> None:
        nonlocal prose_buf, prose_start, prose_end, prose_ocr_blocks
        nonlocal prose_has_native_text, prose_has_ocr_text
        if prose_buf:
            text = "\n".join(prose_buf).strip()
            if text:
                location = {
                    "type": "paragraph",
                    "start_paragraph": prose_start,
                    "end_paragraph": prose_end,
                    "heading": cur_heading(),
                }
                if prose_has_native_text and prose_has_ocr_text:
                    extraction_mode = "mixed"
                elif prose_has_ocr_text:
                    extraction_mode = "ocr"
                elif prose_has_native_text:
                    extraction_mode = "native"
                else:
                    extraction_mode = "not_applicable"
                quality = {
                    "extraction_mode": extraction_mode,
                    "native_text_present": prose_has_native_text,
                    "ocr_blocks": list(prose_ocr_blocks),
                }
                segment = {
                    "kind": "prose",
                    "heading": cur_heading(),
                    "text": text,
                    "source_kind": "docx",
                    "ordinal": len(segs),
                    "unit_key": f"docx:segment:{len(segs)}",
                    "parser": parser,
                    "location": location,
                    "quality": quality,
                }
                segment["parser_unit"] = build_parser_unit(
                    source_kind="docx",
                    unit_kind="section",
                    ordinal=segment["ordinal"],
                    unit_key=segment["unit_key"],
                    parser=parser,
                    location=location,
                    section_path=[part for part in cur_heading().split(" / ") if part],
                    quality={
                        "extraction_mode": extraction_mode,
                        "native_text_present": prose_has_native_text,
                    },
                )
                if prose_ocr_blocks:
                    segment["structured_units"] = build_ocr_parser_units(
                        prose_ocr_blocks,
                        source_kind="docx",
                        parser=parser,
                        parent_key=segment["unit_key"],
                        extraction_mode=extraction_mode if extraction_mode in {"ocr", "mixed"} else "ocr",
                        unit_text=text,
                    )
                segs.append(segment)
        prose_buf = []
        prose_ocr_blocks = []
        prose_start = None
        prose_end = None
        prose_has_native_text = False
        prose_has_ocr_text = False

    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            info = _heading_info(block)
            if info:
                flush_prose()
                title, level = info
                del heading_stack[level - 1:]              # 截断到上一级
                while len(heading_stack) < level - 1:      # 跳级时补空位
                    heading_stack.append("")
                heading_stack.append(title)
                continue
            t = block.text.strip()
            if t:
                paragraph_index += 1
                if prose_start is None:
                    prose_start = paragraph_index
                prose_end = paragraph_index
                prose_buf.append(t)
                prose_has_native_text = True
            if ocr is not None:
                for blob in _paragraph_image_blobs(block):
                    try:
                        ot, blocks = ocr_result_text_and_blocks(ocr(blob))
                    except Exception:  # noqa: BLE001
                        ot, blocks = "", []
                    if ot and ot.strip():
                        prose_buf.append(ot.strip())
                        prose_has_ocr_text = True
                        prose_ocr_blocks.extend(blocks)
        else:  # Table
            caption = prose_buf[-1] if prose_buf else cur_heading()
            flush_prose()
            rows = _table_rows(block)
            if rows:
                table_index += 1
                location = {"type": "table", "table_index": table_index, "heading": cur_heading()}
                segment = {
                    "kind": "table", "heading": cur_heading(),
                    "caption": caption, "header": rows[0], "rows": rows,
                    "source_kind": "docx",
                    "ordinal": len(segs),
                    "unit_key": f"docx:segment:{len(segs)}",
                    "parser": parser,
                    "location": location,
                }
                segment["parser_unit"] = build_parser_unit(
                    source_kind="docx",
                    unit_kind="table",
                    ordinal=segment["ordinal"],
                    unit_key=segment["unit_key"],
                    parser=parser,
                    location=location,
                    section_path=[part for part in cur_heading().split(" / ") if part],
                )
                segment["structured_units"] = _table_parser_units(
                    block,
                    table_key=segment["unit_key"],
                    table_index=table_index,
                    parser=parser,
                )
                segs.append(segment)
    flush_prose()
    return segs


def extract_docx_text(
    data: bytes | Path, ocr: Optional[Callable[[bytes], Any]] = None
) -> str:
    """从 docx 抽取扁平正文（段落 + 表格 + 可选图片 OCR，按文档顺序）。

    供 content_hash / 不需要结构的场景用；表格感知切分请走 extract_docx_segments。
    """
    parts: list[str] = []
    for s in extract_docx_segments(data, ocr):
        parts.append(s["text"] if s["kind"] == "prose" else "\n".join(s["rows"]))
    return "\n".join(parts)
