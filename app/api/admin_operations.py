"""运行状态与任务监控（docs/26 / 批次 C2）。

`GET /admin/operations/status`：只读、只聚合，超管可见。把三类进程心跳
（service_heartbeats）聚合为 online/degraded/offline，并复用现有表统计
embedding_jobs / cleanup_outbox / 重建进度 / 库索引状态。

不返回正文、密钥、完整异常堆栈；last_error 一律截断摘要。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_superuser
from app.config import settings
from app.db import get_db
from app.models.cleanup_outbox import CleanupOutbox
from app.models.embedding_job import EmbeddingJob
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.models.organization_capability_rollout import OrganizationCapabilityRollout
from app.models.public_api_operations import PublicAPIRequestRecord
from app.models.rebuild_operation import RebuildOperation
from app.models.service_heartbeat import ServiceHeartbeat
from app.models.user import User
from app.schemas.admin import (
    CleanupOutboxStats,
    EmbeddingJobStats,
    GraphPublicationStats,
    LibraryIndexStats,
    OperationsStatus,
    RebuildOperationStatus,
    ServiceStatus,
)
from app.services import heartbeat
from app.services import audit_log

router = APIRouter(prefix="/admin/operations", tags=["admin"])

# 四类进程恒定各输出一条（即使无任何心跳行也要给出 offline）。
_SERVICE_TYPES = (
    "api",
    "embedding_worker",
    "cleanup_worker",
    "graph_extractor",
    "knowledge_artifact_worker",
    "classification_worker",
)

# last_error 仅取摘要，截断到 200 字符，绝不含完整堆栈。
_LAST_ERROR_MAX = 200


@router.get("/deployment")
async def deployment_operations(
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Content-free capacity, failure, cost-proxy, and rollout counters."""
    now = (await db.execute(select(func.now()))).scalar_one()
    since = now - timedelta(hours=24)
    request_row = (
        await db.execute(
            select(
                func.count(PublicAPIRequestRecord.id),
                func.count(PublicAPIRequestRecord.id).filter(
                    PublicAPIRequestRecord.outcome != "completed"
                ),
                func.coalesce(func.sum(PublicAPIRequestRecord.input_tokens), 0),
                func.coalesce(func.sum(PublicAPIRequestRecord.output_tokens), 0),
            ).where(PublicAPIRequestRecord.finished_at >= since)
        )
    ).one()
    queue_row = (
        await db.execute(
            select(
                func.count(EmbeddingJob.id).filter(EmbeddingJob.status == "pending"),
                func.count(EmbeddingJob.id).filter(EmbeddingJob.status == "failed"),
            )
        )
    ).one()
    rollouts = dict(
        (
            await db.execute(
                select(
                    OrganizationCapabilityRollout.capability,
                    func.count(OrganizationCapabilityRollout.id),
                )
                .where(OrganizationCapabilityRollout.enabled.is_(True))
                .group_by(OrganizationCapabilityRollout.capability)
            )
        ).all()
    )
    return {
        "window_hours": 24,
        "requests": {"total": request_row[0], "failed": request_row[1]},
        "cost_proxy_tokens": {"input": request_row[2], "output": request_row[3]},
        "capacity": {
            "embedding_pending": queue_row[0],
            "embedding_failed": queue_row[1],
        },
        "enabled_rollouts": {
            capability: rollouts.get(capability, 0)
            for capability in ("public_api_v1", "mcp_adapter", "external_graph_sync")
        },
    }


@router.get("/status", response_model=OperationsStatus)
async def operations_status(
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> OperationsStatus:
    """聚合三类进程在线状态 + 任务 / Outbox / 重建 / 库索引统计。"""
    # 用 DB 侧 now()，避免 app 与 DB 时钟偏差影响在线判定。
    db_now = (await db.execute(select(func.now()))).scalar_one()
    offline_seconds = settings.heartbeat_offline_seconds

    # ── 1. 服务在线状态（service_heartbeats 按 service_type 聚合）──────────
    # 只取未过期行（last_seen_at >= now - prune 窗口），使 known_instances 严格等于
    # 「未过期实例数」——不依赖 API 端 prune 是否刚跑过（docs/26 §4）。用 db_now 保证与
    # 在线判定同一时基。
    prune_cutoff = db_now - timedelta(seconds=settings.heartbeat_prune_seconds)
    hb_rows = list(
        (
            await db.execute(
                select(ServiceHeartbeat).where(ServiceHeartbeat.last_seen_at >= prune_cutoff)
            )
        ).scalars().all()
    )
    by_type: dict[str, list[ServiceHeartbeat]] = {st: [] for st in _SERVICE_TYPES}
    for row in hb_rows:
        by_type.setdefault(row.service_type, []).append(row)
    services = [
        ServiceStatus.model_validate(
            heartbeat.derive_service_status(
                st, by_type.get(st, []), now=db_now, offline_seconds=offline_seconds
            )
        )
        for st in _SERVICE_TYPES
    ]

    # ── 2. embedding_jobs（GROUP BY status；superseded 计入 total 不单列）──
    job_rows = (
        await db.execute(select(EmbeddingJob.status, func.count()).group_by(EmbeddingJob.status))
    ).all()
    jc = {s: n for s, n in job_rows}
    embedding_jobs = EmbeddingJobStats(
        pending=jc.get("pending", 0),
        processing=jc.get("processing", 0),
        done=jc.get("done", 0),
        failed=jc.get("failed", 0),
        total=sum(jc.values()),
    )

    # ── 3. cleanup_outbox（GROUP BY status；dead_letter = failed）─────────
    ob_rows = (
        await db.execute(select(CleanupOutbox.status, func.count()).group_by(CleanupOutbox.status))
    ).all()
    oc = {s: n for s, n in ob_rows}
    failed_n = oc.get("failed", 0)
    cleanup_outbox = CleanupOutboxStats(
        pending=oc.get("pending", 0),
        processing=oc.get("processing", 0),
        done=oc.get("done", 0),
        failed=failed_n,
        dead_letter=failed_n,   # 达 max_attempts 即标 failed（见 app/workers/cleanup.py）
        total=sum(oc.values()),
    )

    # ── 4. 库索引状态（sys_libraries 按 index_state，未删库）──────────────
    lib_rows = (
        await db.execute(
            select(Library.index_state, func.count())
            .where(Library.deleted_at.is_(None))
            .group_by(Library.index_state)
        )
    ).all()
    lc = {s: n for s, n in lib_rows}
    libraries = LibraryIndexStats(
        rebuilding=lc.get("rebuilding", 0),
        failed=lc.get("failed", 0),
    )

    # 5. Current graph publication health counts.
    publication_rows = (
        await db.execute(
            select(GraphPublication.status, func.count())
            .where(GraphPublication.status.in_(("active", "degraded")))
            .group_by(GraphPublication.status)
        )
    ).all()
    publication_counts = dict(publication_rows)
    graph_publications = GraphPublicationStats(
        active=publication_counts.get("active", 0),
        degraded=publication_counts.get("degraded", 0),
    )

    # ── 6. 活动重建 operation（preparing/running）+ 进度 ─────────────────
    # done_job_count 子查询：每个 op 已完成的 job 数。
    done_subq = (
        select(
            EmbeddingJob.rebuild_operation_id.label("op_id"),
            func.count().label("done_n"),
        )
        .where(EmbeddingJob.status == "done")
        .group_by(EmbeddingJob.rebuild_operation_id)
        .subquery()
    )
    op_rows = (
        await db.execute(
            select(
                Library.slug,
                RebuildOperation.status,
                RebuildOperation.expected_job_count,
                RebuildOperation.last_error,
                func.coalesce(done_subq.c.done_n, 0),
            )
            .join(Library, Library.id == RebuildOperation.library_id)
            .outerjoin(done_subq, done_subq.c.op_id == RebuildOperation.id)
            .where(RebuildOperation.status.in_(("preparing", "running")))
            .order_by(Library.slug)
        )
    ).all()
    rebuild_operations = []
    for slug, op_status, expected, last_error, done_n in op_rows:
        expected = expected or 0
        done_n = done_n or 0
        progress = round(done_n / expected * 100, 1) if expected > 0 else 0.0
        rebuild_operations.append(
            RebuildOperationStatus(
                library_slug=slug,
                status=op_status,
                expected_job_count=expected,
                done_job_count=done_n,
                progress_pct=progress,
                last_error=last_error[:_LAST_ERROR_MAX] if last_error else None,
            )
        )

    return OperationsStatus(
        now=db_now,
        offline_threshold_seconds=offline_seconds,
        services=services,
        embedding_jobs=embedding_jobs,
        cleanup_outbox=cleanup_outbox,
        libraries=libraries,
        graph_publications=graph_publications,
        rebuild_operations=rebuild_operations,
    )


@router.post("/cleanup-outbox/requeue-failed")
async def requeue_failed_cleanup_outbox(
    library_id: uuid.UUID | None = Query(default=None),
    actor: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """把 failed cleanup outbox 重新放回 pending，供 cleanup worker 重试。"""
    sql = """
        UPDATE qdrant_cleanup_outbox
        SET status='pending', worker_id=NULL, claimed_at=NULL, finished_at=NULL,
            available_at=NOW(), attempt_count=0, last_error=NULL
        WHERE status='failed'
    """
    params: dict[str, str] = {}
    if library_id is not None:
        sql += " AND library_id = :library_id"
        params["library_id"] = str(library_id)
    result = await db.execute(text(sql), params)
    count = result.rowcount or 0
    await audit_log.record(
        db,
        actor.id,
        "cleanup_outbox.requeue_failed",
        {"library_id": str(library_id) if library_id else None, "requeued_count": count},
    )
    await db.commit()
    return {"requeued_count": count}
