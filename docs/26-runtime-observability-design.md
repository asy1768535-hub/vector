# 26 · 运行状态与任务监控设计（批次 C1，设计稿）

> 本文档**只做设计**，不含实现、不创建迁移、不改业务代码。目标：管理员能看到 API /
> Embedding Worker / Cleanup Worker 是否在线，以及任务、Outbox、重建的状态。保持内部项目
> 最小范围，不引入专业监控平台。待评审通过后再进入实现批次。

## 一、当前结构核对结果

逐项核对（均已读源码确认）：

| 组件 | 现状 | 与监控相关的结论 |
|------|------|------------------|
| API lifespan | `app/main.py:lifespan`：startup 跑 `assert_startup_security()` → `get_enforcer()` → `selfcheck.run_startup_check("API")`，yield，shutdown 关补全连接池。**无任何进程级心跳。** | 需在 lifespan 内启动一个心跳后台任务。 |
| Embedding Worker | `app/workers/embedder.py:run(watch)`：`while True` 轮询，空闲 `sleep(embed_worker_poll_seconds)`（默认 1s），周期 reconcile。`worker_id = f"{socket.gethostname()}-{os.getpid()}"`。**无心跳。** | 启动时另起一个独立 `heartbeat_loop` 后台任务（**不**在主循环里手动打卡），与主循环并发、解耦——长任务 / degraded sleep 期间照常打卡（见 §3）。 |
| Cleanup Worker | `app/workers/cleanup.py:run(watch)`：`while True` 轮询，空闲 `sleep(cleanup_worker_poll_seconds)`（默认 2s）；同样 `worker_id = hostname-pid`。失败指数退避，`attempt_count >= cleanup_worker_max_attempts` → `status='failed'`（**死信**）。 | 同上；死信即 outbox 的 `failed`。 |
| `/health` | `app/api/health.py`：一次性探活 db/qdrant/embedding/rerank/ocr，返回 `status/embedding/rerank/ocr` 等。**是“此刻能否连通”，不含“某进程是否在线/上次心跳”。** | 监控页的「服务在线」用心跳，不替代 `/health`；两者互补。 |
| `/admin/jobs/stats` | `app/api/admin_jobs.py:jobs_stats`（`current_superuser`）：按 `embedding_jobs.status` 聚合，返回 `EmbeddingJobStats{pending,processing,done,failed,total}`。 | 直接复用其聚合 SQL。 |
| rebuild operation 状态 | `app/models/rebuild_operation.py:RebuildOperation`：`status ∈ {preparing,running,done,failed}`、`expected_job_count`、`last_error`、`finished_at`；活动唯一索引 `uq_rebuild_op_active_per_lib`（preparing/running 每库至多一个）。进度 = `count(embedding_jobs where rebuild_operation_id=op.id and status='done') / expected_job_count`（见 `app/services/rebuild.py:reconcile_running`）。`sys_libraries.index_state ∈ {ready,rebuilding,failed}`。 | rebuild 进度/失败库数全部可由现有表算出，无需新表。 |
| 后台任务监控页 | `admin-ui/src/views/Jobs.js`（路由 `jobs`，`meta.admin=true`，标题「任务监控」）：Vue3 组合式 + Element Plus，`load()` 拉列表 + `loadStats()` 拉统计，手动刷新。`admin-ui/src/api.js` 有 `jobStats()/listJobs()`。 | 新增「运行状态」页沿用同风格，不动现有任务监控页。 |

**核对结论**：聚合统计（任务 / Outbox / 重建）全部可复用现有四张表；唯一缺口是“进程是否在线 + 上次心跳时间”——现有任何表都不记录进程存活，需**新增一张极小的心跳表**（仅此一处新增，下文给出迁移与回滚）。

## 二、数据模型（新增 `service_heartbeats`）

仅新增一张表（其余统计复用现有表，不新增列、不改业务表）。

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | `uuid` PK，默认 `uuid4` | 主键 |
| `service_type` | `varchar(32)` not null | `'api'` / `'embedding_worker'` / `'cleanup_worker'` |
| `instance_id` | `varchar(160)` not null | 进程实例唯一标识，约定 = `f"{hostname}-{pid}-{uuid4().hex[:8]}"`。**随机短后缀是关键**：进程重启后 PID 可能被操作系统复用，加后缀保证新进程永远是**新行**，不会覆盖/复活旧实例（见测试「PID 复用不覆盖旧实例」）。与 worker 用于抢锁的 `worker_id`（`hostname-pid`）是不同用途的两个标识。 |
| `hostname` | `varchar(255)` not null | `socket.gethostname()` |
| `pid` | `integer` not null | `os.getpid()` |
| `started_at` | `timestamptz` not null | **本进程真实启动时间**：进程启动时捕获一次 `datetime.now(timezone.utc)` 并全程不变（仅首次 INSERT 写入，后续心跳 upsert 不改）。 |
| `last_seen_at` | `timestamptz` not null | 每次心跳更新为 `now()`；在线/离线据此判定 |
| `status` | `varchar(16)` not null 默认 `'online'` | 进程**自报**生命周期：`'online'`（正常心跳）/ `'stopping'`（优雅关闭时写一次）。**在线/离线以 `last_seen_at` 龄判定为准；`stopping` 实例不计入在线（见 §4）。** |
| `metadata`（DB 列名） | `jsonb` 可空 | 少量非敏感运行信息，如 `{"watch": true, "degraded": false, "poll_seconds": 1}`。**不放正文/密钥/堆栈。** |

> **ORM 保留名冲突（必须遵守）**：SQLAlchemy Declarative 的 `Base.metadata` 是保留属性，模型里**不能**直接用 `metadata` 作 ORM 属性名，否则映射报错。须把属性命名为 `heartbeat_metadata`、显式绑定 DB 列名 `metadata`：
>
> ```python
> heartbeat_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
> ```
>
> 即“DB 列叫 `metadata`，Python 属性叫 `heartbeat_metadata`”。（见测试「metadata ORM 保留名回归」。）

约束与索引：

- `CheckConstraint("service_type IN ('api','embedding_worker','cleanup_worker')", name="ck_heartbeat_service_type")`
- `CheckConstraint("status IN ('online','stopping')", name="ck_heartbeat_status")`
- **唯一约束** `uq_heartbeat_service_instance (service_type, instance_id)`：同类进程不同 `instance_id` 各占一行、互不冲突；同一实例 upsert 命中同一行。
- 索引 `ix_heartbeat_service_lastseen (service_type, last_seen_at)`：按类型取最新心跳 + 离线扫描高效。

派生在线状态（不落库，查询时算）：

```
seconds_since = EXTRACT(EPOCH FROM (now() - last_seen_at))
online = seconds_since <= settings.heartbeat_offline_seconds   # 默认 60
```

过期记录处理：每次进程重启 `pid` 变 → 产生新 `instance_id` 行，旧行会一直“离线”。为避免无限增长，**API 心跳任务每次写入后顺带删除 `last_seen_at < now() - heartbeat_prune_seconds` 的行**（默认 `heartbeat_prune_seconds = 3600`，即 1 小时无心跳即清理）。删除失败同样只记日志、不影响主流程。

新增配置（`app/config.py`，仅新增、有默认值，不改现有项）：

```python
heartbeat_interval_seconds: int = 15     # 心跳写入间隔
heartbeat_offline_seconds: int = 60      # 超过即判离线
heartbeat_prune_seconds: int = 3600      # 超过即清理过期行
```

## 三、心跳流程（独立后台任务，与主循环解耦）

统一封装在新模块 `app/services/heartbeat.py`：

- `async def beat(service_type, instance_id, *, hostname, pid, started_at, status="online", heartbeat_metadata=None) -> None`
  —— **独立短事务**：自己 `async with async_session_factory() as s: ...; await s.commit()`，单条 upsert（`INSERT ... ON CONFLICT (service_type, instance_id) DO UPDATE SET last_seen_at=now(), status=..., pid=..., metadata=...`；`started_at` 仅插入时写）。**绝不复用业务处理的 session/事务**——心跳有自己的连接与事务边界。整体 `try/except`：异常只 `log.warning`，不向上抛。
- `async def prune(cutoff_seconds) -> int` —— 独立短事务删除过期行；异常同样自吞。
- `async def heartbeat_loop(service_type, instance_id, *, hostname, pid, started_at, stop_event, metadata_provider=None, also_prune=False) -> None`
  —— **独立后台任务**：`while not stop_event.is_set(): await beat(...); (API 端 also_prune 时顺带 prune()); await asyncio.sleep(heartbeat_interval_seconds)`。循环体整体 `try/except` 兜底，单次失败不退出循环。`metadata_provider` 为可选回调，每次循环读取当前运行态（如 `degraded`）写进 `heartbeat_metadata`。
- 进程级常量：启动时各算一次 —— `INSTANCE_ID = f"{hostname}-{pid}-{uuid4().hex[:8]}"`、`STARTED_AT = datetime.now(timezone.utc)`。
- `def derive_service_status(rows, now, offline_seconds) -> dict` —— 纯函数，把某 `service_type` 的若干行聚合成 online/degraded/offline + 计数（便于单测，见 §4）。

**关键设计：三类进程都各跑一个独立的 `heartbeat_loop` asyncio 任务，每 15s 写一次，完全不依赖主循环的迭代节奏。** 因此：

- Worker 执行一个**超过 60s 的长任务**（大文档 embed / Qdrant 批量清理）时，主循环阻塞在 `await _process_job(...)`，但 `heartbeat_loop` 是另一个并发任务，照常每 15s 打卡 → 该实例**保持在线**（见测试「长任务仍在线」）。
- Worker 处于 **degraded、`asyncio.sleep(worker_degraded_retry_seconds)` 等待**期间，主循环在睡，`heartbeat_loop` 仍打卡且 `metadata.degraded=true` → 页面显示“在线但降级”，不会误判离线。

各进程接入点：

- **API**（`app/main.py:lifespan`）：startup `asyncio.create_task(heartbeat_loop('api', ..., also_prune=True))`；shutdown `stop_event.set()` → `await beat(status="stopping")` → `await`/取消任务。心跳任务异常不影响请求处理。
- **Embedding Worker**（`app/workers/embedder.py:run`，已是 async）：进入主 `while True` **之前** `asyncio.create_task(heartbeat_loop('embedding_worker', ..., metadata_provider=lambda: {"watch": watch, "degraded": degraded}))`；主循环照常 claim/process，**不**在循环里手动打卡。退出前 `stop_event.set()` + `beat(status="stopping")` + 取消任务。
- **Cleanup Worker**（`app/workers/cleanup.py:run`）：同构，`service_type='cleanup_worker'`。

**事务隔离**：业务（job claim/process、API 请求）与心跳各用**独立 AsyncSession 与事务**；心跳写失败只回滚自己的短事务、记一条 WARNING，**绝不污染或回滚正在进行的业务事务**，也绝不让主任务退出（见测试「心跳独立事务失败不污染业务事务」）。

## 四、管理员接口 `GET /admin/operations/status`

- 新增 `app/api/admin_operations.py`，`router = APIRouter(prefix="/admin/operations", tags=["admin"])`，依赖 `current_superuser`（与 `/admin/jobs` 同款鉴权）。
- 只读、只聚合；**不返回正文、密钥、完整异常堆栈**。`last_error` 一律截断到 200 字符并仅取摘要。

响应示例（空库与有数据各一例）：

```jsonc
// 正常：API 4 副本中 3 个在线（degraded），两 worker 各 1 实例在线，库 demo 正在重建
{
  "now": "2026-06-24T08:00:00Z",
  "offline_threshold_seconds": 60,
  "services": [
    {"service_type": "api", "status": "degraded", "online_instances": 3, "known_instances": 4,
     "latest": {"instance_id": "host-101-9f2a1c3d", "hostname": "host", "pid": 101,
                "last_seen_at": "2026-06-24T07:59:52Z", "seconds_since_last_seen": 8,
                "heartbeat_metadata": {}}},
    {"service_type": "embedding_worker", "status": "online", "online_instances": 1, "known_instances": 1,
     "latest": {"instance_id": "host-202-3b7e1f08", "hostname": "host", "pid": 202,
                "last_seen_at": "2026-06-24T07:59:48Z", "seconds_since_last_seen": 12,
                "heartbeat_metadata": {"watch": true, "degraded": false}}},
    {"service_type": "cleanup_worker", "status": "online", "online_instances": 1, "known_instances": 1,
     "latest": {"instance_id": "host-303-1a2b5c6d", "hostname": "host", "pid": 303,
                "last_seen_at": "2026-06-24T07:59:55Z", "seconds_since_last_seen": 5,
                "heartbeat_metadata": {"watch": true}}}
  ],
  "embedding_jobs": {"pending": 3, "processing": 1, "done": 120, "failed": 0, "total": 124},
  "cleanup_outbox": {"pending": 2, "processing": 0, "done": 88, "failed": 1, "dead_letter": 1, "total": 91},
  "libraries": {"rebuilding": 1, "failed": 0},
  "rebuild_operations": [
    {"library_slug": "demo", "status": "running", "expected_job_count": 50,
     "done_job_count": 37, "progress_pct": 74.0, "last_error": null}
  ]
}
```

```jsonc
// 空数据库 / 进程未起：服务离线、统计全零
{
  "now": "2026-06-24T08:00:00Z",
  "offline_threshold_seconds": 60,
  "services": [
    {"service_type": "api", "status": "online", "online_instances": 1, "known_instances": 1,
     "latest": {"instance_id": "host-101-9f2a1c3d", "hostname": "host", "pid": 101,
                "last_seen_at": "2026-06-24T07:59:59Z", "seconds_since_last_seen": 1, "heartbeat_metadata": {}}},
    {"service_type": "embedding_worker", "status": "offline", "online_instances": 0, "known_instances": 0, "latest": null},
    {"service_type": "cleanup_worker",   "status": "offline", "online_instances": 0, "known_instances": 0, "latest": null}
  ],
  "embedding_jobs": {"pending": 0, "processing": 0, "done": 0, "failed": 0, "total": 0},
  "cleanup_outbox": {"pending": 0, "processing": 0, "done": 0, "failed": 0, "dead_letter": 0, "total": 0},
  "libraries": {"rebuilding": 0, "failed": 0},
  "rebuild_operations": []
}
```

字段来源（全部复用现有表）：

1. `services`：`service_heartbeats` **按 `service_type` 聚合**，三类恒定各输出一条：
   - 先排除 `status='stopping'` 的行（优雅退出的实例**不计入** online/known）。
   - `known_instances` = 该类型在 prune 窗口内（未过期）的非 stopping 实例数。
   - `online_instances` = 其中 `seconds_since_last_seen <= offline_seconds`（默认 60）的实例数。
   - `status`：`online_instances == 0` → `offline`；否则若 `online_instances < known_instances` **或** 任一在线实例 `heartbeat_metadata.degraded == true` → `degraded`；否则 `online`。
   - `latest` = 该类型 `last_seen_at` 最新的实例（供页面展示 host/pid/最后心跳；无任何行 → `null`）。
   纯聚合逻辑落在 `derive_service_status`，便于单测（覆盖「多实例部分离线 → degraded」「PID 复用各占一行」）。
2. `embedding_jobs`：复用 `jobs_stats` 的 `GROUP BY embedding_jobs.status`（`superseded` 计入 `total` 但不单列，它不是健康信号）。
3. `cleanup_outbox`：`GROUP BY qdrant_cleanup_outbox.status`；`dead_letter` = `failed`（已达 `cleanup_worker_max_attempts`，见 `app/workers/cleanup.py`）。
4. `libraries`：`sys_libraries` 按 `index_state` 计 `rebuilding`/`failed`（未删库）。
5. `rebuild_operations`：取 `status IN ('preparing','running')` 的活动 operation，`done_job_count` = `count(embedding_jobs where rebuild_operation_id=op.id and status='done')`，`progress_pct = round(done/expected*100, 1)`（`expected_job_count=0` 时按 0）。`last_error` 截断摘要。

## 五、后台页面草图（文字）

新增页面「运行状态」（`admin-ui/src/views/RuntimeStatus.js`，路由 `operations`，`meta.admin=true`），风格与现有后台一致（el-card + el-table + el-tag），**无 WebSocket、无嵌套卡片、无花哨仪表盘**，右上角一个「刷新」按钮。

```
运行状态                                            [ 刷新 ]

┌── 服务状态 ────────────────────────────────────────────────────┐
│ 服务              状态     在线/已知   最后心跳        主机/PID    │
│ API               ◐ 降级   3/4         8 秒前          host / 101  │
│ Embedding Worker  ● 在线   1/1         12 秒前(降级)    host / 202  │
│ Cleanup Worker    ○ 离线   0/0         —              —           │
└────────────────────────────────────────────────────────────────┘

┌── Embedding 任务 ──┐  ┌── Cleanup Outbox ─────────┐
│ pending      3     │  │ pending        2          │
│ processing   1     │  │ processing     0          │
│ failed       0     │  │ failed/死信     1          │
└────────────────────┘  └───────────────────────────┘

┌── 重建 ───────────────────────────────────────────────────┐
│ 进行中：demo  running  37/50 (74%)                         │
│ 失败库：0                                                  │
└───────────────────────────────────────────────────────────┘
```

- 状态三态：在线 = 绿点 `el-tag type=success`、降级 = 黄点 `warning`、离线 = 灰点 `info`。
- 「在线/已知」列直接显示 `online_instances/known_instances`（如「API 3/4」）；**不展开每个副本的明细列表**，保持简洁（单实例时即 `1/1`）。
- degraded 触发于：部分副本离线（`online < known`），或某在线实例自报 `degraded`（后者在「最后心跳」列附注 `(降级)`）。
- 「最后心跳」取该类型 `latest` 实例的相对时间（如 `12 秒前`），离线显示 `—`。
- 刷新按钮调用一次 `GET /admin/operations/status` 重渲染；不自动轮询（如需可后续加固定间隔 setInterval，本批不做）。
- 新增 `admin-ui/src/api.js` 包装 `operationsStatus()`；在 `admin-ui/src/app.js` 注册路由 + 左侧导航项「运行状态」（`admin:true`）。

## 六、失败处理

- **心跳写入失败**：`beat()`/`prune()` 内部 `try/except`，只 `log.warning`，不抛、不重试风暴；API 后台任务与 Worker 主循环均继续。后果仅是该进程在页面上短暂显示“离线”，恢复写入后自动回到“在线”。
- **状态接口**：纯只读聚合；任一子查询失败应整体返回 5xx 由前端提示，不静默给错数（实现批次细化）。`last_error` 截断 200 字、不含堆栈/正文/密钥。
- **进程崩溃**：不自动重启（交给 systemd/Docker，见 `docs/15-deployment.md`）。崩溃后该实例 `last_seen_at` 停更 → 60s 后判离线 → 1h 后被 `prune` 清理。

## 七、迁移与回滚

新增 Alembic 迁移 `0011_service_heartbeats`（down_revision = `0010`，当前 head）：

- **upgrade**：`op.create_table("service_heartbeats", ...)`（上表全部列）+ 两个 CheckConstraint + 唯一约束 `uq_heartbeat_service_instance` + 索引 `ix_heartbeat_service_lastseen`。
- **downgrade**：`op.drop_table("service_heartbeats")`（表为纯旁路、无外键被依赖，可安全整表删除；回滚后系统行为完全等同当前——心跳能力消失，其余不受影响）。

该表**无任何外键指向业务表，也无业务表指向它**，故迁移/回滚都不触及现有数据，零风险。本批不创建该迁移文件，仅在此定稿。

## 八、测试清单（设计，待实现批次落地）

纯单元/集成，全部 mock，不连真实模型/服务：

1. **心跳 upsert**：首次 `beat` 插入一行；再次 `beat` 命中唯一键只更新 `last_seen_at`/`status`/`metadata`，`started_at` 不变，仍是一行。
2. **在线/离线判定**：`derive_status` 纯函数，`last_seen` 距今 59s → online，61s → offline（边界用注入的“当前时间”，不依赖真实时钟）。
3. **同类多实例不冲突**：同 `service_type` 两个 `instance_id` → 两行并存，唯一约束不报错。
4. **心跳写失败不终止 Worker**：mock `beat` 内部 DB 调用抛异常 → 断言只记日志、`beat` 正常返回、调用方循环不中断。
5. **聚合统计正确**：构造 embedding_jobs / cleanup_outbox / rebuild_operations / sys_libraries 数据，断言 `/admin/operations/status` 各计数与进度正确（含死信 = failed、进度 = done/expected）。
6. **权限**：普通用户（非超管）访问 `/admin/operations/status` → 403。
7. **空数据库返回零值**：无心跳行 → 三类服务恒出现且 `offline`；各统计为 0；`rebuild_operations` 为空数组。
8. **页面渲染与刷新**：mock `operationsStatus()` 返回上面示例，断言三表渲染、在线/降级/离线标签正确、点「刷新」再次拉取（与现有 admin 测试风格一致，前端以手测/轻量渲染校验为主）。
9. **Worker 长任务期间仍在线**：让主循环阻塞在一个 >60s 的模拟任务（注入慢 `_process_job`），断言独立 `heartbeat_loop` 期间仍持续 `beat()`、该实例 `last_seen_at` 持续刷新、聚合状态保持 `online`（用注入时钟，不真睡 60s）。
10. **metadata ORM 保留名回归**：导入/映射 `ServiceHeartbeat` 不抛 `InvalidRequestError`；ORM 属性为 `heartbeat_metadata` 且绑定 DB 列名 `metadata`；构造实例、读写该属性均正常。
11. **PID 复用不覆盖旧实例**：旧实例 `host-100-aaaa`（pid=100）离线但未过期；新进程复用 pid=100 但 `instance_id=host-100-bbbb`，`beat()` 后两行并存、唯一约束不冲突、旧行不被覆盖（聚合得 known=2、online=1 → degraded）。
12. **多实例部分离线 → degraded**：同 `service_type` 4 行，3 fresh + 1 stale（未过期）→ `derive_service_status` 返回 `status=degraded, online_instances=3, known_instances=4`；全 fresh → online；全 stale → offline；`stopping` 行不计入 online/known。
13. **心跳独立事务失败不污染业务事务**：mock 心跳 `beat()` 的独立 session 在 commit 抛错 → 断言只记 WARNING、异常不向上传播；同时另起的业务事务（独立 session）仍能正常提交，证明二者互不影响、无共享事务。

## 九、实施文件清单（供下一批次）

- 新增 `app/models/service_heartbeat.py`（模型）
- 新增 `alembic/versions/0011_service_heartbeats.py`（迁移，down_revision=0010）
- 新增 `app/services/heartbeat.py`（`beat`/`prune`/`heartbeat_loop`/`derive_service_status` + 进程级 `INSTANCE_ID`/`STARTED_AT`；全部走独立短事务）
- 改 `app/main.py`（lifespan 起/停独立心跳任务）
- 改 `app/workers/embedder.py`、`app/workers/cleanup.py`（启动时起独立 `heartbeat_loop` 任务，与主循环并发、解耦，不在主循环里手动打卡）
- 新增 `app/api/admin_operations.py`（`GET /admin/operations/status`）+ 在 `app/main.py` 注册路由
- 改 `app/schemas/admin.py`（新增响应模型：服务状态、outbox 统计含 `dead_letter`、rebuild 进度）
- 改 `app/config.py`（`heartbeat_interval_seconds` / `heartbeat_offline_seconds` / `heartbeat_prune_seconds`）
- 新增 `admin-ui/src/views/RuntimeStatus.js` + 改 `admin-ui/src/api.js`（`operationsStatus()`）+ 改 `admin-ui/src/app.js`（路由 + 导航「运行状态」）
- 新增 `tests/test_heartbeat.py`、`tests/test_admin_operations.py`
- 改 `docs/16-testing.md`（补该页/接口测试说明）、`docs/12-admin-ui.md`（新增页面）、`docs/11-worker.md`（心跳旁路说明）

## 十、明确不做的内容

- 不引入 Prometheus / Grafana / ELK / 任何消息队列。
- 不把完整日志写入 PostgreSQL（心跳 `metadata` 只存少量非敏感运行信息）。
- 不实现自动重启（崩溃恢复交给 systemd / Docker，见 `docs/15-deployment.md`）。
- 不修改检索 / OCR / Embedding / Rerank 业务逻辑。
- 不做复杂多租户监控（按单实例使用，模型保留 `instance_id` 以便将来多实例，但本设计不展开多副本聚合视图）。
- 不做 WebSocket / 自动实时推送（仅手动刷新；后续如需再加固定间隔轮询）。
- 不新增 README 徽章或对外暴露监控端点（`/admin/operations/status` 仅超管可见）。
