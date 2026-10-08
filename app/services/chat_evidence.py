"""Bounded Chat file evidence; library/folder authorization belongs to the caller."""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from types import SimpleNamespace

from sqlalchemy import and_, or_, select

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.schemas.dify import DifyRecord, DifyRetrievalRequest, MetadataConditionGroup, MetadataConditionItem, RetrievalSetting
from app.services.chat_answer import build_context
from app.services.evidence_locator_projection import chunk_locator_projection, parse_locator
from app.services.splitter import _segment_flat_text
from app.services.xlsx_extract import _row_to_numbered_line

MAX_FILES = 3
MAX_GROUP_CHUNKS = 8
MAX_SOURCES = 20
MAX_CONTEXT_CHARS = 12_000
MAX_CATALOG_DOCUMENTS = 1000
MAX_CALCULATION_CELLS = 2048
MAX_CALCULATION_COLUMNS = 32
MAX_CALCULATION_CHARS = 2048
_EXTENSION = re.compile(r"\.(?:pdf|docx?|xlsx?|csv|txt|md)$", re.I)
_QUOTED = re.compile(r"《([^》\n]{2,200})》|[“\"]([^”\"\n]{2,200})[”\"]")
_FILE_TOKEN = re.compile(r"[^\s《》\"“”，,、;；：:！？?]+?\.(?:pdf|docx?|xlsx?|csv|txt|md)(?![a-zA-Z0-9])", re.I)


def _number_and_unit(value, declared_unit):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if len(text) > 80:
        return None
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?)\s*([^\d\s].*)?", text)
    if not match:
        return None
    try:
        number = Decimal(match[1])
    except InvalidOperation:
        return None
    if not number.is_finite() or abs(number.adjusted()) > 64:
        return None
    unit = (match[2] or "").strip()
    expected = unicodedata.normalize("NFKC", declared_unit or "").strip()
    if unit and expected and unit != expected:
        return None
    # Bare numeric cells inherit only the explicitly declared column unit.
    return number, unit or expected


def _header_unit(header):
    match = re.search(r"[（(](?:单位[:：]\s*)?([^()（）]{1,16})[）)]$", header)
    if not match:
        return None
    value = match[1].strip()
    explicit = bool(re.search(r"[（(]单位[:：]", header))
    common = {"元", "万元", "亿元", "件", "个", "人", "台", "套", "栋", "亩", "平方米", "立方米", "吨", "克", "千克", "米", "厘米", "毫米", "天", "小时", "年"}
    return value if explicit or value in common or re.fullmatch(r"[a-zA-Zμ%][a-zA-Zμ0-9²³/%.-]*", value) else None


def calculate_table_snapshot(parent, locator, blocks):
    """Calculate saved cells only after identity and exact rendered-table reconciliation."""
    unsafe = (None, ["表格单元格快照的身份、字段或范围无法完整核验，未计算完整合计。"])
    if not locator or len(blocks) > MAX_CALCULATION_CELLS or not blocks or not parent.text:
        return unsafe
    if (str(locator.document_id) != str(parent.document_id) or str(locator.document_revision_id) != str(parent.document_revision_id)
            or str(locator.unit_id) != str(parent.id) or locator.unit_kind != "table"
            or locator.unit_text_sha256 != sha256(parent.text.encode()).hexdigest()):
        return unsafe
    kind = locator.source.kind
    if kind not in {"xlsx", "xls", "docx"}:
        return unsafe
    row_blocks = {block.id: block for block in blocks if block.block_kind == "row"}
    grid = {}
    payloads = {}
    for block in blocks:
        if (block.library_id != parent.library_id or block.document_id != parent.document_id
                or block.document_revision_id != parent.document_revision_id):
            return unsafe
        own_locator = parse_locator((block.content or {}).get("evidence_locator_v1"), document_id=parent.document_id,
            document_revision_id=parent.document_revision_id, revision_no=locator.revision_no, unit_id=block.id,
            parent_unit_id=block.parent_block_id)
        if (not own_locator or own_locator.unit_kind != block.block_kind or own_locator.source.kind != kind
                or own_locator.unit_text_sha256 != sha256((block.text or "").encode()).hexdigest()):
            return unsafe
        if block.block_kind == "row":
            if block.parent_block_id != parent.id:
                return unsafe
            continue
        if block.block_kind != "cell":
            return unsafe
        ancestor = row_blocks.get(block.parent_block_id)
        if block.parent_block_id != parent.id and (ancestor is None or ancestor.parent_block_id != parent.id):
            return unsafe
        source = own_locator.source.model_dump(exclude_none=True)
        row, col = source.get("row", {}), source.get("column", {})
        if row.get("start") != row.get("end") or col.get("start") != col.get("end"):
            return unsafe
        r, c = row.get("start"), col.get("start")
        if not isinstance(r, int) or not isinstance(c, int) or c > MAX_CALCULATION_COLUMNS:
            return unsafe
        parent_source = locator.source.model_dump(exclude_none=True)
        if "row" in parent_source and not parent_source["row"]["start"] <= r <= parent_source["row"]["end"]:
            return unsafe
        if kind == "docx" and source.get("table") != parent_source.get("table"):
            return unsafe
        if ancestor:
            ancestor_locator = parse_locator((ancestor.content or {}).get("evidence_locator_v1"),
                document_id=parent.document_id, document_revision_id=parent.document_revision_id,
                revision_no=locator.revision_no, unit_id=ancestor.id, parent_unit_id=parent.id)
            if not ancestor_locator or ancestor_locator.source.model_dump(exclude_none=True).get("row") != row:
                return unsafe
        if kind in {"xlsx", "xls"} and source.get("sheet") != locator.source.model_dump().get("sheet"):
            return unsafe
        payload = (block.content or {}).get("parser_unit") or {}
        value = payload.get("value")
        if isinstance(value, (dict, list)) or payload.get("payload_truncated"):
            return unsafe
        # The stored scalar must agree with the locator's hashed quote.
        if value is not None and str(value) != (block.text or ""):
            return unsafe
        if (r, c) in payloads:
            return unsafe
        grid.setdefault(r, {})[c] = block.text if value is None and payload.get("formula") else value
        payloads[r, c] = payload
    if len(grid) < 2:
        return unsafe
    width = max(c for values in grid.values() for c in values)
    rendered = []
    for r in sorted(grid):
        values = tuple(grid[r].get(c) for c in range(1, width + 1))
        if kind in {"xlsx", "xls"}:
            line = _row_to_numbered_line(values)
        else:
            parts = list(dict.fromkeys(str(value or "").strip() for value in values)) if not row_blocks else [str(value or "").strip() for value in values]
            line = " | ".join(parts).strip(" |")
        if line:
            rendered.append(line)
    context = (parent.content or {}).get("parser_unit") or {}
    reconstructed = _segment_flat_text({"kind": "table", "heading": context.get("segment_heading"),
                                       "caption": context.get("segment_caption"), "rows": rendered})
    if reconstructed != parent.text:
        return unsafe
    first_row = min(grid)
    headers = grid[first_row]
    if any(not isinstance(value, str) or not value.strip() or _number_and_unit(value, None) is not None for value in headers.values()):
        return unsafe
    if len({_name(value) for value in headers.values()}) != len(headers) or any(c not in headers for values in grid.values() for c in values):
        return unsafe
    summaries = {"合计", "总计", "小计", "total", "subtotal"}
    label_column = min(headers)
    excluded = [r for r, values in grid.items() if r != first_row and _name(str(values.get(label_column))) in summaries]
    data_rows = [r for r in sorted(grid) if r != first_row and r not in excluded]
    if not data_rows:
        return unsafe
    columns, notices = [], []
    for c, header in sorted(headers.items()):
        unit = _header_unit(header.strip())
        values = [grid[r].get(c) for r in data_rows]
        numbers = [_number_and_unit(value, unit) for value in values]
        numeric = bool(unit) or any(number is not None for number in numbers) or any(isinstance(value, (int, float)) for value in values)
        total = None
        if numeric:
            units = {number[1] for number in numbers if number is not None}
            if any(number is None for number in numbers) or len(units) != 1 or any(payloads.get((r, c), {}).get("formula") and grid[r].get(c) is None for r in data_rows):
                notices.append(f"字段“{header}”存在缺值、不可解析数值、未求值公式或单位不一致，未计算该字段的完整合计。")
            else:
                unit = unit or next(iter(units)) or None
                with localcontext() as decimal_context:
                    decimal_context.prec = 100
                    total = format(sum((number[0] for number in numbers), Decimal(0)), "f")
        nonempty = [str(value) for value in values if value is not None and str(value).strip()]
        columns.append({"field": header.strip(), "unit": unit, "sum": total, "distinct_count": len(set(nonempty)),
                        "nonempty_count": len(nonempty)})
    return {"block_id": str(parent.id), "row_count": len(data_rows), "data_rows": data_rows,
            "excluded_summary_rows": excluded, "columns": columns}, notices


@dataclass
class FileSelection:
    document_ids: list = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    explicit: bool = False


def _name(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def _aliases(document) -> list[str]:
    return list(dict.fromkeys(value for value in (document.title, document.display_name) if value))


def resolve_file_mentions(query: str, documents) -> FileSelection:
    """Exact names precede unique quoted abbreviations; no business aliases."""
    quoted = [next(value for value in match.groups() if value) for match in _QUOTED.finditer(query)]
    # A quoted phrase is a filename only if the catalog can match it or it has a
    # file suffix. Ordinary quoted facts remain ordinary topic queries.
    requests = [value for value in quoted if _EXTENSION.search(value) or any(
        _name(value) in _name(alias) or
        (len(words := re.findall(r"[\w]+", value.casefold())) > 1 and
         all(len(word) >= 2 and _name(word) in _name(alias) for word in words))
        for document in documents for alias in _aliases(document)
    )]
    for match in _FILE_TOKEN.finditer(query):
        value = match.group()
        prefix = _name(query[:match.end()])
        ending = [alias for document in documents for alias in _aliases(document) if prefix.endswith(_name(alias))]
        requests.append(max(ending, key=lambda alias: len(_name(alias))) if ending else value)
    requests = list(dict.fromkeys(requests))
    if not requests:
        normalized_query = _name(query)
        requests = list(dict.fromkeys(alias for document in documents for alias in _aliases(document)
                                     if len(_name(_EXTENSION.sub("", alias))) >= 4
                                     and _name(alias) in normalized_query))
        # Extension-less full names also count, but a supplied extension must
        # not silently select another format with the same basename.
        if not requests and not re.search(r"\.(?:pdf|docx?|xlsx?|csv|txt|md)\b", query, re.I):
            requests = list(dict.fromkeys(_EXTENSION.sub("", alias) for document in documents for alias in _aliases(document)
                                         if len(_name(_EXTENSION.sub("", alias))) >= 4
                                         and _name(_EXTENSION.sub("", alias)) in normalized_query))
    selection = FileSelection(explicit=bool(requests))
    for request in requests:
        key = _name(request)
        exact = [document for document in documents if any(key == _name(alias) for alias in _aliases(document))]
        has_extension = bool(_EXTENSION.search(request))
        words = re.findall(r"[\w]+", _EXTENSION.sub("", request).casefold())
        matches = exact or [document for document in documents if any(
            (key in _name(_EXTENSION.sub("", alias)) or
             (len(words) > 1 and all(len(word) >= 2 and _name(word) in _name(alias) for word in words)))
            and (not has_extension or _EXTENSION.search(alias) and
                 _EXTENSION.search(alias).group().casefold() == _EXTENSION.search(request).group().casefold())
            for alias in _aliases(document)
        )]
        if len(matches) == 1:
            if matches[0].id not in selection.document_ids:
                selection.document_ids.append(matches[0].id)
        elif matches:
            selection.notices.append(f"文件名称“{request}”存在歧义，请提供完整文件名或明确文件夹。")
        else:
            selection.notices.append(f"在当前授权范围内未能唯一定位文件“{request}”；这不表示文件不存在。")
    if len(selection.document_ids) > MAX_FILES:
        selection.notices.append("本次最多取证三份指定文件，请缩小文件范围。")
    return selection


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _current_rows(library_id):
    return (
        select(Chunk, Document, DocumentRevision)
        .join(Document, Document.id == Chunk.document_id)
        .outerjoin(DocumentRevision, DocumentRevision.id == Document.current_revision_id)
        .where(
            Chunk.library_id == library_id, Document.library_id == library_id,
            Document.deleted_at.is_(None),
            or_(
                and_(Document.current_revision_id.is_(None), Chunk.document_revision_id.is_(None), Document.status == "ready"),
                and_(Chunk.document_revision_id == Document.current_revision_id, DocumentRevision.status == "ready",
                     DocumentRevision.document_id == Document.id, DocumentRevision.library_id == library_id),
            ),
        )
        .execution_options(populate_existing=True)
    )


def _record_from_row(chunk, document, revision, seed=None) -> DifyRecord:
    metadata = {key: value for key, value in (seed.metadata if seed else {}).items()
                if key in {"vector_score", "rerank_score", "matched_queries", "rewrite_source"}}
    metadata.update({"document_id": str(document.id), "chunk_id": str(chunk.id), "seq": chunk.seq,
                     "document_revision": document.current_revision,
                     "document_revision_id": str(revision.id) if revision else None,
                     "document_revision_no": revision.revision_no if revision else None,
                     "chat_evidence_origin": "retrieval" if seed else "table_expansion"})
    if revision:
        raw = (chunk.chunk_metadata or {}).get("evidence_locator_v1")
        if isinstance(raw, dict) and raw.get("unit_text_sha256") == sha256(chunk.text.encode()).hexdigest():
            projection = chunk_locator_projection(chunk, document_id=document.id,
                                                 document_revision_id=revision.id, revision_no=revision.revision_no)
            if projection:
                metadata["evidence_locator_v1_projection"] = projection
    return DifyRecord(content=chunk.text, title=(revision.title if revision else None) or document.display_name or document.title or "",
                      score=seed.score if seed else 0.0, metadata=metadata)


def pack_required_evidence(groups, optional, max_context_chars: int) -> tuple[list[DifyRecord], list[str]]:
    """Reserve entire required groups before any optional ranked records."""
    budget = min(MAX_CONTEXT_CHARS, max(0, max_context_chars))
    records: list[DifyRecord] = []
    seen: set[str] = set()
    notices: list[str] = []
    for group in groups:
        additions = [record for record in group if record.metadata["chunk_id"] not in seen]
        candidate = [*records, *additions]
        _context, used = build_context(candidate, budget)
        full = len(used) == len(candidate) and all(visible.content == record.content.strip() for visible, record in zip(used, candidate))
        if len(candidate) > MAX_SOURCES or not full:
            notices.append("必要文件或表格组超过本轮证据预算，不能据此作完整对照、合计或不存在的判断。")
            continue
        records = candidate
        seen.update(record.metadata["chunk_id"] for record in additions)
    for record in optional:
        key = record.metadata["chunk_id"]
        if key in seen:
            continue
        candidate = [*records, record]
        _context, used = build_context(candidate, budget)
        if len(candidate) <= MAX_SOURCES and len(used) == len(candidate) and used[-1].content == record.content.strip():
            records = candidate
            seen.add(key)
    return records, list(dict.fromkeys(notices))


def _calculation_text(calculations, indexes):
    parts = []
    for calculation in calculations:
        result = calculation["result"]
        citations = "".join(f"[{indexes[chunk_id]}]" for chunk_id in calculation["chunk_ids"])
        fields = []
        for column in result["columns"]:
            detail = f"字段“{column['field']}”：非空值数={column['nonempty_count']}，按保存原值去重数={column['distinct_count']}"
            if column["sum"] is not None:
                detail += f"，合计={column['sum']}（原表单位：{column['unit'] or '未注明，仅加总原数值'}，未作单位转换）"
            fields.append(detail)
        excluded = f"；排除原表汇总行：{','.join(map(str, result['excluded_summary_rows']))}" if result["excluded_summary_rows"] else ""
        parts.append(f"《{calculation['title']}》{calculation['location']}{citations}：数据行数={result['row_count']}（不等于业务对象数）{excluded}；" + "；".join(fields))
    return ("本轮完整结构组的确定性计算（不是全库穷举；只适用于列出的文件、字段及范围）：\n" + "\n".join(parts)) if parts else ""


def pack_calculated_evidence(groups, optional, calculations, max_context_chars):
    """Reserve full evidence first, bounded calculation notes second, optional text last."""
    budget = min(MAX_CONTEXT_CHARS, max(0, max_context_chars))
    required, gaps = pack_required_evidence(groups, [], budget)
    indexes = {record.metadata["chunk_id"]: index for index, record in enumerate(required, 1)}
    visible = [calculation for calculation in calculations if all(chunk_id in indexes for chunk_id in calculation["chunk_ids"])]
    context, _used = build_context(required, budget)
    calculation_context = _calculation_text(visible, indexes)
    if len(calculation_context) > min(MAX_CALCULATION_CHARS, max(0, budget - len(context))):
        calculation_context = ""
        gaps.append("必要表格证据已保留，但计算说明超出本轮字符预算，未输出完整合计计算结果。")
    remaining = budget - len(calculation_context)
    packed, more_gaps = pack_required_evidence([required] if required else [], optional, remaining)
    return packed, list(dict.fromkeys([*gaps, *more_gaps])), {"calculation_context": calculation_context, "context_chars": remaining}


def _group_covers_parent(parent_locator, records) -> bool:
    source = parent_locator.source.model_dump(exclude_none=True)
    sources = [record.metadata["evidence_locator_v1_projection"]["source"] for record in records]
    if "row" in source:
        key, inclusive = "row", True
        if any(member.get("sheet") != source.get("sheet") for member in sources):
            return False
    elif "text" in source:
        key, inclusive = "text", False
    else:
        return False
    expected = source[key]
    if "start" not in expected or "end" not in expected or any(key not in member for member in sources):
        return False
    intervals = sorted((member[key]["start"], member[key]["end"]) for member in sources
                       if "start" in member[key] and "end" in member[key])
    if len(intervals) != len(sources):
        return False
    cursor = expected["start"]
    for start, end in intervals:
        if start > cursor or start < expected["start"] or end > expected["end"]:
            return False
        cursor = max(cursor, end + int(inclusive))
    return cursor >= expected["end"] + int(inclusive)


async def _load_file_catalog(db, library, allowed_document_ids):
    catalog_stmt = (
        select(Document, DocumentRevision)
        .outerjoin(DocumentRevision, DocumentRevision.id == Document.current_revision_id)
        .where(Document.library_id == library.id, Document.deleted_at.is_(None), or_(
            and_(Document.current_revision_id.is_(None), Document.status == "ready"),
            and_(DocumentRevision.status == "ready", DocumentRevision.document_id == Document.id,
                 DocumentRevision.library_id == library.id),
        ))
    )
    if allowed_document_ids is not None:
        catalog_stmt = catalog_stmt.where(Document.id.in_(allowed_document_ids))
    catalog_rows = (await db.execute(catalog_stmt.order_by(Document.id).limit(MAX_CATALOG_DOCUMENTS + 1)
                                    .execution_options(populate_existing=True))).all()
    return [SimpleNamespace(id=document.id, title=(revision.title if revision else None) or document.title,
                            display_name=document.display_name) for document, revision in catalog_rows]


async def _load_table_cells(db, parent):
    statement = select(DocumentBlock).where(DocumentBlock.library_id == parent.library_id,
        DocumentBlock.document_id == parent.document_id, DocumentBlock.document_revision_id == parent.document_revision_id,
        DocumentBlock.block_kind.in_(["row", "cell"])).execution_options(populate_existing=True)
    direct = list((await db.execute(statement.where(DocumentBlock.parent_block_id == parent.id)
                  .order_by(DocumentBlock.seq).limit(MAX_CALCULATION_CELLS + 1))).scalars().all())
    row_ids = [block.id for block in direct if block.block_kind == "row"]
    if row_ids and len(direct) <= MAX_CALCULATION_CELLS:
        children = list((await db.execute(statement.where(DocumentBlock.parent_block_id.in_(row_ids),
                        DocumentBlock.block_kind == "cell").order_by(DocumentBlock.seq)
                        .limit(MAX_CALCULATION_CELLS - len(direct) + 1))).scalars().all())
        direct.extend(children)
    return direct


async def collect_chat_evidence(db, library, query: str, *, top_k: int, max_context_chars: int,
                                retrieve, allowed_document_ids: list[str] | None = None):
    """Return records and bounded scope notes without adding model/retry calls."""
    condition = None
    if allowed_document_ids is not None:
        condition = MetadataConditionGroup(conditions=[MetadataConditionItem(name=["document_id"], comparison_operator="in", value=allowed_document_ids)])

    async def request_for(document_id=None):
        scoped = condition if document_id is None else MetadataConditionGroup(conditions=[MetadataConditionItem(
            name=["document_id"], comparison_operator="in", value=[str(document_id)],
        )])
        return await retrieve(DifyRetrievalRequest(knowledge_id=library.slug, query=query,
                              retrieval_setting=RetrievalSetting(top_k=top_k), metadata_condition=scoped))

    if library.lifecycle_mode == "external":
        if _FILE_TOKEN.search(query):
            return [], {"chat_evidence": {"notices": ["该外部知识库未提供可核验的文件目录和版本绑定，当前无法可靠限定指定文件取证。"],
                                         "clarification_required": True}}
        result = await request_for()
        return result.records, result.retrieval_debug
    catalog = await _load_file_catalog(db, library, allowed_document_ids)
    selection = resolve_file_mentions(query, catalog[:MAX_CATALOG_DOCUMENTS])
    if selection.explicit and len(catalog) > MAX_CATALOG_DOCUMENTS:
        selection.notices.append("可见文件目录超过本轮定位上限，请选择更小的文件夹范围后取证。")
    if selection.notices:
        return [], {"chat_evidence": {"notices": selection.notices, "clarification_required": True}}
    retrieved: list[DifyRecord] = []
    notices: list[str] = []
    retrieval_debug = None
    if selection.explicit:
        for document_id in selection.document_ids:
            result = await request_for(document_id)
            own = [record for record in result.records if record.metadata.get("document_id") == str(document_id)]
            if not own:
                notices.append("一份指定文件本轮未取得匹配片段；不能把未命中说成文件不存在。")
            retrieved.extend(own[:top_k])
    else:
        result = await request_for()
        retrieval_debug = result.retrieval_debug
        retrieved = result.records[:min(top_k, MAX_SOURCES)]
        selection.document_ids = list(dict.fromkeys(value for record in retrieved
                                      if (value := _uuid(record.metadata.get("document_id"))) is not None
                                      and (allowed_document_ids is None or str(value) in allowed_document_ids)))
        if not selection.document_ids:
            notes = ["检索片段无法绑定当前授权范围内的文件，已排除；不能据此判断文件不存在。"] if retrieved else []
            return [], {**(retrieval_debug or {}), "chat_evidence": {"notices": notes,
                       "clarification_required": False, "specified_files": 0, "sources": 0}}
    ids = list(dict.fromkeys(value for record in retrieved if (value := _uuid(record.metadata.get("chunk_id"))) is not None))
    rows = list((await db.execute(_current_rows(library.id).where(Chunk.id.in_(ids), Document.id.in_(selection.document_ids)))).all()) if ids else []
    by_id = {str(chunk.id): (chunk, document, revision) for chunk, document, revision in rows}
    safe: list[DifyRecord] = []
    safe_rows = {}
    for seed in retrieved:
        row = by_id.get(seed.metadata.get("chunk_id"))
        if row is None:
            continue
        chunk, document, revision = row
        if str(document.id) != seed.metadata.get("document_id"):
            continue
        if seed.metadata.get("document_revision_id") and str(chunk.document_revision_id) != seed.metadata["document_revision_id"]:
            continue
        if seed.metadata.get("document_revision") is not None and seed.metadata["document_revision"] != document.current_revision:
            continue
        record = _record_from_row(chunk, document, revision, seed)
        safe.append(record)
        safe_rows[str(chunk.id)] = row
    if len(safe) != len(retrieved):
        notices.append("部分检索片段无法绑定当前已发布文件版本，已排除；本轮证据范围不完整。")

    groups = []
    table_groups = []
    calculations = []
    grouped = set()
    for record in safe:
        chunk, document, revision = safe_rows[record.metadata["chunk_id"]]
        projection = record.metadata.get("evidence_locator_v1_projection")
        if not projection or not chunk.block_id or not revision:
            continue
        key = (document.id, revision.id, chunk.block_id)
        if key in grouped:
            continue
        parent = await db.get(DocumentBlock, chunk.block_id)
        if parent is None or parent.library_id != library.id or parent.document_id != document.id or parent.document_revision_id != revision.id or parent.block_kind != "table":
            continue
        parent_raw = (parent.content or {}).get("evidence_locator_v1")
        locator = parse_locator(parent_raw, document_id=document.id, document_revision_id=revision.id,
                                revision_no=revision.revision_no, unit_id=parent.id, parent_unit_id=parent.parent_block_id)
        if not locator or not parent.text or locator.unit_text_sha256 != sha256(parent.text.encode()).hexdigest():
            continue
        grouped.add(key)
        expanded = list((await db.execute(_current_rows(library.id).where(Document.id == document.id,
                        Chunk.document_revision_id == revision.id, Chunk.block_id == parent.id)
                        .order_by(Chunk.seq).limit(MAX_GROUP_CHUNKS + 1))).all())
        if len(expanded) > MAX_GROUP_CHUNKS:
            notices.append("一个命中表格组超过八个分片，未扩充完整范围；不得作完整合计。")
            continue
        group = [_record_from_row(c, d, r, record if c.id == chunk.id else None) for c, d, r in expanded]
        if not group or any("evidence_locator_v1_projection" not in member.metadata for member in group):
            notices.append("一个表格组缺少可信分片定位，完整范围未验证。")
            continue
        complete = _group_covers_parent(locator, group)
        if not complete:
            notices.append("一个表格组的可信行或文字范围覆盖不完整，只能回答实际片段中的局部事实。")
        for member in group:
            member.metadata["chat_table_group_complete"] = complete
        table_groups.append(group)
        if complete and re.search(r"合计|总计|总数|计数|统计|多少|数量|sum\b|count\b|total\b", query, re.I):
            result, calculation_gaps = calculate_table_snapshot(parent, locator, await _load_table_cells(db, parent))
            notices.extend(calculation_gaps)
            if result:
                source = locator.source.model_dump(exclude_none=True)
                location = (source.get("sheet") or {}).get("name", "")
                if "row" in source:
                    location += f"，第 {source['row']['start']}–{source['row']['end']} 行"
                calculations.append({"title": group[0].title, "location": location,
                    "chunk_ids": [member.metadata["chunk_id"] for member in group], "result": result})
    # A group's original order takes precedence over the rank of its seed.
    # Every specified file still reserves evidence before optional records.
    for document_id in selection.document_ids:
        own_groups = [group for group in table_groups if group[0].metadata["document_id"] == str(document_id)]
        if own_groups:
            groups.extend(own_groups)
        else:
            own = [record for record in safe if record.metadata["document_id"] == str(document_id)]
            if own:
                groups.append([own[0]])
    packed, gaps, calculation_scope = pack_calculated_evidence(groups, safe, calculations, max_context_chars)
    notices.extend(gaps)
    if not grouped and re.search(r"表格|合计|总数|计数|明细|总计|total|count|sum", query, re.I):
        notices.append("本轮未取得可证明完整范围的表格组；只能依据实际片段回答局部事实。")
    return packed, {**(retrieval_debug or {}), "chat_evidence": {"notices": list(dict.fromkeys(notices)), "clarification_required": False,
                                      "specified_files": len(selection.document_ids) if selection.explicit else 0, "sources": len(packed), **calculation_scope}}
