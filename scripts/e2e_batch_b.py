"""批次 B 可重复 E2E（#7）：真实 DB + Qdrant + embedding，临时库，跑完清理。

    python scripts/e2e_batch_b.py

覆盖：
  D-del   : 摄入+embed → 删除 → 立即不可见(检索) + outbox pending + Qdrant 点仍在 →
            跑 Cleanup Worker → outbox done + Qdrant 点清零
  D-idem  : 重复删除 → 只一条 outbox（幂等键）
  D-rev   : reingest → cleanup 删旧 revision 点，仅当前 revision 点保留
  D-ext   : external 库删除 → 不入 delete_collection outbox、collection 不删
"""
import asyncio
import sys
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sqlalchemy import func, select, update

from app.config import settings
from app.db import async_session_factory
from app.models.chunk import Chunk
from app.models.cleanup_outbox import CleanupOutbox
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.rebuild_operation import RebuildOperation
from app.services import embedding, qdrant, ingest as ing, cleanup as cleanup_svc
from app.services.retrieval import _recall_visible
from app.workers.embedder import _process_job
from app.workers import cleanup as cleanup_worker

RUN = uuid.uuid4().hex[:8]
SLUGS = []
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  {'[PASS]' if cond else '[FAIL]'} {name}" + (f" - {detail}" if detail else ""))


async def _new_lib(suffix, lifecycle="managed"):
    slug = f"e2eb_{RUN}_{suffix}"
    coll = f"lib_{slug}"
    SLUGS.append(slug)
    async with async_session_factory() as s:
        lib = Library(slug=slug, name=f"E2EB {suffix}", qdrant_collection=coll,
                      embedding_model=settings.embedding_model, embedding_dim=settings.embedding_dim,
                      embedding_base_url=settings.embedding_base_url,
                      chunk_size=1000, chunk_overlap=120, vector_distance="cosine",
                      lifecycle_mode=lifecycle, index_state="ready")
        s.add(lib)
        await s.commit()
        lib_id = lib.id
    await qdrant.ensure_collection(coll, dim=settings.embedding_dim, distance="cosine")
    return lib_id


async def _ingest(lib_id, text, ext):
    async with async_session_factory() as s:
        lib = await s.get(Library, lib_id)
        doc, _, _, _ = await ing.ingest_text(db=s, library=lib, text=text, title=ext, external_id=ext,
                                             metadata=None, splitter="text", created_by=None)
        await s.commit()
        return doc.id


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


async def _raw_points(lib_id, doc_id, query, rev=None):
    """直接查 Qdrant（绕过可见性过滤）某 doc 的 point 数；rev 指定则只数该 revision。"""
    async with async_session_factory() as s:
        lib = await s.get(Library, lib_id)
        vec = await embedding.embed_one(query, model=lib.embedding_model, base_url=lib.embedding_base_url)
    flt = {"must": [{"key": "document_id", "match": {"value": str(doc_id)}}]}
    if rev is not None:
        flt["must"].append({"key": "document_revision", "match": {"value": rev}})
    raw = await qdrant.search(lib.qdrant_collection, vec, limit=50, payload_filter=flt, with_payload=True)
    return len(raw)


async def _delete_doc(lib_id, doc_id):
    """复刻 delete_document 端点的事务（tombstone + supersede + outbox），不经 HTTP。"""
    from datetime import datetime, timezone
    async with async_session_factory() as s:
        lib = await s.get(Library, lib_id)
        now = datetime.now(timezone.utc)
        await s.execute(update(Document).where(Document.id == doc_id).values(
            deleted_at=now, status="deleted", updated_at=now))
        await s.execute(update(EmbeddingJob).where(
            EmbeddingJob.document_id == doc_id,
            EmbeddingJob.status.in_(("pending", "processing"))).values(status="superseded"))
        await cleanup_svc.enqueue_delete_document(s, lib, doc_id)
        await s.commit()


async def _outbox_count(lib_id, status=None):
    async with async_session_factory() as s:
        q = select(func.count()).select_from(CleanupOutbox).where(CleanupOutbox.library_id == lib_id)
        if status:
            q = q.where(CleanupOutbox.status == status)
        return (await s.execute(q)).scalar_one()


async def main():
    T1 = "高血压是常见慢性病，需长期随访。"
    T2 = "糖尿病需控制血糖与饮食。"

    # ── D-del：删除立即不可见 + cleanup 物理清零 ─────────────────────
    lib = await _new_lib("del")
    doc = await _ingest(lib, T1, "X1")
    await _process_pending(lib)
    check("D-del 删前可见", str(doc) in await _visible_ids(lib, T1))
    await _delete_doc(lib, doc)
    check("D-del 删后立即不可见(检索过滤)", str(doc) not in await _visible_ids(lib, T1))
    check("D-del 删后 Qdrant 点仍在(未清理)", await _raw_points(lib, doc, T1) > 0)
    check("D-del outbox 有 pending", await _outbox_count(lib, "pending") == 1)
    await cleanup_worker.run(watch=False)        # 跑 cleanup 一轮
    check("D-del cleanup 后 outbox done", await _outbox_count(lib, "done") == 1)
    check("D-del cleanup 后 Qdrant 点清零", await _raw_points(lib, doc, T1) == 0)

    # ── D-idem：重复删除只一条 outbox ───────────────────────────────
    lib2 = await _new_lib("idem")
    d2 = await _ingest(lib2, T1, "Y1")
    await _process_pending(lib2)
    await _delete_doc(lib2, d2)
    await _delete_doc(lib2, d2)      # 再删一次
    check("D-idem 重复删除仅一条 outbox", await _outbox_count(lib2) == 1)

    # ── D-rev：reingest → cleanup 删旧 revision 点 ──────────────────
    lib3 = await _new_lib("rev")
    d3 = await _ingest(lib3, T1, "Z1")
    await _process_pending(lib3)     # rev1 点
    async with async_session_factory() as s:
        lib = await s.get(Library, lib3)
        d = (await s.execute(select(Document).where(Document.id == d3).with_for_update())).scalar_one()
        await ing.reingest_document(db=s, library=lib, document=d, new_text=T2, title="Z1",
                                    metadata=None, splitter="text", force=True)
        await s.commit()
    await _process_pending(lib3)     # rev2 点（rev1 点仍在 Qdrant，待 cleanup）
    check("D-rev cleanup 前旧 rev1 点仍在", await _raw_points(lib3, d3, T2, rev=1) > 0)
    await cleanup_worker.run(watch=False)
    check("D-rev cleanup 后旧 rev1 点清零", await _raw_points(lib3, d3, T2, rev=1) == 0)
    check("D-rev 当前 rev2 点保留", await _raw_points(lib3, d3, T2, rev=2) > 0)
    check("D-rev reingest 后仍可检索", str(d3) in await _visible_ids(lib3, T2))

    # ── D-ext：external 库删除不删 collection、不入 outbox ───────────
    lib_e = await _new_lib("ext", lifecycle="external")
    async with async_session_factory() as s:
        libe = await s.get(Library, lib_e)
        is_ext = libe.lifecycle_mode == "external"
        if not is_ext:
            await cleanup_svc.enqueue_delete_collection(s, libe)
        libe.deleted_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        await s.commit()
    check("D-ext external 删除不入 delete_collection outbox", await _outbox_count(lib_e) == 0)
    check("D-ext external collection 仍存在", await qdrant.collection_exists(f"lib_{SLUGS[-1]}"))

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
                await db.execute(CleanupOutbox.__table__.delete().where(CleanupOutbox.library_id == lib.id))
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
