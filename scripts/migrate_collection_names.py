"""把 Qdrant collection 名字从 lib_<uuid> 迁移到 lib_<slug>。

对每个 deleted_at IS NULL 的库：
  1. 检查 PG 里 qdrant_collection 是否已经是 f"lib_{slug}"，是则跳过
  2. 否则：
     - 删旧 Qdrant collection（如果存在）
     - 用当前库参数（dim, distance）建新 collection 名 = lib_<slug>
     - UPDATE sys_libraries.qdrant_collection
     - 把该库所有 documents 标 pending、所有 embedding_jobs 标 pending
       → worker 会重新 embed 全部（前提：旧 collection 里的 points 也要丢，因为新 collection 是空的）

用法：
    python scripts/migrate_collection_names.py            # dry-run（只打印计划）
    python scripts/migrate_collection_names.py --apply    # 实际执行

⚠️ 执行后已有文档需要 worker 重新 embed。先停 worker → 跑迁移 → 再开 worker，避免中间状态。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from sqlalchemy import select, text

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import async_session_factory  # noqa: E402
from app.models.library import Library  # noqa: E402
from app.services import qdrant  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("migrate_collection_names")


def _target_name(slug: str) -> str:
    return f"lib_{slug}"


async def _plan() -> list[tuple[Library, str]]:
    """返回需要迁移的 [(library, target_collection_name), ...]"""
    async with async_session_factory() as s:
        rows = await s.execute(
            select(Library).where(Library.deleted_at.is_(None))
        )
        libs = list(rows.scalars().all())
    plan: list[tuple[Library, str]] = []
    for lib in libs:
        target = _target_name(lib.slug)
        if lib.qdrant_collection == target:
            continue
        plan.append((lib, target))
    return plan


async def _apply_one(lib: Library, target: str) -> None:
    log.info(
        "migrating slug=%s : %s → %s (dim=%s, distance=%s)",
        lib.slug, lib.qdrant_collection, target, lib.embedding_dim, lib.vector_distance,
    )

    # 1. 删旧 collection
    try:
        await qdrant.delete_collection(lib.qdrant_collection)
        log.info("  deleted old: %s", lib.qdrant_collection)
    except Exception:  # noqa: BLE001
        log.exception("  delete old collection failed; continuing")

    # 2. 建新 collection
    await qdrant.ensure_collection(target, dim=lib.embedding_dim, distance=lib.vector_distance)
    log.info("  created new: %s", target)

    # 3. 更新 PG + 重置 documents / jobs（worker 会重新 embed 全部）
    async with async_session_factory() as s:
        await s.execute(
            text(
                "UPDATE sys_libraries SET qdrant_collection = :new WHERE id = :id"
            ),
            {"new": target, "id": lib.id},
        )
        await s.execute(
            text(
                "UPDATE documents "
                "SET status = 'pending', last_error = NULL "
                "WHERE library_id = :id AND deleted_at IS NULL"
            ),
            {"id": lib.id},
        )
        await s.execute(
            text(
                "UPDATE embedding_jobs "
                "SET status = 'pending', worker_id = NULL, claimed_at = NULL, "
                "    finished_at = NULL, attempt_count = 0, last_error = NULL "
                "WHERE library_id = :id"
            ),
            {"id": lib.id},
        )
        await s.commit()
    log.info("  PG updated; documents + jobs reset to pending")


async def _run(apply: bool) -> None:
    plan = await _plan()
    if not plan:
        log.info("nothing to migrate; all libraries already use lib_<slug> naming")
        return
    log.info("plan (%d libraries):", len(plan))
    for lib, target in plan:
        log.info(
            "  slug=%-30s  %s  →  %s",
            lib.slug, lib.qdrant_collection, target,
        )
    if not apply:
        log.info("dry-run finished; pass --apply to execute")
        return
    for lib, target in plan:
        await _apply_one(lib, target)
    log.info("done. worker 现在会重新 embed 这些库的所有文档。")


def main() -> None:
    p = argparse.ArgumentParser(description="Rename Qdrant collections from lib_<uuid> to lib_<slug>.")
    p.add_argument("--apply", action="store_true", help="Actually execute. Without this flag, only print plan.")
    args = p.parse_args()
    asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    main()
