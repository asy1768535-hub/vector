"""启动期 / 运行期自检：探活 embedding 服务与 Qdrant。

目的：配置错配（模型名不存在 / API Key 错 / base_url 不对 / Qdrant 连不上）应在
**启动时就大声报出来**，而不是等第一篇文档摄入失败、worker 默默重试到上限才暴露。

设计：**非 fatal** —— 任何失败只打 ERROR 日志，不抛异常、不退出进程，这样配置错了
仍能起服务、登后台改回来。复用 embedding.embed_one（全局模型/url/key）和 Qdrant /healthz。
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.services import embedding
from app.services import rerank as rerank_svc

log = logging.getLogger(__name__)


async def probe_embedding_config(
    *,
    model: str | None,
    base_url: str | None,
    expected_dim: int,
    api_key: str | None = None,
) -> tuple[bool, str, int | None]:
    """对指定 embedding 配置做一次真实 embed，验证模型名/key/地址可用 + 维度匹配。

    返回 (ok, message, dim)：
      - 成功且维度匹配                → (True, "ok", dim)
      - 成功但维度与 expected_dim 不符 → (True, "dim mismatch ...", dim)
      - 失败（模型不存在/鉴权失败/连不上）→ (False, 错误前 300 字, None)

    model/base_url/api_key 为 None 时由 embedding 层回退全局 settings。
    """
    try:
        vec = await embedding.embed_one("healthcheck", model=model, base_url=base_url, api_key=api_key)
        dim = len(vec)
        if dim != expected_dim:
            return True, f"dim mismatch: 实测 {dim} 维，但配置 dim={expected_dim}", dim
        return True, "ok", dim
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300], None


async def probe_embedding() -> tuple[bool, str, int | None]:
    """用全局配置探活（启动自检 / health 用）。"""
    return await probe_embedding_config(
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        expected_dim=settings.embedding_dim,
    )


async def probe_qdrant() -> tuple[bool, str]:
    """GET {qdrant_url}/healthz（2s 超时）。镜像 health._check_qdrant。"""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{settings.qdrant_url.rstrip('/')}/healthz")
        if resp.status_code == 200:
            return True, "ok"
        return False, f"healthz HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300]


async def any_library_rerank_enabled() -> bool:
    """DB 里是否存在库级 rerank_enabled=true 的未删库。

    检索逻辑允许「库级 rerank_enabled=true 覆盖全局 false」，因此即便全局未开，
    只要有库覆盖为 true，实际查询就会走 rerank —— 自检应据此探测，而非显示 off。
    DB 不可用时保守返回 False（宁可不探测，也不误报 rerank 状态）。
    """
    from sqlalchemy import select

    from app.db import async_session_factory
    from app.models.library import Library

    try:
        async with async_session_factory() as session:
            row = await session.execute(
                select(Library.id)
                .where(Library.rerank_enabled.is_(True), Library.deleted_at.is_(None))
                .limit(1)
            )
            return row.first() is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("any_library_rerank_enabled check failed: %s", exc)
        return False


async def probe_rerank() -> tuple[str, str]:
    """探活 rerank。返回 (state, msg)，state ∈ {"off","ok","fail"}。

    off = 未配地址，或全局关闭且无任何库级覆盖（不探测）。
    ok/fail 仅在「已配置 且（全局开 或 有库级 rerank_enabled=true 覆盖）」时给出。
    rerank 是检索期可选能力（失败会回退向量序），不参与 worker 的 consumable 判定。
    """
    if not rerank_svc.is_configured():
        return "off", "not configured"
    # 全局未开时，只要有库级覆盖为 true，实际检索仍会走 rerank → 应探测
    active = settings.rerank_enabled or await any_library_rerank_enabled()
    if not active:
        return "off", "disabled (no library override)"
    try:
        await rerank_svc.rerank("healthcheck", ["healthcheck document"], top_n=1)
        return "ok", "ok"
    except Exception as exc:  # noqa: BLE001
        return "fail", str(exc)[:300]


async def check_consumable() -> tuple[bool, str]:
    """worker 能否消费任务：embedding 与 Qdrant **都**要 OK（缺一不可，否则会烧任务）。

    返回 (ok, 原因)。degraded 恢复循环用它，确保 embedding 和 Qdrant 都恢复才复工。
    """
    emb_ok, emb_msg, _dim = await probe_embedding()
    qdr_ok, qdr_msg = await probe_qdrant()
    # 维度不符虽"可达"(emb_ok=True)，但写 Qdrant 会因维度对不上而失败 → 对 worker 算不可消费
    emb_consumable = emb_ok and emb_msg == "ok"
    if emb_consumable and qdr_ok:
        return True, "ok"
    reasons = []
    if not emb_consumable:
        reasons.append(f"embedding: {emb_msg}")
    if not qdr_ok:
        reasons.append(f"qdrant: {qdr_msg}")
    return False, "; ".join(reasons)


async def run_startup_check(context: str) -> bool:
    """启动自检：探活 embedding + Qdrant，打一段醒目 banner。非 fatal。

    context: "API" / "worker"，用于区分日志来源。
    返回**整体是否可消费**（embedding 维度匹配 且 Qdrant OK）。worker 用它决定是否进入
    degraded——任一依赖不可用、或 embedding 维度不符（写 Qdrant 必失败）都不该消费任务，
    否则 upsert/embed 会把 pending 刷成 failed。注意 API 启动不依赖此返回值（仅记日志），
    所以维度不符时 API 仍照常启动并大声报警，只有 worker 进入 degraded。
    """
    emb_ok, emb_msg, emb_dim = await probe_embedding()
    qdr_ok, qdr_msg = await probe_qdrant()

    log.info("[selfcheck:%s] ===== 启动自检 =====", context)

    if emb_ok and emb_msg == "ok":
        log.info("[selfcheck:%s] embedding OK   model=%s dim=%s url=%s",
                 context, settings.embedding_model, emb_dim, settings.embedding_base_url)
    elif emb_ok:  # 可达但维度不符
        log.error("[selfcheck:%s] embedding WARN model=%s url=%s → %s",
                  context, settings.embedding_model, settings.embedding_base_url, emb_msg)
        log.error("[selfcheck:%s]   → 维度不符会导致写 Qdrant / 检索失败，请核对 EMBEDDING_DIM 与模型。", context)
    else:
        log.error("[selfcheck:%s] embedding FAIL model=%s url=%s → %s",
                  context, settings.embedding_model, settings.embedding_base_url, emb_msg)
        log.error("[selfcheck:%s]   → 文档摄入会全部失败！请检查 EMBEDDING_MODEL / EMBEDDING_API_KEY / EMBEDDING_BASE_URL。", context)

    if qdr_ok:
        log.info("[selfcheck:%s] qdrant OK      url=%s", context, settings.qdrant_url)
    else:
        log.error("[selfcheck:%s] qdrant FAIL    url=%s → %s", context, settings.qdrant_url, qdr_msg)

    # rerank：可选；off 不影响启动判定，fail/未配但开启 时报出来
    rr_state, rr_msg = await probe_rerank()
    if rr_state == "ok":
        log.info("[selfcheck:%s] rerank OK      model=%s url=%s",
                 context, settings.rerank_model, settings.rerank_base_url)
    elif rr_state == "fail":
        log.error("[selfcheck:%s] rerank FAIL    url=%s → %s（检索会回退向量序）",
                  context, settings.rerank_base_url, rr_msg)
    elif settings.rerank_enabled and not rerank_svc.is_configured():
        log.error("[selfcheck:%s] rerank WARN    RERANK_ENABLED=true 但未配 RERANK_BASE_URL/RERANK_MODEL", context)
    else:
        log.info("[selfcheck:%s] rerank off", context)

    log.info("[selfcheck:%s] ====================", context)
    # 可消费 = embedding 维度匹配(emb_msg=="ok") 且 Qdrant OK；维度不符不算（写 Qdrant 会失败）。
    # rerank 不参与（检索期可选，失败回退）。
    return (emb_ok and emb_msg == "ok") and qdr_ok
