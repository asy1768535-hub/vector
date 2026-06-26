"""关键词检索（Hybrid 第一版）：在本系统自己的 PG 里搜 chunks.text + documents.title/external_id。

约束（任务 §3）：
  - 必须 JOIN documents 并过滤 deleted_at（删档绝不出现）；
  - 必须限定 library_id（库隔离）；
  - title / external_id 命中加权（文件名、文号类问题更稳）；
  - 返回与 dense 命中可融合的结构：{"id": chunk_id, "score": float, "payload": {...}}。

优先 pg_trgm word_similarity；pg_trgm 不可用（迁移因权限回退）时退到 ILIKE 分词匹配。
不引入任何外部服务。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

log = logging.getLogger(__name__)

_trgm_available: bool | None = None       # 进程级缓存（pg_trgm 是否可用）
_TOKEN_RE = re.compile(r"[0-9A-Za-z一-鿿]+")


async def _has_trgm(db: AsyncSession) -> bool:
    global _trgm_available
    if _trgm_available is None:
        try:
            row = await db.execute(text("SELECT 1 FROM pg_extension WHERE extname='pg_trgm'"))
            _trgm_available = row.scalar() is not None
        except Exception:  # noqa: BLE001 - 探测失败按不可用处理
            _trgm_available = False
    return _trgm_available


def _tokens(query: str) -> list[str]:
    """ILIKE 回退用：取字母/数字/汉字连续片段（长度≥2），去重保序，限量。"""
    seen: set[str] = set()
    out: list[str] = []
    for t in _TOKEN_RE.findall(query or ""):
        if len(t) >= 2 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:8]


def _hit(row) -> dict:
    chunk_id = str(row.chunk_id)
    return {
        "id": chunk_id,
        "score": float(row.score or 0.0),
        "payload": {
            "chunk_id": chunk_id,
            "document_id": str(row.document_id),
            "library_id": str(row.library_id),
            "text": row.text,
            "title": row.title,
            "external_id": row.external_id,
            # keyword 命中来自实时 PG（非 Qdrant 快照），其 revision 必为当前版本。
            # 必须带上，否则 visibility 会把缺失视作 1，更新过(current_revision>1)的文档会被误过滤。
            "document_revision": int(row.document_revision),
        },
    }


async def recall(db: AsyncSession, library, query: str, *, limit: int) -> list[dict]:
    """关键词召回最多 limit 条。空 query / 缺上下文 → []。"""
    if db is None or library is None or not (query or "").strip():
        return []
    lib_id = str(library.id)
    tb = settings.hybrid_keyword_title_boost
    eb = settings.hybrid_keyword_external_id_boost

    if await _has_trgm(db):
        thr = settings.hybrid_keyword_threshold
        sql = text(
            """
            SELECT c.id AS chunk_id, c.document_id, c.library_id, c.text,
                   d.title, d.external_id, d.current_revision AS document_revision,
                   GREATEST(
                     word_similarity(:q, c.text),
                     word_similarity(:q, COALESCE(d.title, '')) * :tb,
                     word_similarity(:q, COALESCE(d.external_id, '')) * :eb
                   ) AS score
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.library_id = :lib
              AND d.deleted_at IS NULL
              AND (
                    word_similarity(:q, c.text) >= :thr
                 OR word_similarity(:q, COALESCE(d.title, '')) >= :thr
                 OR word_similarity(:q, COALESCE(d.external_id, '')) >= :thr
              )
            ORDER BY score DESC, c.id ASC
            LIMIT :limit
            """
        )
        params = {"q": query, "lib": lib_id, "tb": tb, "eb": eb, "thr": thr, "limit": limit}
        rows = (await db.execute(sql, params)).all()
        return [_hit(r) for r in rows]

    # 回退：ILIKE 分词（pg_trgm 不可用时；title/external_id 命中加权）
    toks = _tokens(query)
    if not toks:
        return []
    params = {"lib": lib_id, "limit": limit, "tb": tb, "eb": eb}
    conds, score_terms = [], []
    for i, t in enumerate(toks):
        params[f"t{i}"] = f"%{t}%"
        conds.append(f"c.text ILIKE :t{i} OR d.title ILIKE :t{i} OR d.external_id ILIKE :t{i}")
        score_terms.append(
            f"(CASE WHEN c.text ILIKE :t{i} THEN 1 ELSE 0 END)"
            f" + (CASE WHEN d.title ILIKE :t{i} THEN :tb ELSE 0 END)"
            f" + (CASE WHEN d.external_id ILIKE :t{i} THEN :eb ELSE 0 END)"
        )
    where = " OR ".join(f"({c})" for c in conds)
    score_expr = " + ".join(score_terms)
    sql = text(
        f"""
        SELECT c.id AS chunk_id, c.document_id, c.library_id, c.text,
               d.title, d.external_id, d.current_revision AS document_revision,
               ({score_expr}) AS score
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.library_id = :lib
          AND d.deleted_at IS NULL
          AND ({where})
        ORDER BY score DESC, c.id ASC
        LIMIT :limit
        """
    )
    rows = (await db.execute(sql, params)).all()
    return [_hit(r) for r in rows]
