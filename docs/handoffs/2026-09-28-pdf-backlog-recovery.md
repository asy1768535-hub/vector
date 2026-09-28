# PDF 历史批次恢复交接（2026-09-28）

## 当前续接提示（2026-09-28 UTC，仍在执行）

下方原交接保留为历史快照。执行审批已通过，隔离恢复、SQL/Qdrant 核验和
原件页证据检查均已实际执行；不要继续使用旧的“未执行/审批阻塞”结论。
当前恢复仍在进行，未声明本批全部完成。687 页文件的 PNG 字节超限及第 609
页空链接动作异常已有独立复现与最小修复；新候选仅用于临时恢复进程。
当前实施记录及候选/审计标识见本机任务
`.trellis/tasks/09-27-pdf-backlog-ingestion/implement.md`。汇总必须区分入库、
视觉覆盖不完整和未验证；不可用 ready 或本地测试代替页面/当前版本向量证据。

## 原交接快照

工作目录：本项目 checkout。
分支 / HEAD：`codex/sync-server-20260917` / `c8e1dc7`。
目标环境：本地修改；授权服务器仅容器元数据与上传哈希已验证，业务验收未验证。
当前目标：继续既有 338 份 PDF 精确快照恢复，不重新设计、不设置定时任务。

## 必须遵守

- 真源：[恢复路线图](../roadmap/pdf-resource-recovery-20260927.zh-CN.md)及本机任务 `.trellis/tasks/09-27-pdf-backlog-ingestion/`。
- 保留现有脏工作树；禁止改快照或 attempt_count 绕过单次保护。
- 原件、生产源码、凭据、个人字段和审计明细留在服务器。
- 用户授权本地修改后上传与既有批次恢复，不授权绕过 SSH Manager 审批。
- 不重启已经退出的恢复容器来重复执行；不删除异常现场。

## Git 状态

以下为本轮代码完成、追加交接文档之前的 `git status --short` 原样输出。
其后本轮仅追加本交接、AGENT_README 的入口和本机实施记录。

```text
 M .env.example
 M admin-ui/import_redesign.test.mjs
 M admin-ui/preview_mode.test.mjs
 M admin-ui/src/folder_import.js
 M admin-ui/src/icons.js
 M admin-ui/src/my_tasks_ui.test.mjs
 M admin-ui/src/preview_mode.js
 M admin-ui/src/views/ApiKeys.js
 M admin-ui/src/views/MyTasks.js
 M admin-ui/style.css
 M admin-ui/upload_inbox_1a.test.mjs
 M app/api/admin_libraries.py
 M app/config.py
 M app/mcp_adapter/cli.py
 M app/mcp_adapter/client.py
 M app/mcp_adapter/config.py
 M app/mcp_adapter/server.py
 M app/schemas/evidence_locator.py
 M app/services/cleanup.py
 M app/services/import_parsing.py
 M app/services/import_uploads.py
 M app/services/ocr.py
 M app/services/parser_units.py
 M app/services/pdf_extract.py
 M app/services/sync_sources.py
 M app/workers/embedder.py
 M app/workers/importer.py
 M docs/AGENT_README.md
 M docs/README.md
 M docs/decisions.md
 M docs/mcp-knowledge-adapter.md
 M docs/roadmap/pdf-understanding-improvement-plan-20260918.zh-CN.md
 M tests/conftest.py
 M tests/test_file_resource_persistence.py
 M tests/test_import_pipeline.py
 M tests/test_import_upload_inbox_1a.py
 M tests/test_import_upload_preflight_1b.py
 M tests/test_importer_inbox_1a.py
 M tests/test_mineru_pdf.py
 M tests/test_ocr.py
 M tests/test_pdf_extract.py
 M tests/test_query_import_api.py
 M tests/test_user_upload_tasks.py
 M tests/test_v09_mcp_adapter.py
?? .claude/skills/
?? .pi/
?? .tmp-upload-observability-rollforward-20260921/
?? AGENTS.md
?? CLAUDE.md
?? admin-ui/sample-files/
?? app/mcp_adapter/auth.py
?? app/services/pdf_quality_inspector.py
?? app/services/pdf_resource_recovery.py
?? deploy/Dockerfile.artifact-runtime
?? deploy/Dockerfile.classification-runtime
?? deploy/Dockerfile.error-messages-ui
?? deploy/Dockerfile.error-messages-ui-r2
?? deploy/Dockerfile.file-resource-link-fix
?? deploy/Dockerfile.graph-manual-candidate
?? deploy/Dockerfile.graph-manual-candidate-r2
?? deploy/Dockerfile.graph-manual-candidate-r3
?? deploy/Dockerfile.graph-manual-candidate-r4
?? deploy/Dockerfile.graph-manual-candidate-r5
?? deploy/Dockerfile.mcp-guide-https
?? deploy/Dockerfile.mcp-https
?? deploy/Dockerfile.my-files-tasks
?? deploy/Dockerfile.navigation
?? deploy/Dockerfile.netdisk-preflight-persistence
?? deploy/Dockerfile.retry-monitored-tasks-header
?? deploy/Dockerfile.retry-response-refresh
?? deploy/Dockerfile.reupload-cancel-failed-chain
?? deploy/Dockerfile.upload-dedupe-failed-files
?? deploy/Dockerfile.worker-exclude-pdf
?? deploy/dry_run_recover_upload_fk_objects.py
?? deploy/inspect_embedding_queue.py
?? deploy/inspect_embedding_workers.py
?? deploy/inspect_one_upload_job.py
?? deploy/inspect_recent_import_failures.py
?? deploy/inspect_retryable_upload_fk_jobs.py
?? deploy/inspect_uploader_upload_scope.py
?? deploy/inspect_worker_capacity.py
?? deploy/inspect_yesterday_import_failures.py
?? deploy/inspect_yesterday_missing_source_uploaders.py
?? deploy/patch_file_resource_link.py
?? deploy/patch_pdf_resource_recovery.py
?? deploy/patch_retry_monitored_tasks_header.py
?? deploy/patch_reupload_failed_chain.py
?? deploy/patch_worker_exclude_pdf.py
?? deploy/pause_embedding_worker.py
?? deploy/publish_error_messages_ui_r2.sh
?? deploy/publish_file_resource_link_fix.py
?? deploy/publish_mcp_https.sh
?? deploy/publish_retry_monitored_tasks_header.py
?? deploy/publish_worker_exclude_pdf.py
?? deploy/reconcile_reuploaded_failed_tasks.py
?? deploy/recreate_container_with_env.sh
?? deploy/retry_one_upload_fk_job.py
?? deploy/run_pdf_resource_recovery.py
?? deploy/verify_file_resource_link_fix.py
?? deploy/verify_pdf_resource_recovery.py
?? docs/roadmap/pdf-cascading-quality-gating-plan-20260922.zh-CN.md
?? docs/roadmap/pdf-production-controlled-trial-plan-20260924.zh-CN.md
?? docs/roadmap/pdf-q0-ground-truth-review-20260923.zh-CN.md
?? docs/roadmap/pdf-resource-recovery-20260927.zh-CN.md
?? outputs/
?? p3_addr_area.png
?? p3_name_area.png
?? s3_p3_fuji_crop.png
?? s3_p3_fuji_text_only.png
?? s3_p3_top_crop.png
?? sample-files/
?? scripts/evaluate_pdf_cascade.py
?? scripts/profile_pdf_pipeline.py
?? tests/fixtures/
?? tests/test_evaluate_pdf_cascade.py
?? tests/test_library_delete_file_cleanup.py
?? tests/test_pdf_quality_fixtures.py
?? tests/test_pdf_quality_inspector.py
?? tests/test_pdf_recovery_verification.py
?? tests/test_pdf_resource_recovery.py
?? tests/test_sync_source_cascade_delete.py
?? tests/test_worker_trial_isolation.py
```

最新提交：`c8e1dc7 docs: project baseline documentation and admin UI styles`。本轮未暂存或提交。

## 本窗口变更

- 新增 `deploy/verify_pdf_resource_recovery.py` 与 `tests/test_pdf_recovery_verification.py`。
- 仅验收工具：精确快照和目标库、当前 revision 的 embedding、结构化页覆盖、当前 revision 的 chunks/Qdrant 对应关系。
- PostgreSQL 强制只读事务；Qdrant 只读 scroll 且不返回原文或向量；stdout 仅汇总，明细写服务器审计文件。
- 区分 complete/incomplete/processing/failed/unverified，绝不把 ready 或直接 embedding 链接为空作为充分结论。
- 源状态/hash 在向量扫描后复核；使用真实 unit_kind/source_kind 校验覆盖报告。
- 现有解析器、配置、Worker、恢复 runner、快照和尝试次数均未修改。
- GitNexus 新入口 UNKNOWN（未入索引），文本确认仅新工具及测试使用；复用覆盖函数 upstream 为 LOW，未修改该共享函数。
- 仅做一次审查。当前版本 Qdrant 验收不扩大为删除或审计所有历史版本向量。

## 验证证据

已运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_pdf_recovery_verification.py tests/test_pdf_resource_recovery.py tests/test_pdf_extract.py
# 90 passed in 2.44s（包含真实 Library 表名断言）
.\.venv\Scripts\python.exe -m py_compile deploy/verify_pdf_resource_recovery.py
# exit 0
git diff --check
# exit 0；仅现有 LF/CRLF 提示
```

- 数据库编排和 Qdrant 为合成数据/mock 测试，不是生产验收。
- 有效上传文件：`recovery-build/verify_pdf_resource_recovery-20260928-a377b9deac24.py`。
- 本地固定副本与服务器 `sha256sum` 一致：`a377b9deac24f4c6a8eef896072944e474f327b4172f1924d48bc157ff6582ee`。
- 首次上传后检测到本地表名修正为 `sys_libraries`；对照 Library 模型确认正确，保留修正并补充回归断言后重新上传。旧 `132ed25a68a8` 版本已废弃，切勿执行。两份文件均未运行，未构建新镜像或替换常驻服务。

未运行 / 未验证：338 项实时业务汇总、两个短样本的当前版本内容与向量验收、长样本失败原因、33 项 remaining 恢复、OCR 原文准确度抽查。

## 运行与外部状态

- 短样本恢复容器：exit 0，OOM false，重启 0。数据成功与全文完整性在本窗口未重新验证。
- 687 页长样本恢复容器：**2026-09-28 01:44:16 UTC 退出 1，OOM false，重启 0**。不能再描述为仍在解析，尚不能确定新失败原因。
- Finance importer/embedder 与 release API 在定向查询时运行正常。
- 未启动 remaining、未重启长样本、未清理容器；保留现场。
- SSH 连接成功；容器元数据和文件哈希查询成功。`docker exec` 被 SSH Manager 的动态执行保护拦截，须面板一次性批准；没有绕过执行限制。
- 当前项目 `ScheduledTaskList` 返回空；未创建或恢复自动化，既有 pdf-2/pdf-3 在此项目视图不可独立核实。

## 漂移与风险

- 旧交接的 304 ready / 1 processing / 33 failed 不是新鲜业务结果，不能继续照报。
- 不因长样本已退出而盲目重试或重置 attempt_count；先诊断服务器内审计记录。
- 不把上传文件当作部署/执行完成，也不把 mock 测试当作生产验证。
- 原有大量未提交及未跟踪成果仍需保留；本轮还观察到另有前端测试发生变化，未修改该文件。

## 下一条安全动作

1. 经 SSH Manager 严格主机校验连接原配置后，重新执行只读命令：

   ```sh
   docker inspect --format '{{.Name}} state={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}' vector-kb-pdf-recovery-canary-small vector-kb-pdf-recovery-canary-long
   ```

2. 请求用户在 SSH Manager 面板批准服务器内诊断执行。两个恢复容器均已退出，不要重启 runner，也不要继续对退出容器使用 exec。批准后准备独立只读验收进程，沿用已有候选镜像、目标库和快照，加载已上传工具；配置值和明细仅留服务器。
3. 先核查已有 recovery audit 和长样本真实失败原因，再验收短样本当前 revision/embedding/chunks/页覆盖/Qdrant；关键内容抽查仍需另有服务器内证据。
4. 只有达到既有验收门槛后，才按原 runner 的 `--scope remaining` 恢复其余任务。保持单次保护和常驻服务不变。
