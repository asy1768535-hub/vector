# 技术设计

## 1. 总体边界

采用分阶段流水线：

```text
浏览器分块上传
  -> 暂存对象
  -> DocumentImportJob 后台解析/切片
  -> 现有 EmbeddingJob
  -> 现有 GraphExtractionJob
```

API 进程只负责鉴权、校验上传会话和顺序写入数据块。解析器、数据库写入、Embedding 与知识图谱构建不占用上传请求生命周期。

## 2. 配置

新增后端配置：

- `max_import_file_bytes = 500 * 1024 * 1024`
- `import_upload_chunk_bytes = 8 * 1024 * 1024`
- `import_selection_max_files = 1000`
- `import_upload_file_concurrency = 2`
- `import_worker_batch_size = 1`
- `import_worker_max_attempts`
- `import_worker_stale_seconds`
- `import_staging_dir`
- `import_staging_retention_seconds`

`GET /libraries/{slug}/import-configuration` 返回非敏感客户端配置：允许扩展名、单文件上限、单次文件数、推荐分块大小和上传并发。前端只消费该响应，不再保存独立常量。

## 3. 数据模型

### 3.1 DocumentImportJob

新增 `document_import_jobs`：

- 身份：`id`、`library_id`、`requested_by`
- 文件：`file_name`、`relative_path`、`content_type`、`size_bytes`、`sha256`
- 暂存：`staging_key`、`upload_offset`、`upload_completed_at`
- 选项：`replace_document_id`、`external_id`、`security_level`、`graph_extraction_requested`
- 状态：`status`、`current_stage`、`attempt_count`、`worker_id`、`claimed_at`、`finished_at`、`last_error`
- 结果：`document_id`、`document_revision_id`、`embedding_job_id`
- 审计：`created_at`、`updated_at`

状态约束覆盖 `uploading/queued/processing/succeeded/failed/cancelled/superseded`。Worker 使用 `FOR UPDATE SKIP LOCKED`、租约回收和有限重试，沿用现有任务模式。

### 3.2 文件路径身份

为 `documents` 增加可空 `source_path`：

- 文件夹导入时写入规范化绝对库内路径，例如 `/项目A/合同/主合同.pdf`。
- 单独选择文件和已有 API 文档保持 `NULL`，继续沿用当前内容哈希或 `external_id` 身份。
- 新增活动文档唯一索引 `(library_id, source_path)`，条件为 `source_path IS NOT NULL AND deleted_at IS NULL`。
- 当前 `(library_id, content_hash)` 唯一索引增加 `source_path IS NULL` 条件，使不同目录中的同内容文件可以分别存在。

目录本身复用已有 `Folder`、`folders` API 和 `ensure_folder_path()`；不新增第二套目录表。

## 4. 上传协议

### 4.1 创建会话

`POST /libraries/{slug}/import-sessions`

请求包含文件名、相对路径、大小、最后修改时间和导入选项。后端执行权限、扩展名、路径、大小、图谱配置与文件数校验，返回任务 ID、当前偏移和分块大小。

### 4.2 上传数据块

`PUT /libraries/{slug}/import-sessions/{job_id}/content`

- 请求使用 `application/octet-stream`。
- `Upload-Offset` 必须等于数据库中的当前偏移。
- 单次正文不得超过配置的分块大小。
- 写入暂存文件成功并 `fsync` 后，在同一受控流程中推进偏移。
- 重复发送已确认分块返回当前偏移，不重复写入。
- 偏移、总大小或身份不符返回 `409`。

### 4.3 完成上传

`POST /libraries/{slug}/import-sessions/{job_id}/complete`

后端检查总大小，流式计算 SHA-256，原子地把任务从 `uploading` 转为 `queued`。响应为 `202` 和任务投影。

### 4.4 状态与重试

- `GET /libraries/{slug}/import-jobs`
- `GET /libraries/{slug}/import-jobs/{job_id}`
- `POST /libraries/{slug}/import-jobs/{job_id}/retry`
- `DELETE /libraries/{slug}/import-sessions/{job_id}` 取消未完成上传

任务投影根据关联的 Import、Embedding 和 Graph job 返回统一阶段与进度，不复制下游状态机。

## 5. 暂存与对象存储

扩展对象存储契约，增加基于本地路径/文件流的 `put_file` 和受限流式读取能力：

- Local：同文件系统优先原子移动，否则分块复制到临时目标后替换。
- MinIO：使用文件流或 `fput_object`，不构造 `BytesIO(content)`。
- OSS：传入文件对象并使用 SDK 流式上传。

上传会话先写入受控 staging 根目录。对象 key 只由服务端 UUID 生成，不使用客户端路径。成功绑定 Revision 后删除 staging；失败终态和过期会话由清理任务回收。

## 6. 解析与切片

解析器改为接受受控文件路径并增量产生结构化片段：

- PDF：`pypdf`/PDFium 从文件路径打开，逐页抽取；OCR 保持单页渲染上限。
- DOCX：从路径打开，逐段落/表格产生片段。
- XLSX：继续使用 `read_only=True`，从路径加载并逐行处理。
- TXT/Markdown/CSV：增量解码和逐行读取。
- JSON：使用流式 JSON 解析依赖处理顶层数组；单对象仍执行结构和解析文本上限。

切片分批写入数据库。超大 normalized source 写入对象存储，Revision 仅保存定位信息；小文档继续兼容当前 PG 正文快照。原文阅读接口按受控窗口读取，不一次返回超大正文。

Worker 在同一数据库事务边界内：

1. 解析并计算内容身份。
2. `ensure_folder_path()` 创建/复用目录。
3. 按 `source_path` 锁定已有文档。
4. 内容未变则标记 `succeeded/unchanged`。
5. 内容变化则创建新 Revision、Chunk、Source 和原文件绑定。
6. 创建现有 `EmbeddingJob`。
7. 提交后清理 staging。

Embedding 成功后的图谱触发保持当前契约，不在 Import Worker 内直接发布图谱。

## 7. 前端

上传区提供两个并列命令：

- `选择文件`
- `选择文件夹`

目录 input 使用 `webkitdirectory multiple`。队列以 `webkitRelativePath` 为主显示，根目录和子目录均保留。选择后：

- 最多收集 1000 个支持文件。
- 不支持、超限、重复和超过数量的文件按原因汇总。
- 两个文件并发，每个文件按后端分块大小顺序上传。
- 行内展示上传百分比和统一处理阶段。
- 页面重新进入时从任务 API 恢复已提交任务；重新选择相同文件后可从已确认偏移继续。

文档页面复用 `GET /folders` 构建左侧目录树；右侧文档列表按 `folder_id` 服务端筛选。目录树和列表在桌面端并排，在窄屏使用抽屉，不把树嵌套在装饰卡片中。

## 8. 兼容与迁移

- 保留 `/import-file`，标记为兼容接口；它继续服务现有 API 调用方，但复用新的会话/任务服务。
- 当前前端新增、单文件替换和批量替换全部切换到新协议。
- 原有返回结果依赖方通过兼容适配保留字段；新接口以 `202 + job_id` 为主。
- Alembic 从当前单一 head `0043` 新增下一版迁移。
- 部署必须同时启动 `app.workers.importer --watch`；本地 start/stop/status 脚本同步更新。

## 9. 风险与控制

- 500 MiB 乘以大量并发会耗尽磁盘：上传并发 2、解析并发 1、staging 配额和过期清理共同控制。
- 文件路径可能穿越：仅接收相对路径，统一斜杠，拒绝空段、`.`、`..`、盘符、UNC 和 NUL。
- 同路径并发导入可能生成冲突 Revision：`source_path` 唯一索引加文档行锁，冲突转幂等读取。
- Worker 崩溃可能留下 staging：租约回收和清理任务兜底。
- 超大原文不能由现有全文接口一次返回：采用窗口读取并保持小文档兼容。

## 10. 回滚

- 新前端可回退到旧 `/import-file`。
- Import Worker 可停止，已上传任务保留在数据库中等待恢复。
- 新表和 `source_path` 为增量字段，不改变已有文档语义。
- 迁移 downgrade 仅在确认不存在新导入任务与 `source_path` 文档后执行。
