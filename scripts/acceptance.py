"""用户级闭环验收：全程走真实 HTTP API（带 Bearer Key），逐步打印证据。

覆盖用户要求的 6 步：
  1) 上传临时文档
  2) 确认任务 done
  3) 用文档明确内容检索命中
  4) 修改重传 → 新内容可检索、旧内容不可见
  5) 删除 → 立即检索不到
  6) 观察 Cleanup Worker 完成 Qdrant 物理清理

HTTP 用 httpx 打 API_BASE；Qdrant/outbox 的物理证据用项目自带客户端直查（旁证）。

用法：
  python scripts/acceptance.py --key <APIKEY> --slug accept_tmp
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx  # noqa: E402
from sqlalchemy import select, func  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import async_session_factory  # noqa: E402
from app.models.cleanup_outbox import CleanupOutbox  # noqa: E402
from app.models.library import Library  # noqa: E402

API_BASE = "http://localhost:8100"

PHRASE_V1 = "紫色河马在月球表面弹奏钢琴，编号 ACC-20260623-ALPHA"
PHRASE_V2 = "翠绿色海豚在火山口朗读莎士比亚，编号 ACC-20260623-BRAVO"

OK = "✅"
NO = "❌"


def line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("-" * 72)


async def poll_job_done(client: httpx.AsyncClient, slug: str, job_id: str, timeout=60.0) -> str:
    """轮询任务状态直到 done/failed 或超时。返回最终状态。"""
    deadline = time.monotonic() + timeout
    last = "?"
    while time.monotonic() < deadline:
        r = await client.get(f"{API_BASE}/libraries/{slug}/jobs/{job_id}")
        r.raise_for_status()
        last = r.json()["status"]
        if last in ("done", "failed"):
            return last
        await asyncio.sleep(1.0)
    return last


async def retrieve(client: httpx.AsyncClient, slug: str, query: str, top_k=5):
    """走 Dify 风格 /retrieval（用户级检索入口）。返回 records 列表。"""
    r = await client.post(
        f"{API_BASE}/retrieval",
        json={"knowledge_id": slug, "query": query,
              "retrieval_setting": {"top_k": top_k, "score_threshold": 0}},
    )
    r.raise_for_status()
    return r.json().get("records", [])


def hit_doc(records, doc_id: str) -> bool:
    return any((rec.get("metadata") or {}).get("document_id") == doc_id for rec in records)


def hit_phrase(records, phrase: str) -> bool:
    key = phrase[:12]
    return any(key in (rec.get("content") or "") for rec in records)


async def qdrant_live_points(collection: str, doc_id: str) -> int:
    """直查 Qdrant REST /points/count：该 document_id 当前还有多少个 point（物理清理硬证据）。"""
    headers = {"Content-Type": "application/json"}
    if settings.qdrant_api_key:
        headers["api-key"] = settings.qdrant_api_key
    body = {
        "exact": True,
        "filter": {"must": [{"key": "document_id", "match": {"value": doc_id}}]},
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{settings.qdrant_url}/collections/{collection}/points/count",
            json=body, headers=headers,
        )
        r.raise_for_status()
        return int(r.json()["result"]["count"])


async def outbox_state(doc_id: str):
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(CleanupOutbox.status, func.count())
            .where(CleanupOutbox.document_id == doc_id)
            .group_by(CleanupOutbox.status)
        )).all()
        return {s: int(c) for s, c in rows}


async def load_collection(slug: str) -> str:
    """从库配置读取真实 collection 名（不假设是 lib_{slug}——迁移过的库可能不一致）。"""
    async with async_session_factory() as db:
        coll = (await db.execute(
            select(Library.qdrant_collection).where(
                Library.slug == slug, Library.deleted_at.is_(None)
            )
        )).scalar_one_or_none()
    if not coll:
        raise SystemExit(f"[error] 库不存在或无 collection：{slug}")
    return coll


async def main(key: str, slug: str) -> int:
    collection = await load_collection(slug)
    headers = {"Authorization": f"Bearer {key}"}
    ok_all = True
    doc_id: str | None = None
    deleted = False
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:

        # ── 步骤 1：上传临时文档 ──────────────────────────────
        line("步骤 1：上传临时文档（v1）")
        r = await client.post(
            f"{API_BASE}/libraries/{slug}/documents",
            json={"title": "验收临时文档",
                  "text": f"本文档用于用户级闭环验收。唯一标识句：{PHRASE_V1}。"
                          f"这句话只在第一版出现，用于验证检索命中与旧版本不可见。",
                  "metadata": {"purpose": "acceptance", "version": "v1"}},
        )
        r.raise_for_status()
        d = r.json()
        doc_id, job_id = str(d["document_id"]), str(d["job_id"])
        print(f"{OK} 上传成功  document_id={doc_id}")
        print(f"   job_id={job_id}  初始 status={d['status']}  chunk_count={d['chunk_count']}")

        try:
            # ── 步骤 2：确认任务 done ─────────────────────────────
            line("步骤 2：轮询任务监控直到 done")
            st = await poll_job_done(client, slug, job_id)
            ok = st == "done"
            ok_all &= ok
            print(f"{OK if ok else NO} 任务最终状态：{st}")
            pts = await qdrant_live_points(collection, doc_id)
            print(f"   Qdrant 中该文档向量点数：{pts}")

            # ── 步骤 3：用明确内容检索命中 ────────────────────────
            line("步骤 3：用文档明确内容检索（应命中 v1）")
            recs = await retrieve(client, slug, PHRASE_V1)
            ok = hit_doc(recs, doc_id) and hit_phrase(recs, PHRASE_V1)
            ok_all &= ok
            print(f"{OK if ok else NO} 命中 v1：返回 {len(recs)} 条，命中本文档={hit_doc(recs, doc_id)}，含 v1 标识句={hit_phrase(recs, PHRASE_V1)}")
            if recs:
                top = recs[0]
                print(f"   top1 score={top.get('score'):.4f}  content={(top.get('content') or '')[:60]}…")

            # ── 步骤 4：修改重传 → 新可检索、旧不可见 ──────────────
            line("步骤 4：修改文档重新上传（v2），新内容可检索、旧内容不可见")
            r = await client.put(
                f"{API_BASE}/libraries/{slug}/documents/{doc_id}",
                json={"title": "验收临时文档",
                      "text": f"本文档已修改为第二版。新的唯一标识句：{PHRASE_V2}。"
                              f"第一版的标识句不应再被检索到。",
                      "metadata": {"purpose": "acceptance", "version": "v2"}},
            )
            r.raise_for_status()
            job2 = str(r.json()["job_id"])
            print(f"   reingest job_id={job2}，等待 done…")
            st2 = await poll_job_done(client, slug, job2)
            print(f"   v2 任务状态：{st2}")

            recs_new = await retrieve(client, slug, PHRASE_V2)
            ok_new = hit_phrase(recs_new, PHRASE_V2)
            recs_old = await retrieve(client, slug, PHRASE_V1)
            old_visible = hit_phrase(recs_old, PHRASE_V1)
            ok_old = not old_visible
            ok_all &= ok_new and ok_old
            print(f"{OK if ok_new else NO} 新内容(v2)可检索：{ok_new}（top1 score={recs_new[0].get('score'):.4f}）" if recs_new else f"{NO} 新内容(v2)无返回")
            print(f"{OK if ok_old else NO} 旧内容(v1)不可见：{ok_old}（用 v1 标识句检索，结果里含 v1 句={old_visible}）")
            pts2 = await qdrant_live_points(collection, doc_id)
            print(f"   Qdrant 该文档点数（含尚未物理清理的旧版残留）：{pts2}")

            # ── 步骤 5：删除 → 立即检索不到 ───────────────────────
            line("步骤 5：删除文档，立即检索确认不可见")
            r = await client.delete(f"{API_BASE}/libraries/{slug}/documents/{doc_id}")
            deleted = r.status_code == 204
            print(f"   DELETE 返回 HTTP {r.status_code}")
            recs_del = await retrieve(client, slug, PHRASE_V2)
            gone = not hit_doc(recs_del, doc_id) and not hit_phrase(recs_del, PHRASE_V2)
            ok_all &= gone
            print(f"{OK if gone else NO} 删除后立即检索不到：{gone}（用 v2 标识句检索，命中本文档={hit_doc(recs_del, doc_id)}）")
            ob = await outbox_state(doc_id)
            print(f"   cleanup_outbox 入队状态：{ob}")

            # ── 步骤 6：观察 Cleanup Worker 物理清理 ──────────────
            line("步骤 6：观察 Cleanup Worker 完成 Qdrant 物理清理")
            deadline = time.monotonic() + 90.0
            pts_now = await qdrant_live_points(collection, doc_id)
            while pts_now > 0 and time.monotonic() < deadline:
                await asyncio.sleep(2.0)
                pts_now = await qdrant_live_points(collection, doc_id)
            ob2 = await outbox_state(doc_id)
            cleaned = pts_now == 0
            ok_all &= cleaned
            print(f"{OK if cleaned else NO} Qdrant 物理清理完成：该文档剩余点数={pts_now}")
            print(f"   cleanup_outbox 最终状态：{ob2}（done = worker 已消费并物理删除）")
        finally:
            # 失败也要清理临时文档（正常路径已在步骤5删除，这里兜底）
            if doc_id and not deleted:
                try:
                    rr = await client.delete(f"{API_BASE}/libraries/{slug}/documents/{doc_id}")
                    print(f"\n[cleanup] 兜底删除临时文档 {doc_id} → HTTP {rr.status_code}")
                except Exception as exc:  # noqa: BLE001
                    print(f"\n[cleanup] 兜底删除失败（请手动清理 {doc_id}）：{exc}")

    line("总判定")
    print(f"{OK + ' 全部通过' if ok_all else NO + ' 存在未通过项'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="用户级闭环验收（API Key 从环境变量 ACC_API_KEY 读，不走命令行避免留痕）",
    )
    p.add_argument("--slug", default="accept_tmp")
    a = p.parse_args()
    key = os.environ.get("ACC_API_KEY")
    if not key:
        sys.exit("[error] 请用环境变量传 Key：ACC_API_KEY=<key> python scripts/acceptance.py --slug <lib>")
    sys.exit(asyncio.run(main(key, a.slug)))
