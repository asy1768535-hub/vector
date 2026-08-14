# 实施计划

## 1. 契约与迁移

- 增加 Import 配置、会话、任务状态和重试 Schema。
- 新增 `DocumentImportJob` 模型与 Alembic 迁移。
- 为 `Document.source_path` 增加字段和条件唯一索引。
- 增加模型/迁移正反向契约测试。

## 2. 暂存与流式存储

- 实现路径规范化、staging key 和磁盘配额校验。
- 扩展 Local、MinIO、OSS 适配器的流式写入契约。
- 实现上传偏移校验、分块落盘、完成哈希和取消清理。
- 覆盖超限、乱序、重复分块、路径穿越、存储失败测试。

## 3. 导入任务服务与 API

- 实现创建会话、追加内容、完成、查询、取消和重试。
- 保持 `insert` 权限和图谱上传配置的后端复验。
- 增加 API 影响检查和路由契约测试。
- 为旧 `/import-file` 增加兼容适配。

## 4. 路径解析器与 Import Worker

- 把 PDF、DOCX、XLSX、文本、Markdown、CSV、JSON 解析入口改为路径/流式接口。
- 实现分批切片持久化和大正文外置。
- 实现 `FOR UPDATE SKIP LOCKED` claim、租约、重试和终态清理。
- 复用 `ensure_folder_path()`，按 `source_path` 实现跳过/新 Revision。
- 创建现有 EmbeddingJob，并保留图谱触发边界。

## 5. 前端上传

- 新增后端配置读取和任务 API 封装。
- 增加“选择文件夹”input 与相对路径队列。
- 将单批上限改为 1000，按原因汇总不可上传文件。
- 实现 8 MiB 分块、2 文件并发、偏移恢复、取消和重试。
- 将新增、单替换和批量替换接入新协议。
- 在同一队列行展示上传、解析、切片、Embedding 和图谱阶段。

## 6. 文档目录树

- 复用现有 folders API 加载目录。
- 文档列表 API 增加 `folder_id` 筛选与服务端分页。
- 文档页增加桌面目录树和移动端目录抽屉。
- 展示来源路径，并验证同名文件可区分。

## 7. 运行与清理

- 新增 `app.workers.importer` CLI 与 heartbeat。
- 更新 start/stop/status 本地脚本和部署文档。
- 扩展 cleanup worker 回收过期会话与孤立 staging。
- 增加磁盘配额、过期时间和并发配置启动校验。

## 8. 验证

后端重点：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_import_upload_sessions.py
.\.venv\Scripts\python.exe -m pytest tests/test_import_worker.py
.\.venv\Scripts\python.exe -m pytest tests/test_v02_m4_folders_sync_evidence.py
.\.venv\Scripts\python.exe -m pytest tests/test_query_import_api.py tests/test_batch2_fixes.py
.\.venv\Scripts\python.exe -m pytest tests/test_v04_m5_triggers.py tests/test_v08_object_storage.py
.\.venv\Scripts\python.exe -m ruff check app tests
```

前端重点：

```powershell
node --test admin-ui/import_redesign.test.mjs
node --test admin-ui/folder_import.test.mjs
node --test admin-ui/documents_folder_tree.test.mjs
node --test (Get-ChildItem admin-ui -Recurse -Filter *.test.mjs | ForEach-Object FullName)
```

迁移与范围：

```powershell
.\.venv\Scripts\alembic.exe heads
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe downgrade -1
.\.venv\Scripts\alembic.exe upgrade head
git diff --check
```

浏览器验收：

- 1440x900 与 390x844。
- 多层目录、同名文件、1000 文件边界。
- 500 MiB 边界使用稀疏测试文件或生成流验证，不把大二进制加入仓库。
- 上传过程中刷新、重选同文件后续传。
- 单项失败不阻塞同批其他文件。
- 目录树、任务进度、替换和图谱开关无回归。

## 9. 高风险文件与回滚点

- `app/api/documents.py`：旧接口兼容，先以契约测试保护。
- `app/services/revision_files.py` 与对象存储适配器：先增加新方法，不改变旧 `put/read`。
- `app/workers/embedder.py`：只消费现有 EmbeddingJob，不并入文件解析逻辑。
- `admin-ui/src/views/Import.js`：上传状态机独立成 helper，视图只做投影。
- `admin-ui/src/views/Documents.js`：目录树不改变现有文档详情、删除和替换入口。
- Alembic：新表/可空列先上线，Worker 和前端随后启用。
