# API Key 页完整 API / MCP 接入指南验收

最新状态（2026-10-04）：用户授权后完整指南已与图标一起部署，5 个前端文件的线上 HTTP/hash 和 ready 验证通过。见[发布交接](../handoffs/2026-10-04-ui-icons-access-guide-release.md)。真实用户浏览器与系统剪贴板仍未验证；下文保留 9 月 30 日的本地实施记录。此前 API Key 遮罩与撤销行为的发布状态以对应发布记录为准。

## 范围与入口

- API Key 页保留 HTTP API 与 MCP 两个入口；弹窗按章节展示完整指南，可折叠，每种指南均可复制全文给智能体。
- `admin-ui/src/api_access_guide.js` 是网页及复制内容的共同 owner；`admin-ui/src/components/ApiAccessGuide.js` 负责展示，`admin-ui/src/views/ApiKeys.js` 负责弹窗和复制反馈。
- 覆盖 11 个 Public v1 路由、权限补充入口、12 个固定 MCP 工具与条件上传工具、5 种 resource URI、请求参数、权限/范围、分页、返回字段、证据追溯、SSE、错误与能力边界。
- 保留用户已有 API Key 列表、遮罩复制和撤销逻辑；指南不会调用业务接口，复制内容不读取实际密钥。
- 本机任务：[PRD](../../.trellis/tasks/09-30-api-mcp-access-guide/PRD.md)、[设计](../../.trellis/tasks/09-30-api-mcp-access-guide/design.md)、[实施记录](../../.trellis/tasks/09-30-api-mcp-access-guide/implementation.md)。这些执行文件不提交。

## 契约核验与限制

对照 `app/schemas/public_v1.py`、`app/schemas/graph_catalog.py`、`app/schemas/knowledge_catalog.py` 和 MCP 注册/客户端代码，并只读核验当前部署的路由、工具和相关配置。未调用真实上传或其他写入操作。

- 明确 REST 的平铺请求体与 MCP `request` 包装；实体/关系详情核心对象为 `entity.entity` / `relation.relation`。
- 图谱目录默认 `publication_state=all`，正式事实示例显式使用 `published` 和 `active`。
- 检索与问答检查 text 通道，跨库图谱目录检查 graph 通道；验证 200 后仍需读取兼容结果。
- SSE 只有 `result` 表示最终成功；delta、HTTP 200 或 EOF 均不能单独证明回答完成。
- 当前已注册上传工具与异步导入任务响应存在适配不完整：文件可能已入队但 MCP 报 `upstream_invalid_response`。指南明确建议网页上传，并要求先检查任务再重试。未真实上传验收、未在此任务修复适配器。
- 本地联邦部分失败保护与线上存在差异，指南没有承诺成功响应一定覆盖所有库。

## 新鲜验证

1. `node --test admin-ui/api_keys_visibility.test.mjs admin-ui/api_keys_redesign.test.mjs admin-ui/api_key_creation_contract.test.mjs admin-ui/read_state_batch_b.test.mjs`：41 项通过。
2. 新增复制回归先在旧说明上失败（缺少 `list_permissions`），完整指南实现后通过；HTTP/MCP 全文及测试密钥不泄露均覆盖。
3. 6 个 HTTP JSON 请求体经对应 Pydantic 模型校验；13 个 MCP arguments 与实际注册工具的 inputSchema 校验通过，工具名集合一致。仅创建本地 server 对象读取 schema，未执行工具。
4. 本机可复核 schema：在项目根执行 `$env:PYTHONPATH='.'; .\.venv\Scripts\python.exe .trellis/tasks/09-30-api-mcp-access-guide/verify_examples.py`。
5. 本地真实 Vue / Element Plus 页面：桌面 HTTP 指南和 MCP 指南可打开；375 × 812 下正文无横向溢出、章节可折叠、底部复制按钮可见。预览数据为无效测试值，未连接业务服务。
6. 两个复制按钮实际点击后均显示成功提示；内置浏览器剪贴板读取接口未返回内容，因此系统剪贴板最终落地内容仍未验证。自动化测试验证实际传给复制函数的全文，不将成功提示代替剪贴板内容验证。
7. JavaScript 语法检查和 `git diff --check` 通过。

## 影响与恢复

GitNexus upstream impact：`copyMcpInstructions` LOW，直接调用者为页面 setup，未解析到其他流程；`setup` 和说明常量 UNKNOWN，已通过页面路由与模板/函数文本引用确认动态调用，未把空调用集视为未使用。测试源常量同样按 UNKNOWN 补查引用。新增指南函数尚未入索引时的 UNKNOWN 已通过导入和调用点补查。

仅修改接入说明、展示和复制处理。恢复时仅撤回本任务指南集成与新增文件，不能整文件覆盖已有 API Key 交互或其他任务的样式/测试改动。不涉及迁移、权限变更、真实密钥恢复或生产数据写入。部署仍需单独授权；本记录不代表已发布。
