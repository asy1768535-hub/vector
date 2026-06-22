"""检索效果评测尺子：跑真实检索链路，输出 hit@k / MRR / Recall。

直连 service 层（app.services.retrieval.run_retrieval），不走 HTTP/认证。
需要 embedding 服务 + Qdrant + PostgreSQL 均可达（评测的是真实管道）。

用法：
  # 1) 从某个库已有文档自动造一批「弱」合成评测集（来源 B，先跑通流程）
  python scripts/eval_retrieval.py gen --library medical --n 20 --out eval/dataset.synthetic.jsonl

  # 2) 跑评测，对比 rerank 开/关（rerank 未配置则自动跳过）
  python scripts/eval_retrieval.py run --dataset eval/dataset.synthetic.jsonl --rerank both

  # 3) 调候选召回数、输出 markdown 报告
  python scripts/eval_retrieval.py run --dataset eval/dataset.jsonl --rerank on --candidate-k 100 --report eval/report.md

评测集字段见 eval/README.md。条目未写 library 时用 --library 兜底。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

# 让脚本可独立运行
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import func, select, text as sql_text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import async_session_factory  # noqa: E402
from app.deps import load_active_library  # noqa: E402
from app.models.chunk import Chunk  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.schemas.dify import DifyRetrievalRequest, RetrievalSetting  # noqa: E402
from app.services import rerank as rerank_svc  # noqa: E402
from app.services.eval import (  # noqa: E402
    Retrieved,
    aggregate,
    first_hit_rank,
    format_table,
    load_dataset,
    recall_at_k,
)
from app.services.retrieval import run_retrieval  # noqa: E402

log = logging.getLogger("eval_retrieval")


# ───────────────────────── run（评测） ─────────────────────────

def _resolve_configs(mode: str) -> list[tuple[str, bool]]:
    """把 --rerank 模式解析成 [(标签, rerank_enabled), ...]，rerank 未配置则剔除。"""
    want_dense = mode in ("both", "off")
    want_rerank = mode in ("both", "on")
    configs: list[tuple[str, bool]] = []
    if want_dense:
        configs.append(("dense", False))
    if want_rerank:
        if rerank_svc.is_configured():
            configs.append(("rerank", True))
        else:
            print("[warn] reranker 未配置（RERANK_BASE_URL/RERANK_MODEL 为空），跳过 rerank 对比")
    return configs


async def _run_eval(args: argparse.Namespace) -> int:
    items = load_dataset(args.dataset)
    if not items:
        print(f"[error] 评测集为空：{args.dataset}")
        return 2
    for it in items:
        if not it.library:
            it.library = args.library
    missing = [it.query for it in items if not it.library]
    if missing:
        print(f"[error] {len(missing)} 条样本未指定 library，且未提供 --library 兜底")
        return 2

    top_k = args.top_k
    ks = [k for k in (1, 3, 5) if k <= top_k] or [top_k]
    if args.candidate_k:
        settings.rerank_candidate_k = args.candidate_k  # 运行期覆盖召回候选数

    configs = _resolve_configs(args.rerank)
    if not configs:
        print("[error] 没有可跑的配置（--rerank on 但 reranker 未配置）")
        return 2

    print(f"评测集 {args.dataset}：{len(items)} 条 | top_k={top_k} | "
          f"candidate_k={settings.rerank_candidate_k} | 配置={[c[0] for c in configs]}")

    rows: list[dict] = []
    async with async_session_factory() as db:
        libcache: dict[str, object] = {}

        async def get_lib(slug: str):
            if slug not in libcache:
                libcache[slug] = await load_active_library(slug, db)
            return libcache[slug]

        for label, rr in configs:
            ranks: list[int | None] = []
            recalls: list[float | None] = []
            errors = 0
            for it in items:
                lib = await get_lib(it.library)
                if lib is None:
                    log.warning("库不存在：%s（query=%.30s）", it.library, it.query)
                    errors += 1
                    ranks.append(None)
                    recalls.append(None)
                    continue
                try:
                    resp = await run_retrieval(
                        collection=lib.qdrant_collection,
                        embedding_model=lib.embedding_model,
                        embedding_base_url=lib.embedding_base_url,
                        request=DifyRetrievalRequest(
                            knowledge_id=it.library,
                            query=it.query,
                            retrieval_setting=RetrievalSetting(top_k=top_k),
                        ),
                        source_config=lib.source_config,
                        rerank_enabled=rr,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("检索失败 [%s] query=%.30s → %s", label, it.query, exc)
                    errors += 1
                    ranks.append(None)
                    recalls.append(None)
                    continue
                recs = [
                    Retrieved(str(r.metadata.get("document_id") or ""), r.content)
                    for r in resp.records
                ]
                ranks.append(first_hit_rank(recs, it))
                recalls.append(recall_at_k(recs, it, top_k))

            agg = aggregate(ranks, recalls, ks=ks)
            agg["config"] = label
            agg["errors"] = errors
            rows.append(agg)

    table = format_table(rows, ks=ks, recall_k=top_k)
    print("\n" + table)

    if args.report:
        md = format_table(rows, ks=ks, recall_k=top_k, markdown=True)
        header = (f"# 检索评测报告\n\n"
                  f"- 评测集：`{args.dataset}`（{len(items)} 条）\n"
                  f"- top_k：{top_k} ｜ candidate_k：{settings.rerank_candidate_k}\n\n")
        Path(args.report).write_text(header + md + "\n", encoding="utf-8")
        print(f"\n报告已写入 {args.report}")
    return 0


# ───────────────────────── gen（合成弱评测集） ─────────────────────────

def _pseudo_query(chunk_text: str) -> str:
    """从 chunk 造伪 query：取第一句（中英文句末），太短/太长则退回前 80 字。"""
    t = " ".join(chunk_text.split())
    parts = re.split(r"(?<=[。．.!?！？])\s*", t)
    first = next((s for s in parts if s.strip()), "")
    q = first if 8 <= len(first) <= 120 else t[:80]
    return q.strip()


def _distinctive_fragment(chunk_text: str, n: int = 30) -> str:
    """取 chunk 中段一小段作为有辨识度的命中片段（避开开头，免得和 query 雷同）。"""
    t = " ".join(chunk_text.split())
    if len(t) <= n:
        return t
    start = min(len(t) // 3, len(t) - n)
    return t[start:start + n]


async def _gen(args: argparse.Namespace) -> int:
    async with async_session_factory() as db:
        lib = await load_active_library(args.library, db)
        if lib is None:
            print(f"[error] 库不存在：{args.library}")
            return 2
        if args.seed is not None:
            await db.execute(sql_text("SELECT setseed(:s)"), {"s": max(-1.0, min(1.0, args.seed))})
        rows = (await db.execute(
            select(Chunk.text, Chunk.document_id)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.library_id == lib.id, Document.deleted_at.is_(None))
            .order_by(func.random())
            .limit(args.n)
        )).all()

    if not rows:
        print(f"[error] 库 {args.library} 没有可用 chunk（文档可能还没入库/转向量）")
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for chunk_text, doc_id in rows:
            q = _pseudo_query(chunk_text)
            frag = _distinctive_fragment(chunk_text)
            if not q or not frag:
                continue
            fh.write(json.dumps({
                "query": q,
                "library": args.library,
                "expected_doc_ids": [str(doc_id)],
                "expected_text": [frag],
                "synthetic": True,
            }, ensure_ascii=False) + "\n")
            written += 1

    print(f"已生成 {written} 条合成弱评测样本 → {out_path}")
    print("提示：这是弱标签（query 源自文档本身），仅用于跑通流程；真实集请人工标注后放 eval/dataset.jsonl")
    return 0


# ───────────────────────── CLI ─────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="检索效果评测尺子")
    p.add_argument("--verbose", action="store_true", help="打印检索 INFO 日志")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="跑评测并输出指标表")
    pr.add_argument("--dataset", default="eval/dataset.jsonl", help="JSONL 评测集路径")
    pr.add_argument("--library", default=None, help="条目未写 library 时的兜底库 slug")
    pr.add_argument("--top-k", type=int, default=5, help="每条 query 取回多少条（默认 5）")
    pr.add_argument("--rerank", choices=["both", "on", "off"], default="both",
                    help="both=dense+rerank 对比；on=仅 rerank；off=仅 dense")
    pr.add_argument("--candidate-k", type=int, default=None, help="覆盖 rerank 召回候选数")
    pr.add_argument("--report", default=None, help="额外写出 markdown 报告的路径")

    pg = sub.add_parser("gen", help="从已有文档造合成弱评测集")
    pg.add_argument("--library", required=True, help="目标库 slug")
    pg.add_argument("--n", type=int, default=30, help="样本条数（默认 30）")
    pg.add_argument("--out", default="eval/dataset.synthetic.jsonl", help="输出 JSONL 路径")
    pg.add_argument("--seed", type=float, default=None, help="随机种子（-1~1，可复现抽样）")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.cmd == "run":
        rc = asyncio.run(_run_eval(args))
    elif args.cmd == "gen":
        rc = asyncio.run(_gen(args))
    else:  # pragma: no cover
        rc = 2
    sys.exit(rc)


if __name__ == "__main__":
    main()
