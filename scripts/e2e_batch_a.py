"""批次 A 可重复 E2E（#6）：真实 DB + Qdrant + embedding，临时 managed 库，跑完清理。

可重复执行（每次随机 slug、结束清理），用于回归而非一次性验收：

    python scripts/e2e_batch_a.py

覆盖：
  R-rev  : 新建 rev1 可见 → reingest 后旧 rev 被 revision 过滤(不可见) → 重 embed rev2 可见
  R-rb   : 三阶段 rebuild → 每文档一条带 operation 的 job、finalize 后库 ready、revision 再 +1
  R-resume: prepare 后崩溃 → run_rebuild 恢复同一 operation（不新建）→ 完成
  R-final: finalize 幂等（重复调用不二次完成、不报错）
  R-empty: 空库 rebuild expected=0 直接完成
"""
import asyncio
import sys
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sqlalchemy import select

from app.config import settings
from app.db import async_session_factory
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.rebuild_operation import RebuildOperation
from app.services import embedding, qdrant, ingest as ing, rebuild as rebuild_svc
from app.services.retrieval import _recall_visible
from app.workers.embedder import _process_job

RUN = uuid.uuid4().hex[:8]
SLUGS = []
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  {'[PASS]' if cond else '[FAIL]'} {name}" + (f" - {detail}" if detail else ""))


async def _new_lib(name_suffix):
    slug = f"e2e_{RUN}_{name_suffix}"
    coll = f"lib_{slug}"
    SLUGS.append(slug)
    async with async_session_factory() as s:
        lib = Library(
            slug=slug, name=f"E2E {name_suffix}", qdrant_collection=coll,
            embedding_model=settings.embedding_model, embedding_dim=settings.embedding_dim,
            embedding_base_url=settings.embedding_base_url,
            chunk_size=1000, chunk_overlap=120, vector_distance="cosine",
            lifecycle_mode="managed", index_state="ready",
        )
        s.add(lib); await s.commit()
        lib_id = lib.id
    await qdrant.ensure_collection(coll, dim=settings.embedding_dim, distance="cosine")
    return lib_id


async def _process_pending(lib_id):
    async with async_session_factory() as s:
        jobs = (await s.execute(select(EmbeddingJob).where(
            EmbeddingJob.library_id == lib_id, EmbeddingJob.status == "pending"))).scalars().all()
    for j in jobs:
        async with async_session_factory() as s:
            await _process_job(s, j)


async def _visible_ids(lib_id, query):
    async with async_session_factory() as s:
        lib = await s.get(Library, lib_id)
        vec = await embedding.embed_one(query, model=lib.embedding_model, base_url=lib.embedding_base_url)
        raw = await _recall_visible(s, lib, lib.qdrant_collection, vec, needed=10)
    return {(it.get("payload") or {}).get("document_id") for it in raw}


async def _get(model, _id):
    async with async_session_factory() as s:
        return await s.get(model, _id)


async def main():
    T1 = "高血压是常见慢性心血管疾病，需长期管理与随访。"
    T2 = "糖尿病是以高血糖为特征的代谢性疾病，需控制饮食与用药。"

    # ── R-rev：revision 过滤 ──────────────────────────────────────────
    lib_id = await _new_lib("rev")
    async with async_session_factory() as s:
        lib = await s.get(Library, lib_id)
        doc, _, _, _ = await ing.ingest_text(db=s, library=lib, text=T1, title="d", external_id="E1",
                                             metadata=None, splitter="text", created_by=None)
        await s.commit(); doc_id = doc.id
    await _process_pending(lib_id)
    check("R-rev 新建 rev1 可见", str(doc_id) in await _visible_ids(lib_id, T1))

    async with async_session_factory() as s:
        lib = await s.get(Library, lib_id)
        d = (await s.execute(select(Document).where(Document.id == doc_id).with_for_update())).scalar_one()
        await ing.reingest_document(db=s, library=lib, document=d, new_text=T2, title="d2",
                                    metadata=None, splitter="text", force=True)
        await s.commit()
    rev_after = (await _get(Document, doc_id)).current_revision
    check("R-rev reingest 后旧 rev 被过滤(不可见)", str(doc_id) not in await _visible_ids(lib_id, T2),
          f"rev={rev_after}")
    await _process_pending(lib_id)
    check("R-rev rev2 重 embed 后可见", str(doc_id) in await _visible_ids(lib_id, T2))

    # ── R-rb：三阶段 rebuild ─────────────────────────────────────────
    async with async_session_factory() as s:
        await rebuild_svc.run_rebuild(s, lib_id)
    await _process_pending(lib_id)
    lib = await _get(Library, lib_id)
    d3 = await _get(Document, doc_id)
    check("R-rb rebuild 后库 ready", lib.index_state == "ready" and lib.active_rebuild_operation_id is None,
          lib.index_state)
    check("R-rb rebuild 后 revision 再 +1", d3.current_revision == rev_after + 1)
    check("R-rb rebuild 后可检索", str(doc_id) in await _visible_ids(lib_id, T2))

    # ── R-final：finalize 幂等 ───────────────────────────────────────
    async with async_session_factory() as s:
        op_id = (await s.execute(select(RebuildOperation.id).where(
            RebuildOperation.library_id == lib_id, RebuildOperation.status == "done"
        ).order_by(RebuildOperation.created_at.desc()).limit(1))).scalar_one()
    async with async_session_factory() as s:
        again = await rebuild_svc.try_finalize(s, op_id)
    lib2 = await _get(Library, lib_id)
    check("R-final 重复 finalize 幂等(无二次推进、库仍 ready)", again is False and lib2.index_state == "ready")

    # ── R-resume：prepare 后崩溃 → 恢复同一 operation ────────────────
    lib_r = await _new_lib("resume")
    async with async_session_factory() as s:
        lib = await s.get(Library, lib_r)
        doc, _, _, _ = await ing.ingest_text(db=s, library=lib, text=T1, title="r", external_id="R1",
                                             metadata=None, splitter="text", created_by=None)
        await s.commit(); rdoc = doc.id
    await _process_pending(lib_r)
    # 模拟崩溃：只跑 prepare（建 preparing operation + bump revision），不 qdrant/activate
    async with async_session_factory() as s:
        prep_op_id, *_ = await rebuild_svc._prepare(s, lib_r)
    # 恢复：run_rebuild 应识别 preparing operation 并续跑（不新建）
    async with async_session_factory() as s:
        resumed_id = await rebuild_svc.run_rebuild(s, lib_r)
    await _process_pending(lib_r)
    libr = await _get(Library, lib_r)
    async with async_session_factory() as s:
        op_cnt = (await s.execute(select(RebuildOperation).where(
            RebuildOperation.library_id == lib_r))).scalars().all()
    check("R-resume 恢复同一 operation(未新建)", str(prep_op_id) == resumed_id and len(op_cnt) == 1,
          f"prep={prep_op_id} resumed={resumed_id} ops={len(op_cnt)}")
    check("R-resume 恢复后库 ready", libr.index_state == "ready", libr.index_state)
    check("R-resume 恢复后可检索", str(rdoc) in await _visible_ids(lib_r, T1))

    # ── R-empty：空库 rebuild 直接完成 ──────────────────────────────
    lib_e = await _new_lib("empty")
    async with async_session_factory() as s:
        await rebuild_svc.run_rebuild(s, lib_e)
    libe = await _get(Library, lib_e)
    check("R-empty 空库 rebuild 直接 ready", libe.index_state == "ready", libe.index_state)

    print(f"\n结果：{sum(results)}/{len(results)} 通过  (run={RUN})")
    return all(results)


async def _cleanup():
    for slug in SLUGS:
        try:
            await qdrant.delete_collection(f"lib_{slug}")
        except Exception as e:
            print("清理 collection 失败:", slug, e)
    async with async_session_factory() as db:
        for slug in SLUGS:
            lib = (await db.execute(select(Library).where(Library.slug == slug))).scalar_one_or_none()
            if lib:
                await db.execute(EmbeddingJob.__table__.delete().where(EmbeddingJob.library_id == lib.id))
                await db.execute(Chunk.__table__.delete().where(Chunk.library_id == lib.id))
                await db.execute(RebuildOperation.__table__.delete().where(RebuildOperation.library_id == lib.id))
                await db.execute(Document.__table__.delete().where(Document.library_id == lib.id))
                await db.execute(Library.__table__.delete().where(Library.id == lib.id))
                await db.commit()
    print(f"[清理] 删除 {len(SLUGS)} 个临时库及数据")


async def _run():
    try:
        return await main()
    finally:
        await _cleanup()


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(_run()) else 1)
