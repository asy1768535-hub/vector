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
from typing import Callable, Optional

from docx import Document
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

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


def _table_rows(table: Table) -> list[str]:
    """表格转行列表：行内 ' | ' 拼接，去相邻重复单元格（合并单元格），跳过空行。"""
    out: list[str] = []
    for row in table.rows:
        dedup: list[str] = []
        for cell in row.cells:
            t = cell.text.strip()
            if not dedup or dedup[-1] != t:
                dedup.append(t)
        line = " | ".join(dedup).strip(" |")
        if line:
            out.append(line)
    return out


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
    data: bytes, ocr: Optional[Callable[[bytes], str]] = None
) -> list[dict]:
    """按文档顺序抽成 prose/table segment 列表，并维护章节路径。"""
    doc = Document(io.BytesIO(data))
    segs: list[dict] = []
    heading_stack: list[str] = []          # heading_stack[i] = 第 i+1 级标题
    prose_buf: list[str] = []
    prose_start: int | None = None
    prose_end: int | None = None
    paragraph_index = 0
    table_index = 0

    def cur_heading() -> str:
        return " / ".join(h for h in heading_stack if h)

    def flush_prose() -> None:
        nonlocal prose_buf, prose_start, prose_end
        if prose_buf:
            text = "\n".join(prose_buf).strip()
            if text:
                segs.append({
                    "kind": "prose",
                    "heading": cur_heading(),
                    "text": text,
                    "location": {
                        "type": "paragraph",
                        "start_paragraph": prose_start,
                        "end_paragraph": prose_end,
                        "heading": cur_heading(),
                    },
                })
        prose_buf = []
        prose_start = None
        prose_end = None

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
            if ocr is not None:
                for blob in _paragraph_image_blobs(block):
                    try:
                        ot = ocr(blob)
                    except Exception:  # noqa: BLE001
                        ot = ""
                    if ot and ot.strip():
                        prose_buf.append(ot.strip())
        else:  # Table
            caption = prose_buf[-1] if prose_buf else cur_heading()
            flush_prose()
            rows = _table_rows(block)
            if rows:
                table_index += 1
                segs.append({
                    "kind": "table", "heading": cur_heading(),
                    "caption": caption, "header": rows[0], "rows": rows,
                    "location": {"type": "table", "table_index": table_index, "heading": cur_heading()},
                })
    flush_prose()
    return segs


def extract_docx_text(data: bytes, ocr: Optional[Callable[[bytes], str]] = None) -> str:
    """从 docx 抽取扁平正文（段落 + 表格 + 可选图片 OCR，按文档顺序）。

    供 content_hash / 不需要结构的场景用；表格感知切分请走 extract_docx_segments。
    """
    parts: list[str] = []
    for s in extract_docx_segments(data, ocr):
        parts.append(s["text"] if s["kind"] == "prose" else "\n".join(s["rows"]))
    return "\n".join(parts)
