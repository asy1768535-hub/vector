# 21 · 批次 A 实施计划（#6 Revision + Job Generation）

> 状态：**计划稿（plan-only，未改代码）**。依据见 [20-revision-and-deletion-consistency](./20-revision-and-deletion-consistency.md)。
> 范围对应设计文档 §16「批次 A」。批次 B（#7 outbox + 删除可见性）单独计划。

---

## 1. 范围

**做（批次 A）**
- 迁移 0009：revision 列、rebuild 相关列、活动 job 唯一索引、lifecycle_mode/index_state、`rebuild_operations` 表。
- 模型层：`Document.current_revision`、`EmbeddingJob.document_revision/rebuild_operation_id`、
  `Library.lifecycle_mode/index_state/active_rebuild_operation_id`、新模型 `RebuildOperation`。
- 写路径 `_new_generation`：固定锁顺序 + `current_revision += 1` + supersede 旧 job + 建新 job（带 revision/operation）。
- Worker：统一资格条件（§5.1）三处复用、固定锁顺序（§5.2）、最终写入有界 Qdrant 超时、payload 写 `document_revision`、文档状态写守护。
- 重建：`rebuild_operations` 生命周期、`index_state` 状态机、重建期普通写 503(+`Retry-After`)。
- 检索：共享可见性过滤的**基础设施 + revision 维度**（lifecycle_mode 分流、有界补召回）。

**不做（留批次 B）**
- `qdrant_cleanup_outbox` 表与 Cleanup Worker。
- 删除→tombstone 单事务 + outbox；reingest 的 `purge_stale_revisions` outbox。
- 检索过滤的 **deleted 维度**（批次 A 先只挡 stale revision；删除可见性由 B 完成）。

> **发布门槛**：批次 A 单独**不可作为生产删除安全版本**（删除可见性需 B）。A/B 可分开发/验收，
> 但生产**必须一起发布**（设计 §11.3）。A 内含 revision 过滤是为了 A 验收期不召回旧 revision。

---

## 2. 迁移 0009（`alembic/versions/0009_*.py`）

DDL（`down_revision='0008'`，**严格按此顺序解开循环外键**）：
1. `documents.current_revision INT NOT NULL DEFAULT 1`，`CHECK (current_revision >= 1)`。
2. `embedding_jobs.document_revision INT`：DEFAULT 1 加列 → 回填存量为 1 → 设 NOT NULL →
   **去掉 DB 默认值**（决策 2：强制应用层显式赋值）。
3. **先建表 `rebuild_operations`**（不含回指 sys_libraries 的 FK），含：
   `status CHECK in (preparing,running,done,failed)`、`expected_job_count INT NOT NULL DEFAULT 0`、
   `library_id FK→sys_libraries`、`UNIQUE (library_id) WHERE status IN ('preparing','running')`、
   索引 `(library_id, status)`。**不要 `target_revisions JSONB`**（大库爆行，改用 jobs 作快照 + expected_job_count）。
4. `embedding_jobs.rebuild_operation_id UUID NULL` FK→`rebuild_operations(id)` **ON DELETE RESTRICT**。
5. 活动 job 部分唯一索引 `uq_jobs_doc_rev_active (document_id, document_revision) WHERE status IN ('pending','processing')`。
5b. operation 文档快照唯一约束 `uq_jobs_op_doc (rebuild_operation_id, document_id) WHERE rebuild_operation_id IS NOT NULL`
    （防同 operation 内重复 job 让 `done_count==expected_job_count` 提前满足）。
6. `sys_libraries.lifecycle_mode VARCHAR(16) NOT NULL DEFAULT 'managed'` CHECK in (managed,external)。
7. `sys_libraries.index_state VARCHAR(16) NOT NULL DEFAULT 'ready'` CHECK in (ready,rebuilding,failed)。
8. `sys_libraries.active_rebuild_operation_id UUID NULL`，最后 `ALTER TABLE ADD CONSTRAINT` 补 FK→`rebuild_operations(id)`。

数据回填：
- 存量库 `lifecycle_mode='managed'`。
- **人工步骤**：`case_chunks` 等源库补全库由超管显式改 `external`（在启用严格过滤前）。
- 升级前置检查（沿用 0008 经验）：活动 job 是否已有违反新唯一索引的重复 `(document_id, document_revision)`；有则先清理。

downgrade（逆序）：先 drop `sys_libraries.active_rebuild_operation_id` 的回指 FK，再 drop 列/索引/表
（注明会丢失 revision 保护）。

验收：`alembic upgrade head` → `current` 显示 0009 (head)；`pg_indexes` 核对部分唯一索引谓词。

---

## 3. 模型层（`app/models/`）

- `document.py`：加 `current_revision: Mapped[int]`。
- `embedding_job.py`：加 `document_revision: Mapped[int]`、`rebuild_operation_id: Mapped[uuid|None]`；
  新增活动 job 部分唯一索引到 `__table_args__`；放行 `superseded` 状态（字符串列，无需 DDL）。
- `library.py`：加 `lifecycle_mode`、`index_state`、`active_rebuild_operation_id`。
- 新 `rebuild_operation.py`：`RebuildOperation`（id/library_id/collection_name/`status`(preparing|running|done|failed)/
  `expected_job_count`/时间/last_error）。**无 `target_revisions`**——以 jobs（带 `rebuild_operation_id`+`document_revision`）
  作为 revision 快照，operation 仅存 `expected_job_count` 供 finalize 判定。

---

## 4. 写路径（`app/services/ingest.py`）

新增 `_new_generation(db, library, document, *, rebuild_operation_id=None) -> EmbeddingJob`：
1. 按 §5.2 锁顺序：调用方已持 `library` 锁（普通路径为 **`FOR KEY SHARE`**）；本函数对 `document` 行 `FOR UPDATE`。
2. `document.current_revision += 1`。
3. 该文档 `pending/processing` job → `superseded`。
4. 建新 `EmbeddingJob(document_revision=current_revision, rebuild_operation_id=...)`。

接入点：
- `ingest_text`（新建）：doc 落库后 `current_revision=1`、显式建 rev=1 job。
  保留本轮已实现的 `_find_active` 身份解析 + `begin_nested` 并发冲突（#4，已上线）。
- `reingest_document`：no-op 判定**扩展到 payload 维度**（title/metadata/任何入 payload 字段变更都算 changed → +revision）；
  changed 时走 `_new_generation`。**决策 1：A 阶段立即移除当前 API 层「先删 Qdrant 再 commit」的同步删点**，
  改为只动 PG（旧点靠 revision 过滤即不可见），物理清理由批次 B 的 outbox 完成。
- 调用方（`app/api/documents.py` 的 ingest/update/import-file/delete）：先 Casbin 鉴权 →
  **若 `lifecycle_mode=='external'` → 409**（外部库本系统不可写，设计 §4.5）→ 再按锁顺序
  **`SELECT library FOR KEY SHARE`**（与其它 Worker 并发、与 rebuild 互斥）并校验 `index_state=='ready'`，
  否则 503+`Retry-After`。**切勿对 library 用 `FOR UPDATE`**（会串行化全库写入）。

---

## 5. Worker（`app/workers/embedder.py`）

- 新 `eligibility(job, document, library, operation) -> bool`：实现 §5.1 完整资格条件——
  `ready AND rebuild_operation_id IS NULL`，或 `rebuilding AND ==active_rebuild_operation_id AND operation.status=='running'`；
  `failed` 全拒。单测纯逻辑（覆盖设计 §14.4 矩阵 1–5）。
- `_process_job`：
  1. claim 后按锁顺序重读 library+document(+operation)，`eligibility` 不满足 → `superseded` 返回（§5.3）。
  2. embed。
  3. 最终写入（§5.4）：按锁顺序 **`library FOR KEY SHARE`** → `document FOR UPDATE`，再 `eligibility` 复核；
     仅通过才 `upsert_points`（payload 带 `document_revision`，**有界超时** `qdrant_upsert_timeout_seconds`，
     禁止锁内长退避）→ job done → 文档 ready（仅当仍 current revision，I4）。
  4. `_build_payload` 加系统字段 `document_revision=job.document_revision`（最后写、不可被 metadata 覆盖，沿用 #3）。
  5. 文档状态写守护：仅当 `current_revision==job.document_revision` 才置 ready（I4）。
- `_mark_failed`/重试：failed→pending 仅当仍 current revision 且未删；否则 superseded（§6.2）。

---

## 6. 重建（`app/api/admin_libraries.py` + 新服务）

`rebuild_collection` 重写为 §9 **三阶段**，**绝不在 DB 事务里调 Qdrant**。
**前置**：Casbin 鉴权 → 若 `lifecycle_mode=='external'` → **409，绝不 `delete_collection`**（防误删 case_chunks 外部 collection）。
- **prepare 事务**：`SELECT library FOR UPDATE` → 建 `RebuildOperation(status='preparing')` +
  设 `index_state='rebuilding'`/`active_rebuild_operation_id` → 各活动文档 `current_revision+=1` →
  旧 job superseded → 提交（此刻无 job 可执行：operation 仍 preparing）。
- **qdrant（事务外）**：删/建 collection（幂等、可重试，不持锁）。
- **activate 事务**：`SELECT library FOR UPDATE` 校验仍是本 operation → 每文档建一条带
  `rebuild_operation_id` 的 job、写 `expected_job_count` → `operation.status='running'` 提交。
- **finalize（决策 3：即时 + 周期 reconcile）**：
  - Worker 每标一条 job done 后**立即尝试 finalize**（对 operation/library 加锁、幂等）。
  - 新增 reconcile（可挂在 Worker 主循环或独立轻量巡检）：周期扫描 `running` operation 兜底，
    防 Worker done 后、finalize 前崩溃。
  - 判定：本 operation `done` job 数 == `expected_job_count` → operation done + `index_state='ready'` +
    `active_rebuild_operation_id=NULL`；任一 job 终态 failed 或 collection 失败 → operation/库 `failed`。
- 重试同 operation 复用已递增的 `current_revision`（**不再 +revision**）；全新重建才 +revision。
- 检索遇 `index_state in ('rebuilding','failed')` → 503（见 §7）。
- **失败恢复**（设计 §9「失败恢复」表）：prepare 后崩溃→续跑同 operation 不再 +revision;
  qdrant 后/activate 前崩溃→幂等重跑 ensure + activate;running job 失败→库 failed,可重试同 operation;
  发起全新 operation→旧 operation 的 `pending/processing/failed`→superseded、**`done` 保留作历史审计**、revision+1。
  **`active_rebuild_operation_id` 在修复/全新重建完成前不静默清空。**
- **finalize/reconcile 锁序**：按 §5.2 全局序 `library → rebuild_operation → document`;
  即时 finalize 锁 `library FOR UPDATE → rebuild_operation FOR UPDATE`;reconcile **先无锁扫描** running id、
  再按该序加锁,**不可**先锁 operation 再等 library(否则与 rebuild 死锁)。

---

## 7. 检索（`app/services/retrieval.py` + `app/api/documents.py` query）

- 新建共享可见性过滤服务（两路复用：`/retrieval`、`/libraries/{slug}/query`）。
- 库级 `lifecycle_mode` 分流：`external` 跳过；`managed` 执行。
- `index_state != 'ready'` → 503（rebuilding/failed 不返回半成品）。
- **批次 A 只实现 revision 维度**：Qdrant 召回后批量
  `SELECT id, library_id, current_revision FROM documents WHERE id = ANY(:ids)`，丢弃「不存在 /
  payload 缺 `document_id` / `library_id` 不匹配 / `payload.document_revision (?? 1) != current_revision`」的候选；
  过滤在 source enrichment + rerank **之前**。（注意 payload 字段名是 **`document_revision`**，非 `revision`。）
- 有界补召回：`visibility_overfetch_factor/max`、`refetch_max_rounds(2~3)`、`total_candidate_cap`、`latency_budget_ms`。
- 过渡兼容：payload 缺 `document_revision` 视作 1（开关 `retrieval_consistency_filter`，便于灰度/回滚）。
- （deleted 维度在批次 B 给同一回查加 `deleted_at`、并丢弃 `deleted_at IS NOT NULL` 的候选。）

---

## 8. 配置（`app/config.py`）

新增：`visibility_overfetch_factor=3`、`visibility_overfetch_max=200`、`visibility_refetch_max_rounds=2`、
`visibility_total_candidate_cap=500`、`visibility_latency_budget_ms=800`、`retrieval_consistency_filter=true`、
`qdrant_upsert_timeout_seconds`（worker 最终写入有界超时）、`rebuild_*`（如需）。

---

## 9. API / Schema 影响

- `EmbeddingJobRead` 加 `document_revision`；后台 job 状态展示 `superseded`（中性色，不计失败）。
- `DocumentRead` 加 `current_revision`。
- 写接口（上传/更新/删除/rebuild）对 `external` 库一律 409；对 `rebuilding` 库 503+`Retry-After`。
- 检索接口在库 `rebuilding/failed` 时 503。
- admin-ui：库管理展示 `lifecycle_mode`/`index_state`；job 列表展示 revision/superseded（最小改动）。

---

## 10. 已定案（评审决策）

1. **A 阶段立即移除**同步「按 document_id 删点」，靠 revision 过滤挡住旧点；A 不独立上线，
   物理清理由批次 B 的 outbox 完成。
2. **回填完成后移除 `embedding_jobs.document_revision` 的 DB 默认值**，强制所有代码显式赋值。
3. 重建完成判定用 **「Worker 即时 finalize + 周期 reconcile」组合**（§6）。
4. 并发测试使用 **`SET LOCAL lock_timeout='2s'`**；**暂不**设置生产全局 lock timeout。

---

## 11. 实施顺序（每步可独立测试）

1. 迁移 0009 + 模型层 → `alembic upgrade head` 验证、模型 import 验证。
2. `eligibility` 纯逻辑函数 + 单测（§14.4 矩阵 1–5）。
3. 写路径 `_new_generation` + ingest/reingest 接入 + 单测（revision++/supersede/no-op 扩展到 payload）。
4. Worker 三处资格检查 + 锁顺序 + payload revision + 单测/集成（§14.1、§14.4 #6/#9）。
5. 重建三阶段 operation 状态机 + finalize（即时 + reconcile，锁序 library→operation）+ external 409 + 503 +
   单测/集成（§14.4 #7/#8、§14.5 R1–R12、§15.3）。
6. 检索 revision 过滤 + lifecycle 分流 + 有界补召回 + 单测/集成（§14.4 #10）。
7. API/schema/admin-ui 最小适配。
8. 全量 `pytest` 绿 + 自包含临时库 E2E（真实 DB/Qdrant/embedding，跑完清理，沿用本轮验收风格）。

---

## 12. 风险与回滚

- 唯一索引创建失败（存量重复活动 job）：迁移前置检查 + 清理。
- 锁顺序遗漏导致死锁：所有写路径统一 library→document、库锁用 `FOR KEY SHARE`（rebuild 用 `FOR UPDATE`）；
  并发集成测试用 `SET LOCAL lock_timeout='2s'` 验证无死锁（§14.4 #9）；暂不设生产全局 lock timeout。
- 检索过滤误伤外部库：`lifecycle_mode` 回填 external 必须在启用过滤前完成；过滤前置开关可灰度。
- 回滚：停 Worker → 关 `retrieval_consistency_filter` → 0009 downgrade（注明丢 revision 保护）。

---

## 13. 完成标准（DoD）

- 旧 Worker 无法成为当前 revision、无法覆盖当前文档状态（§16 批次 A 完成标准）。
- §14.1 + §14.4 + §14.5 全绿；E2E 资格条件/重建并发/三阶段恢复用例通过。
- 全量回归不破；external 库（case_chunks）检索行为零变化、鉴权一致、写/rebuild 被 409 拒且不触碰 Qdrant。
- **重申**：批次 A 不单独上生产；与批次 B 一起发布。

---

## 14. 批次 A 完成情况（实现记录）

**状态：已实现（未提交生产，按计划与批次 B 一起发布）。**

### 14.1 实现落点
- 迁移 `0009_revision_and_rebuild_operations.py`：`documents.current_revision`、
  `embedding_jobs.document_revision`(回填后去默认)/`rebuild_operation_id`、活动唯一索引
  `uq_jobs_doc_rev_active`、operation 文档唯一 `uq_jobs_op_doc`、`sys_libraries.lifecycle_mode/
  index_state/active_rebuild_operation_id`(CHECK)、新表 `rebuild_operations`(循环 FK 末尾 ALTER)。
- 模型：`app/models/rebuild_operation.py` + 三表列/索引。
- 写路径 `app/services/ingest.py`：`_new_generation`(+rev+supersede)、新建 rev=1、reingest 走新代际、移除同步删点。
- Worker `app/workers/embedder.py`：`eligibility()` 三处复用、最终写入锁序、payload 带 `document_revision`、
  `_mark_failed` revision 守卫、upsert 有界超时、周期 reconcile。
- 重建 `app/services/rebuild.py`：prepare/qdrant/activate 三阶段、finalize(即时+reconcile)、恢复 preparing。
- 检索 `app/services/visibility.py` + `retrieval.py`：lifecycle 分流 + revision 维度过滤 + 有界补召回。
- API/schema：写守卫 external 409 / rebuilding 503、`EmbeddingJobRead.document_revision`、
  `DocumentRead.current_revision`、`LibraryRead` 补三字段；`delete_library` 对 external 跳过删 collection；
  `scripts/register_case_chunks.py` 设 `lifecycle_mode='external'`。

### 14.2 评审返工修正（第二轮）
- **`uq_jobs_doc_rev_active` 之前漏建** → 已补入模型/迁移/实库。
- **`_lock_writable` 用旧对象** → 改读锁定后的新鲜 Library 行；`update`/upsert 对 Document 加 `FOR UPDATE`。
- **重建锁序反向 + 无恢复 + delete 失败继续** → `_activate`/`_mark_op_failed` 改 library→operation;
  `run_rebuild` 恢复 preparing operation;delete_collection 真失败 → op failed + raise。
- **`_mark_failed` 无 revision 守卫** → 过期任务 superseded、不回 pending、不污染新版本；upsert 用 `qdrant_upsert_timeout_seconds`。
- **external 删库仍删 collection / case-chunks 实库为 managed** → `delete_library` 跳过;脚本+实库改 external。
- **reconcile 仅空闲执行** → 主循环按 `worker_reconcile_seconds` 周期执行。
- **关键修正（集成测试发现）**：`with_for_update(key_share=True)` 实渲染 `FOR NO KEY UPDATE`(自冲突、
  仍串行化 worker)；改为 `with_for_update(read=True, key_share=True)` → 真 `FOR KEY SHARE`。
- **finalize 并发双完成**：`db.get` 的 identity-map 陈旧读 → 改标量取 id + `populate_existing` 锁内重读。

### 14.3 测试结果
- 全量 mock 套件：**141 passed, 5 skipped**(5 为 env 门控 PG 集成测试)。
- 真实 PG 集成 `tests/test_batch_a_pg_integration.py`(env 门控)：**4/4**——索引存在、唯一约束拒重复、
  锁互斥(FOR KEY SHARE 不互阻 / FOR UPDATE 被阻)、finalize 并发只一次；连 ingest 集成 **5/5**。
- 可重复 E2E `scripts/e2e_batch_a.py`(真实 DB/Qdrant/embedding，跑完清理)：**连跑两次各 11/11**——
  revision 过滤、三阶段重建、prepare 崩溃恢复、finalize 幂等、空库重建。
- app/ 批次 A 代码 ruff 0 项;实库 `alembic 0009`、`case-chunks=external`、两 job 唯一索引就位。
