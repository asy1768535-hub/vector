# PDF 生产受控试用方案（两段专属消费、防退化门禁、隔离重置、接口保真与全链路验收）

- **编写日期**：2026-09-24
- **当前状态**：**方案提交审阅中（尚未执行任何生产变更，未启动任何容器）**
- **方案定位与边界**：
  1. P3 本地成果保持“扫描表格文字行列排版试验”的有限验收状态不变，底层不伪称构建了 AST `Table/Row/Cell` 级语义结构化单元对象，亦未接入单元格独立局部重 OCR；
  2. 本方案专门解决在生产环境中（宿主机 `10.0.10.2`，192 核 Linux）：
     - 解析（Importer）与向量化（Embedder）两段针对指定测试知识库的专属消费；
     - 目标库配置缺失/无效时强行拒绝启动（Fail-Fast 门禁，绝不退化为全局领取）；
     - 阻断历史积压任务（约 338 个，执行前待复核实际精确数量）；
     - 消除 Worker 超期重置等全局副作用，避免误碰全库其他知识库；
     - 建立严格可追溯的候选代码与镜像指纹体系；
     - 按实际代码规范修订上传、删除与异步清理接口；
     - 提供覆盖“缺失配置、历史隔离、非 PDF 竞争、解析后 Embedding、异常中断、完整检索”的 6 项验证用例；
  3. **本方案仅供技术审阅，修订后停下，暂不执行部署或容器启动。**

---

## 1. 解析与向量化两段专属消费机制（阻断历史积压）

### 1.1 历史积压现状与倾泻风险
- **数据库积压现状**：生产 PostgreSQL 数据库 `document_import_jobs` 表中积压了约 342 个处于 `status = 'queued'` 的 PDF 任务，其中**约 338 个创建于 2026-09-22**，全部归属于历史测试库 `c5d69a03-2cda-47fd-a273-18f52746bf7e`（**上述数量均为历史抽样记录，执行前待复核实际精确数量**）。
- **默认申领逻辑缺陷**：现有生产 Worker 任务申领 SQL 采用全局 `ORDER BY created_at` 且无知识库维度过滤。若在常驻 Worker 上直接将 `WORKER_EXCLUDE_PDF` 设为 `0`，Worker 会首先申领 2026-09-22 创建的历史任务，导致新上传的测试文件被长时间阻塞，并引发大批无意义的历史重试与资源争抢。

### 1.2 两段专属消费流水线设计（Importer + Embedder）
为了让指定库的 PDF 能够从解析到向量化全流程自动流转，同时绝对不干扰历史库与其他正常业务库，必须在**解析（Importer）**与**向量化（Embedder）**两段均建立专属受控消费机制：

```
                    ┌────────────────────────────┐
                    │    用户上传 / API 提交     │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                    ┌────────────────────────────┐
                    │    PostgreSQL 任务队列     │
                    │   document_import_jobs     │
                    └──────┬──────────────┬──────┘
                           │              │
        [非 PDF 任务]      │              │  [仅限指定 target_library_id 的 PDF]
                           │              │
                           ▼              ▼
┌─────────────────────────────────┐   ┌──────────────────────────────────┐
│   vector-kb-importer-release    │   │    vector-kb-importer-trial      │
│  (生产常驻 Importer)             │   │   (受控试用 Importer 容器)        │
│                                 │   │                                  │
│ - WORKER_EXCLUDE_PDF=1 (阻断PDF)│   │ - WORKER_EXCLUDE_PDF=0           │
│ - 处理 Word, Excel, 图片         │   │ - WORKER_TARGET_LIBRARY_ID=      │
│ - 历史积压继续锁死              │   │   "<UUID>" (硬限定目标测试库)    │
└─────────────────────────────────┘   └─────────────────┬────────────────┘
                                                        │ 解析并切块
                                                        ▼
                                      ┌──────────────────────────────────┐
                                      │        PostgreSQL 任务队列       │
                                      │         embedding_jobs           │
                                      └──────┬────────────────────┬──────┘
                                             │                    │
                          [非 PDF 任务]      │                    │  [仅限指定 target_library_id 的 PDF]
                                             │                    │
                                             ▼                    ▼
                  ┌─────────────────────────────────┐   ┌──────────────────────────────────┐
                  │   vector-kb-embedder-release    │   │    vector-kb-embedder-trial      │
                  │  (生产常驻 Embedder)             │   │   (受控试用 Embedder 容器)        │
                  │                                 │   │                                  │
                  │ - WORKER_EXCLUDE_PDF=1 (阻断PDF)│   │ - WORKER_EXCLUDE_PDF=0           │
                  │ - 历史 PDF 向量任务继续锁死     │   │ - WORKER_TARGET_LIBRARY_ID=      │
                  │                                 │   │   "<UUID>" (硬限定目标测试库)    │
                  └─────────────────────────────────┘   └─────────────────┬────────────────┘
                                                                          │ 生成向量并写入
                                                                          ▼
                                                        ┌──────────────────────────────────┐
                                                        │         Qdrant 向量数据库        │
                                                        │       lib_<target_library_id>    │
                                                        └──────────────────────────────────┘
```

#### A. 解析段专属消费（Importer）
1. **常驻 Importer 保持阻断**：`vector-kb-importer-release` 严格保持 `WORKER_EXCLUDE_PDF=1`，继续毫秒级消费 Word、Excel、图片等非 PDF 业务文件，绝不触碰任何 PDF；
2. **专属 Importer 申领 SQL 限定**：
   在 `app/workers/importer.py` 的 `_claim_jobs` 查询中，强制增加目标库硬过滤与仅限 PDF 条件：
   ```sql
   AND library_id = :target_library_id
   AND LOWER(file_name) LIKE '%.pdf'
   ```
   （SQLAlchemy 参数绑定直接传入 UUID 对象，兼容 PostgreSQL 与 SQLite，避免方言 `::uuid` 语法冲突；试用 Importer 只领取该库的 PDF 文件，同库 docx/xlsx 留给常驻 Worker）。
   - 容器名称：`vector-kb-importer-trial`；
   - 环境变量：
     - `WORKER_EXCLUDE_PDF=0`
     - `WORKER_TARGET_LIBRARY_ID=<target_library_uuid>`（指定的专用测试知识库 UUID）
     - `OCR_INTRA_OP_NUM_THREADS=4`，`OCR_INTER_OP_NUM_THREADS=1`
   - 运行命令：`python -m app.workers.importer --watch`。

#### B. 向量化段专属消费（Embedder）
1. **常驻 Embedder 保持阻断**：
   - 当前常驻 `vector-kb-embedder-release` 容器环境变量中亦保持 `WORKER_EXCLUDE_PDF=1`，其在申领 `embedding_jobs` 时会直接过滤掉 PDF 任务（`NOT :exclude_pdf OR LOWER(d.title) NOT LIKE '%.pdf'`）；
   - 这意味着如果不启动专属 Embedder，新解析完成的 PDF 切块将无法被自动向量化，进而无法进入检索；
2. **专属 Embedder 申领 SQL 限定**：
   在 `app/workers/embedder.py` 的 `_claim_jobs` 查询中，强制增加目标库硬过滤与仅限 PDF 条件：
   ```sql
   AND j.library_id = :target_library_id
   AND (
       LOWER(COALESCE(d.source_path, '')) LIKE '%.pdf'
       OR LOWER(COALESCE(d.title, '')) LIKE '%.pdf'
   )
   ```
   （试用 Embedder 仅处理目标库 PDF 向量任务，同库 docx/xlsx 向量任务交由常驻 Embedder 处理）。
   - 容器名称：`vector-kb-embedder-trial`；
   - 环境变量：
     - `WORKER_EXCLUDE_PDF=0`
     - `WORKER_TARGET_LIBRARY_ID=<target_library_uuid>`
   - 运行命令：`python -m app.workers.embedder --watch`。

---

## 2. 目标库配置强校验与防退化门禁（Fail-Fast Gating）

**核心安全原则：目标库 ID 缺失、无效或过滤未生效时，必须坚决拒绝启动，绝不允许隐式退化为全局领取。**

在两段 Worker 启动时（主入口 `if __name__ == "__main__":` 模块初始化及 `--watch` 循环开始前），必须执行强校验：

1. **环境参数校验逻辑**：
   ```python
   exclude_pdf = os.getenv("WORKER_EXCLUDE_PDF", "").strip().lower() in {"1", "true", "yes", "on"}
   raw_target_lib = os.getenv("WORKER_TARGET_LIBRARY_ID", "").strip()

   if not exclude_pdf:
       # 当允许处理 PDF 时，必须是靶向受控模式，强校验目标库 ID
       if not raw_target_lib:
           log.critical("FATAL: WORKER_TARGET_LIBRARY_ID must be specified when WORKER_EXCLUDE_PDF=0 in trial mode.")
           sys.exit(1)
       try:
           target_library_id = uuid.UUID(raw_target_lib)
       except (ValueError, TypeError) as exc:
           log.critical("FATAL: WORKER_TARGET_LIBRARY_ID='%s' is not a valid UUID: %s", raw_target_lib, exc)
           sys.exit(1)
   ```
2. **防退化门禁与断言**：
   - 在 `_claim_jobs` 执行 SQL 准备阶段，增加程序断言：
     ```python
     if not exclude_pdf:
         assert target_library_id is not None, "Target library ID must never be None when exclude_pdf is False"
     ```
   - 绝不允许由于参数解析异常或空字符串而使 SQL 变为 `(:target_lib IS NULL OR ...)` 进而扫描全表；
   - 校验失败立即调用 `sys.exit(1)` 退出进程，容器在 Docker 级别呈现为 `Exited (1)` 失败状态，阻断任何数据库操作。

---

## 3. Worker 超期任务重置等全局副作用核查与库隔离处理

### 3.1 现存代码全局副作用深度核查
深度审计发现，现有两段 Worker 中内置的任务超时回收机制均存在严重的**全局副作用**：

1. **解析段 `app/workers/importer.py::_reset_stale_jobs`**：
   ```sql
   UPDATE document_import_jobs
   SET status = 'failed' / 'queued', ...
   WHERE status = 'processing'
     AND current_stage IN ('validating', 'parsing', 'chunking')
     AND claimed_at IS NOT NULL
     AND claimed_at < NOW() - (:seconds || ' seconds')::interval
   ```
   - **风险**：该 SQL 完全没有 `library_id` 条件。若试用 Worker 以长周期运行，它会扫描整个数据库中所有处于 `processing` 且超时的任务。如果其他正常知识库恰好有长文件正在被常驻 Worker 处理，可能会被试用 Worker 误判为超时而强行重置或置为失败！
2. **向量化段 `app/workers/embedder.py::_reset_stale_jobs`**：
   ```sql
   UPDATE embedding_jobs
   SET status = 'failed' / 'pending', ...
   WHERE status = 'processing'
     AND claimed_at IS NOT NULL
     AND claimed_at < NOW() - (:secs || ' seconds')::interval
   ```
   - **风险**：同样完全没有 `library_id` 条件，会对全库所有知识库的在途 `embedding_jobs` 产生全局副作用。

### 3.2 专属试用 Worker 的无毒隔离设计
为了彻底杜绝误碰全库其他任务，两段试用 Worker 必须采用**库隔离的超时重置机制**：

1. **重置 SQL 强制追加库级限定**：
   在靶向试用模式下，两段 Worker 的 `_reset_stale_jobs` 必须强制绑定目标知识库参数：
   - `importer.py` 重置 SQL 调整为：
     ```sql
     WHERE status = 'processing'
       AND library_id = :target_library_id
       AND current_stage IN ('validating', 'parsing', 'chunking')
       AND claimed_at < NOW() - (:seconds || ' seconds')::interval
     ```
   - `embedder.py` 重置 SQL 调整为：
     ```sql
     WHERE status = 'processing'
       AND library_id = :target_library_id
       AND claimed_at < NOW() - (:secs || ' seconds')::interval
     ```
2. **主循环全局清理与 Reconcile 审计与隔离**：
   - **Importer 暂存清理（`_maybe_cleanup_staging`）**：在靶向试用模式下直接跳过，全库 staging 清理与超时对齐由后台常驻 Importer 统一执行，试用 Worker 零介入；
   - **Embedder 重建收口（`rebuild_svc.reconcile_running`）**：在靶向试用模式下跳过周期与退出时的全局 rebuild operations reconcile，避免试用 Worker 触碰其他库的索引重建；
3. **效果保障**：
   - 试用 Worker 仅对指定目标测试库内部超时卡死的自身任务执行清理与重置；
   - 对数据库内所有其他业务知识库的任务执行**零扫描、零更新、零副作用**；
4. **手动应急重置命令（严格限定库 ID）**：
   若试用过程意外中断需要人工重置任务，操作命令严格限定如下：
   ```sql
   UPDATE document_import_jobs
   SET status = 'queued', current_stage = 'queued', worker_id = NULL, claimed_at = NULL
   WHERE library_id = '2ed27105-9d37-4094-9c07-ec4446f8a56a'::uuid
     AND status = 'processing';
   ```

---

## 4. 接口路径修正与异步清理机制保真说明

针对现有代码库实际实现，对前序文档中的模糊描述进行精准修正：

### 4.1 导入上传接口（当前代码真实路由）
- **分块上传与会话流式接口（标准路径，定义于 `app/api/import_uploads.py`）**：
  1. 创建上传会话：`POST /libraries/{slug}/import-sessions`（生成 `DocumentImportJob`，分配 `staging_key`）；
  2. 上传文件分块：`PUT /libraries/{slug}/import-sessions/{job_id}/content`（请求头带 `Upload-Offset` 与分块二进制）；
  3. 完成上传触发处理：`POST /libraries/{slug}/import-sessions/{job_id}/complete`（进入 `queued` 状态供 Worker 申领）；
  4. 中途取消会话：`DELETE /libraries/{slug}/import-sessions/{job_id}`（清理 staging 文件并关闭任务）；
- **直接上传接口（定义于 `app/api/documents.py`）**：
  - `POST /libraries/{slug}/import-file`（表单文件单步直传）；
- **规范纠正**：上传流式内容使用 `PUT /libraries/{slug}/import-sessions/{job_id}/content`，单步直传端点为 `POST /libraries/{slug}/import-file`。
### 4.2 文档删除与异步清理机制（当前代码真实逻辑）
- **删除请求入口（定义于 `app/api/documents.py`）**：
  - `DELETE /libraries/{slug}/documents/{document_id}`
- **两阶段删除与异步清理真实流程**：
  1. **第一阶段：事务内即时软删除与检索屏蔽（同步事务）**：
     - 将 `Document` 标记为软删除：`deleted_at = now, status = 'deleted'`；
     - 将关联在途的 `EmbeddingJob` 标记为失效：`status = 'superseded', finished_at = now`；
     - **检索立即生效**：Dify 或系统检索查询带有 `WHERE Document.deleted_at IS NULL` 过滤，该文档在 HTTP 接口返回 204 的瞬间即对所有检索完全不可见；
  2. **第二阶段：Outbox 异步物理清理（Cleanup Worker 异步幂等执行）**：
     - 在删除事务中，系统调用 `cleanup_service.enqueue_delete_document(db, locked, doc.id)`，向 `cleanup_outbox` 表写入一条类型为 `delete_document` 的事件；
     - 独立的后台服务 `cleanup_worker` 定时申领该事件，异步调用 Qdrant API 物理删除集合中的对应点，并清理文件资源引用与临时落盘；
- **规范纠正**：文档删除**绝不是在 HTTP 事务中同步执行 Qdrant 物理擦除**，而是采用“同步软删除隐藏 + Outbox 异步物理清理”架构。

---

## 5. 可追溯的候选代码与镜像指纹体系

为了彻底消除模糊的“已归档 P3 Commit”说法，建立严密、可验证、全链条可追溯的发布指纹体系：

### 5.1 源码构建指纹（Source Build Fingerprint）
在服务器构建镜像前，记录并核验以下指纹三元组：
1. **基础 Git 提交**：运行 `git rev-parse HEAD`，获取基线 Commit SHA；
2. **工作区差异校验和**：
   - 运行 `git status --porcelain`；
   - 若工作区存在因本次任务修改的未提交变更，运行 `git diff | sha256sum` 生成补丁哈希；
   - 只有当该补丁哈希与本地已通过全部测试的补丁哈希一致时，方可进入镜像构建；
3. **核心源码 SHA-256 清单**：
   构建脚本对关键受影响文件进行 `sha256sum` 计算并固化落盘为 `build_manifest.json`：
   - `app/services/pdf_extract.py`（网格形态学对齐与跨列防重复保护）
   - `app/services/ocr.py`（独立数字正则与线程配置）
   - `app/workers/importer.py`（靶向库过滤与隔离超时重置）
   - `app/workers/embedder.py`（靶向库过滤与隔离超时重置）
   - `app/config.py`（确保 `pdf_quality_cascade_enabled = False`）

### 5.2 镜像产物指纹（Image Artifact Fingerprint）
1. **镜像名称与候选标签**：
   - Importer 镜像：`vector-kb-importer:candidate-p3-trial-20260924`
   - Embedder 镜像：`vector-kb-embedder:candidate-p3-trial-20260924`
   - **严禁使用或覆写生产 `release` 标签**；
2. **Image Digest 固化记录**：
   构建完成后，记录 Docker 镜像的唯一内容散列：
   - `docker inspect --format='{{index .RepoDigests 0}}' <IMAGE_NAME>` 或 `docker image inspect --format='{{.Id}}'` 生成的 `sha256:...` 摘要；
3. **运行时容器内指纹自检**：
   容器启动前通过一次性容器自检：
   ```bash
   docker run --rm <IMAGE_NAME> sha256sum /app/services/pdf_extract.py /app/services/ocr.py /app/workers/importer.py
   ```
   确认容器内部署的代码与源码构建指纹 100% 逐位吻合。

---

## 6. 六大针对性验证用例设计

针对方案设计的关键安全机制与业务闭环，设计以下 6 个专项测试用例：

### 用例 1：缺失/无效目标库配置拒绝启动（Fail-Fast 门禁用例）
- **测试目的**：验证当 `WORKER_EXCLUDE_PDF=0` 但未提供合法 `WORKER_TARGET_LIBRARY_ID` 时，容器绝不退化为全局领取，坚决退出。
- **前置条件**：准备启动参数。
- **触发操作**：
  1. 场景 A：设置 `WORKER_EXCLUDE_PDF=0`，不传 `WORKER_TARGET_LIBRARY_ID` 启动容器；
  2. 场景 B：设置 `WORKER_EXCLUDE_PDF=0`，设置 `WORKER_TARGET_LIBRARY_ID="123-abc-not-uuid"` 启动容器。
- **预期结果与通过标准**：
  - 容器在 1 秒内异常退出（Exit Code 1）；
  - 容器日志输出带有 `FATAL: WORKER_TARGET_LIBRARY_ID ...` 的明确拦截记录；
  - 数据库日志证实未向数据库发起任何任务申领 SQL。

### 用例 2：历史队列完全隔离验证（Historical Queue Isolation）
- **测试目的**：验证历史测试库（`c5d69a03`）中积压的任务（约 338 个，执行前待复核实际精确数量）全程未被碰触。
- **前置条件**：查询并记录当前数据库中 `library_id = 'c5d69a03'` 且 `status = 'queued'` 的所有任务 ID 清单及 `attempt_count`。
- **触发操作**：启动试用 Worker 容器并使其持续轮询 60 秒。
- **预期结果与通过标准**：
  - 历史库的任务状态 100% 维持为 `queued`；
  - `attempt_count`、`worker_id`、`claimed_at` 均保持无变动；
  - 试用 Worker 的日志显示每次循环 `claimed: 0`，零历史任务泄漏。

### 用例 3：非 PDF 任务无干扰并发竞争验证（Non-PDF Concurrency）
- **测试目的**：验证试用期间在线提交的常规非 PDF 文件（docx、xlsx、txt）由常驻 Worker 正常消费，两组 Worker 无锁争抢、无队列饿死。
- **前置条件**：试用 Worker 正在后台运行。
- **触发操作**：向系统正常知识库上传 S4 (`赤水不动产位置信息.docx`) 与 S5 (`赤水不动产面积统计.xlsx`)。
- **预期结果与通过标准**：
  - 常驻 Worker `vector-kb-importer-release` 毫秒级领走并在基准时间内消费完成（时延执行前待复核）；
  - 试用 Worker 日志显示忽略了这批非目标库的任务；
  - 数据库行级锁 `FOR UPDATE SKIP LOCKED` 正常工作，两组 Worker 互不阻塞。

### 用例 4：解析后分段 Embedding 靶向消费验证（Targeted Post-Parse Embedding）
- **测试目的**：验证目标库 PDF 完成解析后生成的 `embedding_jobs` 仅被专属 Embedder 消费，常驻 Embedder 保持阻断，且专属 Embedder 绝不领走其他库的任务。
- **前置条件**：专属 Importer 与专属 Embedder 均已启动并限定于测试库。
- **触发操作**：在测试库提交 S2（1 页扫描件）完成解析并生成 1 个切块。
- **预期结果与通过标准**：
  - `vector-kb-embedder-trial` 自动领走该 `embedding_job` 并调用模型生成向量写入 Qdrant；
  - 常驻 `vector-kb-embedder-release` 日志确认由于 `WORKER_EXCLUDE_PDF=1` 忽略了该 PDF 向量任务；
  - 若此时其他知识库生成了 docx 的向量任务，试用 Embedder 忽略，常驻 Embedder 正常领走，实现严格分流。

### 用例 5：异常中断与库级无毒重置验证（Abnormal Interruption & Scoped Stale Reset）
- **测试目的**：验证试用容器在处理长文档时被强行中断（`SIGKILL`），内置重置机制仅恢复目标库的任务，绝不误伤全库其他在途任务。
- **前置条件**：目标库上传 S1（45 页长文档）进入 `processing` 状态。
- **触发操作**：
  1. 执行 `docker kill -s 9 vector-kb-importer-trial` 强行杀死试用容器；
  2. 模拟超时或由试用 Worker 执行重置。
- **预期结果与通过标准**：
  - 目标库中 S1 任务被正确重置为 `queued`；
  - 数据库审计确认执行的 UPDATE 语句包含 `WHERE library_id = :target_library_id`；
  - 其他知识库中正在执行的长任务（若有）未发生状态跳变或被误标失败。

### 用例 6：端到端完整鉴权检索验证（End-to-End Authenticated Retrieval）
- **测试目的**：验证 5 份样本全链路入库后，通过真实 Dify Retrieval API 进行 Bearer Token 鉴权检索的召回率与版面质量。
- **前置条件**：5 份样本全部处理完成，文档状态流转为 `ready`。
- **触发操作**：调用 Dify 检索 API 依次检索：
  - **Query 1**：“赤水市房他证押字第2014-00006号”
  - **Query 2**：“遵义市 石油宾馆 高家庄家常馆”
  - **Query 3**：“赤水市交通建筑建材工程有限责任公司 法律尽职调查报告”
- **预期结果与通过标准**：
  - **S3 权证召回**：Top 1 命中 S3 切块，相似度 `score >= 0.70`，返回切块呈现规整的 5 列管道符网格，空白单元格占位完好，跨列文本严格单次出现；
  - **S2 招牌召回**：Top 1 命中 S2 切块，相似度 `score >= 0.70`，招牌文字“丽烫染照”完整存在；
  - **S1 尽调召回**：Top 1 命中 S1 切块，相似度 `score >= 0.75`，时间戳保持 `16:23` 冒号格式；
  - 检索耗时处于正常服务阈值（执行前待复核）。

---

## 7. 生产数量、指标与时延基准（执行前待复核）

所有生产数据规模与运行耗时基准统一标注为**执行前待复核**，坚决不作无证据的固化宣称：

| 维度 / 项目 | 当前参考数据（历史抽样） | 状态说明 |
| :--- | :--- | :--- |
| **全库 queued 状态 PDF 任务数** | 约 342 个 | **执行前待复核实际精确数量** |
| **历史测试库 `c5d69a03` 积压数** | 约 338 个 | **执行前待复核实际精确数量** |
| **S1（45页混合）生产总导入耗时上限** | <= 40 秒（参考历史单次 35.8s） | **执行前待复核（受宿主机实时负载影响）** |
| **S2（1页扫描件）生产总导入耗时上限** | <= 5 秒（参考历史单次 3.4s） | **执行前待复核（受宿主机实时负载影响）** |
| **S3（6页扫描表格）生产总导入耗时上限**| <= 15 秒（参考历史单次 10.5s） | **执行前待复核（受宿主机实时负载影响）** |
| **S4 / S5（Word / Excel）导入耗时上限** | <= 1 秒 | **执行前待复核** |
| **宿主机硬件配置** | Linux x86_64, 192 CPU 核心, 内存充足 | **执行前待复核容器配额限制（CPU/Memory limits）** |
| **检索召回相似度门禁** | `score >= 0.70`（S1 尽调 `>= 0.75`） | **执行前待复核基准阈值** |

---

## 8. 安全回退机制与应急步骤

试用方案遵循“零停机、秒级切断、零数据残留”的设计原则：

1. **级别一：秒级停止试用（服务完全无损，耗时 < 3 秒）**：
   - 执行：`docker stop vector-kb-importer-trial vector-kb-embedder-trial && docker rm vector-kb-importer-trial vector-kb-embedder-trial`；
   - 效果：指定库的 PDF 解析与向量化消费立即切断，常驻 Worker `vector-kb-importer-release` 与 `vector-kb-embedder-release` 从始至终保持运行，全量在线业务不受任何干扰。
2. **级别二：处理中中断任务的队列回滚**：
   - 运行针对测试库的定向重置 SQL（见 3.2 节），将残留的 `processing` 任务重置回 `queued`；338 个历史任务全程未被触碰。
3. **级别三：测试知识库数据与向量清理**：
   - 调用标准业务接口删除试用文档：`DELETE /libraries/{slug}/documents/{document_id}`，触发事务软删除并由 Cleanup Worker 异步清理 Qdrant 中的对应向量点；
   - 也可直接重置该测试库，确保数据库与其他业务库 100% 洁净。

---

## 9. 方案结论与审阅交接

- 本方案完整补齐了解析（Importer）与向量化（Embedder）两段专属消费逻辑，建立了强校验防退化门禁，彻底排除了超时重置的全局副作用；
- 方案精准对照当前代码修正了上传、删除及异步清理机制，建立了基于代码与镜像指纹的严密溯源体系，并制定了针对性验证用例。

---

## 10. 2026-09-24 S3 隔离受控试验执行证据固化

### 10.1 配置歧义修复与负向测试（单元测试 + 远程容器测试）
1. **两项配置歧义根治**：
   - **开关漏配/非法**：`WORKER_EXCLUDE_PDF` 必须显式为 `'1'` 或 `'0'`，两者均缺失时触发 `FATAL: Ambiguous or missing WORKER_EXCLUDE_PDF=''` 并 `exit(1)`。
   - **目标库与排除开关冲突**：配置了 `WORKER_TARGET_LIBRARY_ID` 但 `WORKER_EXCLUDE_PDF=1` 时，触发 `FATAL: Configuration conflict!` 并 `exit(1)`。
2. **负向测试验证**：
   - **本地单元测试**：`tests/test_worker_trial_isolation.py` 14 个测试全数通过（包括两开关漏配、配置冲突、目标库参数绑定过滤、超时重置范围收敛测试）。
   - **远程容器内实测**：针对 `vector-kb-importer:candidate-p3-trial-20260924` 与 `vector-kb-embedder:candidate-p3-trial-20260924` 执行 4 项负向配置启动测试，100% 返回 exitcode 1 并输出指定 FATAL 日志。

### 10.2 凭据安全与明文环境文件安全清理
1. **远程明文文件清理**：构建与容器启动生成的临时 `.env` 文件在容器运行后即刻执行 unlink，构建包 `trial_files.tar.gz` 彻底清除。
2. **本地临时凭据清理**：本地 scratch 目录中的 `extracted_creds.txt` 彻底删除。
3. **日志安全审查**：所有脚本执行与日志输出全程杜绝打印密码、Token 或密钥明文变量值。

### 10.3 隔离库（pdfmrecheck）S3 真实全链路试验
1. **样本文件**：用户提供的真实 S3 扫描表格 PDF（`他证（赤水市房他证押字第2014-00006号）.pdf`，9,222,327 字节）。
2. **任务处理与 Worker 归属**：
   - 上传文件名：`p3_s3_trial_1790243900.pdf`，生成文档 ID：`52b15d5f-ecc5-47e3-930e-da7f45537c1a`。
   - 导入任务：`176cf49d-6640-4188-b7dd-64c6f47551ae`，由专属试用容器 `vector-kb-importer-trial`（Worker ID `4cc056014ff7-1`）在 15.276 秒内完成，状态 `succeeded`。
   - 向量任务：`24c6e6da-8276-492d-98fd-c060d18d866a`，由专属试用容器 `vector-kb-embedder-trial`（Worker ID `80a0d771bd1e-1`）在 1.115 秒内完成，状态 `done`。
   - Qdrant 向量点增长：`103 -> 110`（净增 7 个点）。
3. **切块与表格排版核验（7 个切块）**：
   - 检出他项权证、抵押清单表头；
   - 跨列不确定标记 `[跨列不确定: 参见第1列]` 准确出现于邻列，原列保留完整文本，无重复膨胀；
   - 关键表格单元格 `201400629` 与 `69.59` 分列独立规整呈现。
4. **鉴权检索召回核验**：
   - **Query 1** `赤水市房他证押字第2014-00006号`：Top 1 命中 S3，相似度 `0.9973`（耗时 0.401s）。
   - **Query 2** `谢和平 和平山庄 201400629 69.59`：Top 1 命中 S3，相似度 `0.9658`（耗时 0.383s）。
   - **Query 3** `201400629 69.59 房屋他项权`：Top 1 命中 S3，相似度 `0.9749`（耗时 0.340s）。

### 10.4 历史积压队列 338 个任务逐项比对审计
将历史测试库 `c5d69a03-2cda-47fd-a273-18f52746bf7e` 的全量 338 个待处理任务与试验前快照 `historical_baseline_before.json` 进行精确逐 ID 对比：
- **总任务数**：338 / 338；
- **状态变动**：0（全数维持 `status = 'queued'`）；
- **attempt_count 变动**：0（全数保持 `attempt_count = 0`）；
- **Worker 申领变动**：0（全数保持 `worker_id IS NULL`）；
- **结论**：历史任务隔离率 100%，未发生任何误领或状态漂移。

### 10.5 文档软删除与异步清理闭环
1. **调用删除接口**：`DELETE /libraries/pdfmrecheck/documents/52b15d5f-ecc5-47e3-930e-da7f45537c1a`，返回 HTTP 204（耗时 0.082s）。
2. **文档状态**：数据库记录流转为 `status = 'deleted'`，`deleted_at IS NOT NULL`。
3. **即刻检索不可见**：删除后立刻检索相同关键词，S3 文档完全不可见（命中数降为 0）。
4. **异步清理 Outbox 消费**：
   - `qdrant_cleanup_outbox` 产生 2 条事件：`delete_document_all` 与 `delete_file_resources`。
   - 常驻 Cleanup Worker `vector-kb-cleanup-release`（Worker ID `1e64695acec2-1`）在 2 秒内消费完成，两记录状态均流转为 `status = 'done'`。
5. **Qdrant 物理清理**：Qdrant 集合点数精确从 110 点回落至 103 点（净清理 7 个点）。

### 10.6 候选镜像摘要与常驻 Worker 状态
1. **候选镜像摘要**：
   - **Importer 候选镜像**：`vector-kb-importer:candidate-p3-trial-20260924`
     - ID：`sha256:7c6de33204f70f3411a27e55ece4171947c921cc37140d936cdfe23120df6c7d`
     - 大小：1,628,512,763 字节 (~1.63 GB)
     - 创建时间：`2026-09-24T17:48:23.966508464+08:00`
   - **Embedder 候选镜像**：`vector-kb-embedder:candidate-p3-trial-20260924`
     - ID：`sha256:e1693b10008e55b50ef2c9d5351919d0b26e269a275c305f64ad02e81917096e`
     - 大小：1,555,363,092 字节 (~1.56 GB)
     - 创建时间：`2026-09-24T17:48:24.324170028+08:00`
2. **试用容器清理**：`vector-kb-importer-trial` 与 `vector-kb-embedder-trial` 已停止并删除。
3. **常驻 Worker 健康与隔离状态**：
   - `vector-kb-importer-release`：持续运行，环境变量保持 `WORKER_EXCLUDE_PDF=1`。
   - `vector-kb-embedder-release`：持续运行，环境变量保持 `WORKER_EXCLUDE_PDF=1`。
   - **正式业务库状态**：严密保持未开放，暂不放行，待技术评估审阅。
4. **完整证据归档路径**：`s3_trial_execution_evidence_20260924.json`。
