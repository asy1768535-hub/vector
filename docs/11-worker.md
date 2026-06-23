# 11 · Worker

> 系统有**两个 worker**：Embedding Worker（本文）+ Cleanup Worker。生产需各起一个常驻进程。

## Cleanup Worker（#7 删除/重建的 Qdrant 物理清理）

```bash
python -m app.workers.cleanup --watch    # 长跑：消费 qdrant_cleanup_outbox
python -m app.workers.cleanup            # 处理完即退出（cron 友好）
```

删除文档/库、reingest 后会在同事务写入 `qdrant_cleanup_outbox`；Cleanup Worker 用
`FOR UPDATE SKIP LOCKED` 抢锁、幂等执行 Qdrant 删除、失败指数退避重试、超上限标 failed。
逻辑可见性在删除提交时即生效（检索按 `deleted_at` 过滤），不依赖本进程是否跑完。

## Embedding Worker

## 启动

```bash
python -m app.workers.embedder           # 处理完 pending 即退出（cron 友好）
python -m app.workers.embedder --watch   # 长跑模式：空闲 sleep poll_seconds 后再抢
```

支持多副本：

```bash
python -m app.workers.embedder --watch &   # 终端 A
python -m app.workers.embedder --watch &   # 终端 B
python -m app.workers.embedder --watch &   # 终端 C
```

`FOR UPDATE SKIP LOCKED` 保证三个副本不会抢到同一 job。

## 主循环

```python
# app/workers/embedder.py
while True:
    async with session:
        reset = await _reset_stale_jobs(session)   # 兜底：超时 processing → pending
        jobs = await _claim_jobs(session, worker_id, EMBED_WORKER_BATCH_DOCS)

    if not jobs:
        if not watch:
            return
        await asyncio.sleep(EMBED_WORKER_POLL_SECONDS)
        continue

    for job in jobs:
        async with session:
            await _process_job(session, job)
```

每个 job 独立 session，错误不互相影响。

## 抢锁 SQL

```sql
WITH picked AS (
    SELECT id FROM embedding_jobs
    WHERE status = 'pending'
      AND attempt_count < :max_attempts
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT :limit
)
UPDATE embedding_jobs j
SET status = 'processing',
    worker_id = :worker_id,
    attempt_count = j.attempt_count + 1,
    claimed_at = NOW()
FROM picked
WHERE j.id = picked.id
RETURNING j.id
```

要点：
- **`FOR UPDATE SKIP LOCKED`**：被其他事务锁住的行直接跳过，多 worker 并发零冲突
- **`ORDER BY created_at`**：FIFO 处理
- **`attempt_count` 上限**：失败 N 次后不再被认领（避免死循环）
- **一次返回多条 ID**，再用 `SELECT IN (…)` 拉全字段（减少锁持有时间）

## 处理单个 job

```python
async def _process_job(db, job):
    lib = await db.get(Library, job.library_id)
    doc = await db.get(Document, job.document_id)
    if lib is None or doc is None:
        return _mark_failed(db, job, "library or document missing")
    if lib.deleted_at or doc.deleted_at:
        return _mark_failed(db, job, "library or document soft-deleted")

    chunks = SELECT * FROM chunks WHERE document_id = doc.id ORDER BY seq

    # 1. 批量 embed（按 EMBED_BATCH_SIZE 分批）
    vectors = []
    for i in range(0, len(texts), batch):
        piece = await embedding.embed_texts(texts[i:i+batch], model=lib.embedding_model)
        vectors.extend(piece)

    # 2. 校验维度
    assert all(len(v) == lib.embedding_dim for v in vectors)

    # 3. 上传 Qdrant
    points = [{
        "id": str(chunk.id),
        "vector": vec,
        "payload": {
            "library_id": str(lib.id),
            "document_id": str(doc.id),
            "chunk_id": str(chunk.id),
            "seq": chunk.seq,
            "text": chunk.text,
            "title": doc.title,
            "external_id": doc.external_id,
            **(doc.doc_metadata or {}),
        }
    } for chunk, vec in zip(chunks, vectors)]

    await qdrant.upsert_points(lib.qdrant_collection, points)

    # 4. 标 done
    UPDATE embedding_jobs SET status='done', finished_at=now() WHERE id=job.id
    UPDATE documents      SET status='ready' WHERE id=doc.id
```

## 失败处理

```python
async def _mark_failed(db, job, reason):
    # 若已超 max_attempts → final failed；否则可回 pending 重试
    next_status = "failed" if (job.attempt_count >= MAX_ATTEMPTS) else "pending"
    UPDATE embedding_jobs SET
        status = next_status,
        last_error = reason[:1000],
        finished_at = now() if final else None,
        worker_id = None if retry else job.worker_id,
        claimed_at = None if retry else job.claimed_at
    WHERE id = job.id

    if next_status == "failed":
        UPDATE documents SET status='failed', last_error=reason WHERE id=job.document_id
```

| 配置项 | 默认 | 用途 |
|---|---|---|
| `EMBED_WORKER_MAX_ATTEMPTS` | 5 | 单 job 最多尝试次数 |
| `EMBED_WORKER_STALE_SECONDS` | 3600 | `processing` 滞留超过此值 → 重置 `pending` |

## stale 任务兜底

如果 worker 进程崩了（OOM、被 SIGKILL），它持有的 `processing` job 会一直卡着。每轮循环开头：

```python
UPDATE embedding_jobs
SET status='pending', worker_id=NULL, claimed_at=NULL
WHERE status='processing'
  AND claimed_at < NOW() - INTERVAL ':secs seconds'
```

`secs = EMBED_WORKER_STALE_SECONDS`（默认 1h）。设置时要 >> 单 job 最长处理时间。

## 手动重试

管理后台「任务监控」→ 选失败的 job → 点「重试」：

```http
POST /admin/jobs/{job_id}/retry
```

会把 status 重置为 `pending`，清掉 worker_id / last_error。下轮 worker 循环就会重新抢。

## 性能调优

| 想 | 改哪里 |
|---|---|
| 增大单批吞吐 | `EMBED_WORKER_BATCH_DOCS` ↑（单轮抢多少文档） |
| Embedding 服务并发 | `EMBED_BATCH_SIZE` ↑（单次 HTTP 多少 chunk） |
| 减少空轮询 CPU | `EMBED_WORKER_POLL_SECONDS` ↑（默认 1s） |
| 加快失败放弃 | `EMBED_WORKER_MAX_ATTEMPTS` ↓ |
| 多机部署吞吐 | 起 N 个 worker 进程（SKIP LOCKED 自动分布） |

## 监控

- **「任务监控」页**：`/console/#/jobs`，按状态过滤
- **直接 SQL**：

```sql
SELECT status, COUNT(*) FROM embedding_jobs GROUP BY status;

-- 看堆积
SELECT library_id, COUNT(*) FROM embedding_jobs
WHERE status = 'pending' GROUP BY library_id ORDER BY 2 DESC;

-- 看最近失败
SELECT id, document_id, last_error, attempt_count, finished_at
FROM embedding_jobs
WHERE status = 'failed'
ORDER BY finished_at DESC LIMIT 20;
```

- **Prometheus 指标**：路线图 P5；可在 `embedder.py` 加 prometheus_client 导出 `pending`/`processing`/`done`/`failed` 计数

## 常见失败原因

| `last_error` 关键字 | 可能原因 |
|---|---|
| `embedding service 500/502` | bge-m3 服务挂了 |
| `embedding service 429` | bge-m3 限流 |
| `vector count mismatch` | bge-m3 返回数 ≠ 输入数（极少；多半是网关截断） |
| `dim mismatch: expected 1024` | 库的 `embedding_dim` 跟 bge-m3 输出不一致 |
| `qdrant ... 4xx/5xx` | Qdrant 挂了 / collection 不存在 / 维度错配 |
| `library or document soft-deleted` | 摄入完 worker 抢锁前被删 |
