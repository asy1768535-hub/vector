"""检索评测的纯逻辑层：命中判定 + 指标聚合 + 表格格式化。

与检索链路、DB、网络解耦：所有函数只吃普通数据（EvalItem + Retrieved），
便于单测。真正跑检索、连库的部分在 scripts/eval_retrieval.py 里。

指标口径：
  - hit@k   = 「首个命中名次 <= k」的查询占比（命中即算，与命中几条无关）
  - MRR     = 平均 1/首命中名次；整轮未命中记 0
  - Recall@k= 仅当条目给了 expected_doc_ids（多文档）时有意义：top-k 命中的期望文档占比
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Sequence

_WS = re.compile(r"\s+")


def normalize(s: str) -> str:
    """小写 + 折叠空白，用于子串匹配（中英文混排也稳）。"""
    return _WS.sub(" ", (s or "").strip()).lower()


@dataclass
class EvalItem:
    """一条评测样本。expected_doc_ids 与 expected_text 至少给一个。"""

    query: str
    library: str | None = None
    expected_doc_ids: list[str] = field(default_factory=list)
    expected_text: list[str] = field(default_factory=list)
    note: str = ""
    synthetic: bool = False

    @staticmethod
    def from_dict(d: dict) -> "EvalItem":
        query = (d.get("query") or "").strip()
        if not query:
            raise ValueError("缺少 query")
        doc_ids = d.get("expected_doc_ids") or []
        texts = d.get("expected_text") or []
        if isinstance(doc_ids, str):
            doc_ids = [doc_ids]
        if isinstance(texts, str):
            texts = [texts]
        if not doc_ids and not texts:
            raise ValueError(f"query={query[:40]!r} 需至少给 expected_doc_ids 或 expected_text")
        return EvalItem(
            query=query,
            library=d.get("library"),
            expected_doc_ids=[str(x) for x in doc_ids],
            expected_text=[str(x) for x in texts],
            note=str(d.get("note") or ""),
            synthetic=bool(d.get("synthetic", False)),
        )


@dataclass
class Retrieved:
    """一条检索结果（从 DifyRecord 抽出的最小信息）。"""

    doc_id: str
    content: str


def load_dataset(path: str) -> list[EvalItem]:
    """读 JSONL 评测集；跳过空行与 # / // 注释行；坏行报 path:行号。"""
    items: list[EvalItem] = []
    with open(path, encoding="utf-8") as fh:
        for ln, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            try:
                items.append(EvalItem.from_dict(json.loads(line)))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"{path}:{ln}: {exc}") from exc
    return items


def is_hit(doc_id: str, content: str, item: EvalItem) -> bool:
    """该检索结果是否命中：doc_id 命中期望，或正文含任一期望片段（归一化子串）。"""
    if doc_id and doc_id in item.expected_doc_ids:
        return True
    if item.expected_text:
        norm = normalize(content)
        return any(normalize(t) in norm for t in item.expected_text if t)
    return False


def first_hit_rank(records: Sequence[Retrieved], item: EvalItem) -> int | None:
    """首个命中的 1-based 名次；全不中返回 None。"""
    for rank, r in enumerate(records, start=1):
        if is_hit(r.doc_id, r.content, item):
            return rank
    return None


def recall_at_k(records: Sequence[Retrieved], item: EvalItem, k: int) -> float | None:
    """top-k 命中的期望文档占比；条目没给 expected_doc_ids 则不适用（None）。"""
    expected = set(item.expected_doc_ids)
    if not expected:
        return None
    found = {r.doc_id for r in records[:k] if r.doc_id in expected}
    return len(found) / len(expected)


def aggregate(
    ranks: Sequence[int | None],
    recalls: Sequence[float | None] = (),
    *,
    ks: Sequence[int] = (1, 3, 5),
) -> dict:
    """把每条查询的（首命中名次、recall）汇总成一行指标。"""
    n = len(ranks)
    out: dict = {"n": n}
    for k in ks:
        hits = sum(1 for r in ranks if r is not None and r <= k)
        out[f"hit@{k}"] = (hits / n) if n else 0.0
    mrr = sum((1.0 / r) for r in ranks if r is not None)
    out["mrr"] = (mrr / n) if n else 0.0
    valid = [x for x in recalls if x is not None]
    out["recall"] = (sum(valid) / len(valid)) if valid else None
    return out


def _pct(x: object) -> str:
    return f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "-"


def format_table(
    rows: Sequence[dict],
    *,
    ks: Sequence[int] = (1, 3, 5),
    recall_k: int = 5,
    markdown: bool = False,
) -> str:
    """把若干 run 配置的指标行排成对齐表（或 markdown）。

    每行 dict 形如 {"config","hit@1","hit@3","hit@5","mrr","recall","n","errors"}。
    """
    headers = ["config", *[f"hit@{k}" for k in ks], "MRR", f"Recall@{recall_k}", "n", "err"]

    def cells(row: dict) -> list[str]:
        return [
            str(row.get("config", "")),
            *[_pct(row.get(f"hit@{k}")) for k in ks],
            f"{row.get('mrr', 0.0):.3f}",
            _pct(row.get("recall")) if row.get("recall") is not None else "-",
            str(row.get("n", 0)),
            str(row.get("errors", 0)),
        ]

    body = [cells(r) for r in rows]

    if markdown:
        lines = ["| " + " | ".join(headers) + " |",
                 "| " + " | ".join("---" for _ in headers) + " |"]
        lines += ["| " + " | ".join(c) + " |" for c in body]
        return "\n".join(lines)

    widths = [len(h) for h in headers]
    for c in body:
        widths = [max(w, len(v)) for w, v in zip(widths, c)]
    sep = "  "
    out = [sep.join(h.ljust(widths[i]) for i, h in enumerate(headers)),
           sep.join("-" * widths[i] for i in range(len(headers)))]
    out += [sep.join(v.ljust(widths[i]) for i, v in enumerate(c)) for c in body]
    return "\n".join(out)
