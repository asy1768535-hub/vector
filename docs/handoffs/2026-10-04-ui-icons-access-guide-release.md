# 图标与完整 API/MCP 指南发布交接（2026-10-04）

工作目录：本项目根目录。分支：`codex/sync-server-20260917`；HEAD：`2c3a646`。
目标环境：既有正式 API，已通过本窗口新鲜验证。用户明确授权部署这两项；多库检索不动。

## 发布范围与保留

仅叠加 5 个前端文件：`admin-ui/src/icons.js`、`admin-ui/src/views/ApiKeys.js`、`admin-ui/src/api_access_guide.js`、`admin-ui/src/components/ApiAccessGuide.js`、`admin-ui/style.css`。

icons 仅补 9 个缺失字段，原有 64 个图标渲染保持一致；CSS 保留服务器原 219899 字节，追加 19 条指南规则。ApiKeys 生产差异仅指南集成，保留已发布遮罩、完整复制和撤销行为。服务器源码树与基础镜像一致；候选镜像与生产仅这 5 文件不同，所有后端源码一致。

未发布多库检索候选、PDF 级联或问答取证规划，未改迁移、权限、业务数据、对象、向量、网关、Worker 或 MCP 服务。

## 本窗口验证

- `node --test` 图标 8 套件及 API Key 4 套件：184 passed / 0 failed。
- 既有 `verify_examples.py`：6 个 HTTP JSON 请求体、全部 13 个 MCP arguments 通过；没有执行业务工具。
- 4 个候选 JavaScript 文件 `node --check` 通过；`validate_candidate.mjs`：9 个新图标与本地成果一致，64 个现有图标与生产基线一致。
- 发布前 hash、归档传输 hash、镜像父层及完整源码树检查通过。
- 切换后：环境变量值、命令、入口、用户、工作目录、标签、挂载、端口、网络及运行控制一致；其他本项目容器 ID/镜像/状态不变。
- 正式 HTTP 读取 5 文件均 200 且 SHA-256 与候选一致；9 个图标出现、完整指南接入，API Key 遮罩及隐藏撤销逻辑保留；PDF 人工复核两条接口保留。
- 独立复核 ready，RestartCount=0。未观察到验证失败或自动回退。
- 未验证：真实用户登录浏览器逐页操作、系统剪贴板内容。指南不代表已修复既有 MCP 上传适配问题。

## 运行与恢复

已发布镜像：`vector-kb-app:ui-icons-guide-20261004-r1`。
旧 API 保留为停止容器：`vector-kb-api-release-rollback-ui-icons-guide-20261004-r1`。禁止在旧容器清理中自动删除。

如需代码回退：停止本次新 API，将其改名为独立故障版本以保留证据，再将上述旧容器恢复为 `vector-kb-api-release` 并启动，核对 ready。回退仅影响本次前端更新，不修改业务数据；本窗口没有实际触发回退。

本机[任务记录](../../.trellis/tasks/10-04-ui-icons-access-guide-release/implement.md)保留候选 manifest、独立差异、脚本及 deployment.json；没有暂存或提交 Git。

## 当前 Git 快照

以下包含用户既有未提交成果，不代表本次发布范围；禁止整体部署 dirty checkout。

```text
 M README.md
 M admin-ui/api_keys_redesign.test.mjs
 M admin-ui/retrieval_test_console.test.mjs
 M admin-ui/src/api.js
 M admin-ui/src/app.js
 M admin-ui/src/catalog_ui.js
 M admin-ui/src/menu_access.js
 M admin-ui/src/menu_access.test.mjs
 M admin-ui/src/retrieval_test_ui.js
 M admin-ui/src/views/ApiKeys.js
 M admin-ui/src/views/Permissions.js
 M admin-ui/src/views/RetrievalTest.js
 M admin-ui/style.css
 M app/api/admin_permissions.py
 M app/api/federated_retrieval.py
 M app/api/knowledge_catalog.py
 M app/casbin/enforcer.py
 M app/schemas/evidence_locator.py
 M app/schemas/federated_retrieval.py
 M app/schemas/knowledge_catalog.py
 M app/schemas/organizations.py
 M app/services/docx_extract.py
 M app/services/federated_retrieval.py
 M app/services/federated_retrieval_contracts.py
 M app/services/import_parsing.py
 M app/services/import_upload_preflight.py
 M app/services/knowledge_catalog.py
 M app/services/parser_units.py
 M app/services/pdf_coverage.py
 M app/services/pdf_extract.py
 M app/services/pdf_resource_recovery.py
 M app/services/public_v1.py
 M app/services/qdrant.py
 M app/services/rebuild.py
 M app/services/xlsx_extract.py
 M app/workers/embedder.py
 M app/workers/importer.py
 M docs/AGENT_README.md
 M docs/README.md
 M docs/decisions.md
 M docs/documentation-status.md
 M docs/mcp-knowledge-adapter.md
 M docs/roadmap/pdf-understanding-improvement-plan-20260918.zh-CN.md
 M tests/test_docx_extract.py
 M tests/test_evidence_locator.py
 M tests/test_pdf_resource_recovery.py
 M tests/test_rebuild_external_guard.py
 M tests/test_v08_federated_retrieval.py
 M tests/test_v08_federated_retrieval_api.py
 M tests/test_v08_knowledge_catalog.py
 M tests/test_worker_trial_isolation.py
 M tests/test_xlsx_extract.py
?? admin-ui/api_keys_visibility.test.mjs
?? admin-ui/independent_multisearch.test.mjs
?? admin-ui/member_multisearch.test.mjs
?? admin-ui/pdf_blank_coverage.test.mjs
?? admin-ui/permission_organization_roles.test.mjs
?? admin-ui/src/api_access_guide.js
?? admin-ui/src/components/ApiAccessGuide.js
?? app/services/pdf_coverage_reviews.py
?? check_active_detail.py
?? check_flow_pdf.py
?? check_g1_physical.py
?? deploy/mcp_gateway.conf.template
?? deploy/mcp_gateway_location.conf.template
?? deploy/switch_mcp_domain.py
?? deploy/switch_pdf_automatic_batches.py
?? deploy/verify_pdf_automatic_batches.py
?? deploy/verify_pdf_batch_publication.py
?? docs/handoffs/2026-09-29-member-multisearch.md
?? docs/handoffs/2026-09-29-organization-role-permissions.md
?? docs/handoffs/2026-09-29-pdf-blank-page-evidence.md
?? docs/handoffs/2026-09-30-mcp-domain-switch.md
?? docs/quality/api-key-visibility-20260930.md
?? docs/quality/api-mcp-access-guide-20260930.md
?? docs/quality/chat-evidence-plan-review-20260930.md
?? docs/quality/independent-multisearch-plan-review-20260930.md
?? docs/quality/member-multisearch-20260929.md
?? docs/quality/permission-apikey-release-20260930.md
?? docs/quality/permission-policy-freshness-20260930.md
?? docs/quality/project-container-cleanup-20260930.md
?? docs/quality/role-downgrade-visibility-20260929.md
?? docs/roadmap/chat-evidence-reliability-plan-20260929.zh-CN.md
?? docs/roadmap/independent-multisearch-plan-20260930.zh-CN.md
?? docs/roadmap/pdf-automatic-page-batches-20260929.zh-CN.md
?? docs/roadmap/pdf-blank-page-coverage-20260929.zh-CN.md
?? inspect_35_active.py
?? inspect_b1.py
?? inspect_b2.py
?? inspect_g1_remaining.py
?? tests/test_admin_permission_organization_roles.py
?? tests/test_casbin_policy_freshness.py
?? tests/test_pdf_blank_coverage.py
?? tests/test_pdf_blank_pages.py
?? tests/test_pdf_coverage_reviews.py
?? tests/test_public_federated_partial.py
?? view-screenshot.html
?? watch_g3_fast.py
```

## 下一条安全动作

先执行 `git status --short`，再对照本交接及候选 manifest；后续用户浏览器核验 API/MCP 指南打开与复制、导入/图谱/审计图标。其他任务继续遵循各自实施与发布边界，不因此次部署扩展授权。
