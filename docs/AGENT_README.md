# Vector Knowledge Base：给后续智能体的项目说明

> **审计基线：2026-09-17，本地提交 `8a7ffcbe`。** 这是一份“从哪里继续、哪些事实可信”的入口，不是部署成功声明。代码、Alembic 和当次测试结果优先于旧规划或历史对话；目标服务器需要单独重新核验。

## 新会话只读 5 分钟路线

1. 先读根目录 [`AGENTS.md`](../AGENTS.md)，确认 GitNexus、脏工作树、验证和授权规则；
2. 读本页的事实优先级、运行状态和 owner 定位；
3. 读 [`current-architecture.md`](./current-architecture.md) 和 [`decisions.md`](./decisions.md)，不要重新讨论已确定的边界；
4. 当前活跃任务是 **PDF 确定性解析策略（M2 收尾与路由 V1）**。先读[路线图](./roadmap/pdf-understanding-improvement-plan-20260918.zh-CN.md)，再读本机 [Pi 计划](../.pi/plan/确定性-pdf-解析策略实施计划-20260920-0017.md)与 [Trellis 任务](../.trellis/tasks/09-20-deterministic-pdf-parsing/)；
5. 仅在确定受影响 owner 后才读代码、测试或历史计划；验收使用 [`quality/verification-matrix.md`](./quality/verification-matrix.md)，转交前遵循 [`handoffs/README.md`](./handoffs/README.md)。

活跃任务的阶段状态是：M2 的确定性路由与资源/结构一致性收尾仍需独立验收；M3 发布、扩容或生产启用均不在当前授权范围。工作树中已有相关未提交改动，必须先做影响分析和新鲜验证，不能因路线图写有“实施回写”而假设它已正确或可发布。

用户另行授权的 338 份历史 PDF 恢复见 [2026-09-28 交接](./handoffs/2026-09-28-pdf-backlog-recovery.md)：已通过执行审批开展独立恢复与服务器只读核验，当前仍在处理。687 页文件先后证实渲染 PNG 字节超限及第 609 页空链接动作触发 pypdf 异常；针对性候选只在临时恢复环境使用。旧的“未执行/审批阻塞”描述属于历史快照，不可再据此报告状态。最终入库数、页面覆盖与当前版本向量以本任务新鲜服务器审计为准；入库成功不等于全部视觉内容或 OCR 文字正确。此授权不扩展上述默认 M3 边界。

后续效率改进已有[单页扫描 PDF 后验路由实施说明](./roadmap/pdf-cascading-quality-gating-plan-20260922.zh-CN.md)：步骤与验收门槛已固定，代码和性能仍未验证。先完成当前 M2/V1 验收；不要把实施说明、被 Git 忽略的本机样本记录或后续任务状态当作已实施证据。

## 先读这一页：项目是什么

这是一个面向组织内部资料的**知识资产与检索服务**。用户把文件上传到某个知识库；系统可靠保存原文件、异步解析和向量化，按库级权限返回检索结果，并向 Dify 兼容接口、管理后台、上层 Agent 或办公系统提供受控访问。

它不是通用的“聊天应用编排器”。上层应用负责工作流和最终回答；本项目负责文件、证据、检索、知识图谱及其治理边界。管理后台的 Chat 是已实现的一个消费界面，不应被误解为全部产品边界。

## 当前事实的优先级

发生冲突时，按以下顺序判断：

1. 当前 checkout 的代码、`alembic/versions/`、测试和目标环境的实时输出；
2. 最近提交和本文件所列的已核验实现；
3. 有日期、带实施回写的规划文档；
4. 普通 README、设计稿、历史运行记录和对话摘要。

特别注意：根目录 [`README.md`](../README.md) 仍写着迁移 head `0062`（核对日期 2026-08-29）；本地 `alembic heads` 在本次审计中实际输出 **`0076 (head)`**。`v0.x`、`P*`、`M*` 是阶段/设计标签，不等于 Python 包版本；包版本仍是 `0.1.0`。

不要把“代码存在”当作“生产已启用”，也不要把某次聊天中报告的服务器状态当作现在仍成立的部署事实。

## 当前本地运行状态

本次只读检查（2026-09-17 22:58，`scripts/status_local.ps1`）显示：API、DOC Converter、Importer、Embedder、Cleanup、Graph Extractor、Knowledge Artifact、Classification Worker 均为 **DOWN**；`/health` 不可访问。端口 `5599` 有静态预览，但它不是业务 API。

这不是故障结论，更不是远程服务器状态。需要运行项目时先检查配置与依赖，再执行：

```powershell
.\scripts\status_local.ps1
.\scripts\start_local.ps1
```

脚本将启动 API、DOC Converter、Importer、Embedder、Cleanup；图谱抽取、知识产物和分类 worker 是否启动取决于 `.env` 的 feature gate。停机只用 `.\scripts\stop_local.ps1`，它按本项目 PID 文件识别进程。

## 系统结构与主链路

```text
管理后台 / Dify / 上层 Agent / MCP 客户端
                 │
                 ▼
        FastAPI（app/main.py，/console 挂载零构建后台）
                 │
     ┌───────────┼─────────────────────┐
     ▼           ▼                     ▼
PostgreSQL    Qdrant              受控对象存储
身份/权限/      每库 collection      local / MinIO / OSS
任务/审计/状态  向量检索               原始 FileResource
     │
     ▼
Importer → Embedder → 可选 Graph / Artifact / Classification worker
```

后端总入口是 [`app/main.py`](../app/main.py)。启动期会验证危险配置、能力开关和对象存储；远端对象存储不可用时会拒绝启动，而本地开发默认存储路径的检查较宽松。管理后台是直接由 [`admin-ui/`](../admin-ui) 静态托管的 Vue 代码，没有单独前端打包步骤。

最重要的数据路径是：

```text
选择文件/文件夹
  → 分块上传会话与完整性校验
  → FileResource 已验证保存（available）
  → DocumentImportJob queued / processing / succeeded|failed
  → 解析、切块、Embedding、Qdrant
  → 可选图谱、知识产物、分类
```

`FileResource.storage_status` 与知识处理状态刻意分离：原文件已可靠保存，不等于已成功解析或向量化。处理失败不得把已保存文件误报为丢失，也不能让 worker 删除长期原文件。

## 已实现能力与启用边界

| 域 | 主要 owner | 本地代码事实 | 启用/边界 |
| --- | --- | --- | --- |
| 身份、API Key、库级授权 | `app/auth/`、`app/casbin/`、`app/api/me.py` | JWT Cookie、API Key、Casbin `(user, library, action)`；组织授权实现也已存在 | `organization_authorization_enabled` 默认 `false`；权限修改属于高风险边界 |
| 知识库、文档与摄入 | `app/api/documents.py`、`app/services/import_uploads.py`、`app/workers/importer.py` | 多格式解析、上传会话、异步任务、revision/outbox 清理、目录处理 | 入口、文件尺寸和格式受配置约束；不能假设所有格式或 provider 已配置 |
| 原文件与“我的文件” | `app/models/file_resource.py`、`app/services/file_resources.py`、`app/api/me.py`、`admin-ui/src/views/MyFiles.js` | `0076` 引入长期 `FileResource`；可列出本人原文件、按相对路径成树、下载短时 URL、删除符合条件的文件/文件夹 | “已保存”和“已处理”是双状态；对象存储 provider、下载和删除均需重新做库/用户范围校验 |
| 向量检索 | `app/services/retrieval.py`、`app/api/retrieval.py` | 每库 Qdrant collection、Dify 兼容 `/retrieval`、过滤、可选 rerank | Dense 是基础路径；Hybrid、query rewrite、chat 等不能仅凭代码存在就当作默认启用 |
| 图谱与治理 | `app/services/graph_*`、`app/api/v03_graph.py` 至 `v07_entity_linking.py` | schema、抽取、候选、发布、治理、catalog、检索、实体链接及 P1/P2/P3 演进代码都存在 | 大多由 `graph_*_enabled` 开关控制，默认 `false`；图谱写入、迁移和治理操作均不是普通文档改动 |
| 知识产物与分类 | `app/services/knowledge_artifact_*`、`classification_*` | worker、模型 provider、taxonomy、分类决策与后台 API 已实现 | runtime/auto-trigger/external model 默认关闭，须验证模型、配额和权限 |
| 扩展 API | `federated_retrieval.py`、`public_v1.py`、`organizations.py`、`external_graph_sync.py`、`mcp_adapter/` | 跨库检索、公开 API、组织、外部图谱同步与 MCP 适配器均已存在 | 对应 gate 默认关闭或有单独启动契约；不可据此宣称对外发布 |
| 管理后台 | `admin-ui/src/` | 上传、我的文件、知识目录、搜索、检索测试、图谱、分类、运营和 Chat 等页面已存在 | 前端测试是独立 Node 脚本；静态预览不能证明真实 API 行为 |

`.env`、任何密钥、服务器地址、数据库数据、对象存储对象和日志都不是本说明的输入或输出。不要把它们复制到文档、测试夹具或聊天内容。

## 规划—代码核对

| 规划/设计 | 与代码的核对结论 | 后续动作 |
| --- | --- | --- |
| [`49-upload-read-reliability-optimization-plan.md`](49-upload-read-reliability-optimization-plan.md) | 文档自称 3C `in_progress`，且当时限定“不新增 schema/对象存储”；当前代码已越过该局部范围，拥有 `FileResource`、对象存储和“我的文件”实现。它仍是上传可靠性历史与未验证项的来源，不再是完整当前范围。 | 不要以该文的 `active_substage` 自动授权新工作；先形成新的明确阶段/验收边界。 |
| [`54` P3.0](54-p3-identity-evolution-read-only-baseline.md) | 基线已标记完成并说明局限。 | 用作身份演进的约束来源。 |
| [`55` P3.1](55-p3-1-canonical-entity-evolution-design-freeze.md) | 对应迁移 `0071/0072`、canonical evolution 服务与测试都在仓库。 | 代码存在不等于生产迁移已验证。 |
| [`56` P3.2](56-p3-2-stable-predicate-evolution-design-freeze.md) | 对应 `0073`、stable predicate evolution 服务与测试都在仓库。 | 同上；保留其 PostgreSQL 并发/回滚门槛。 |
| [`57` P3.3](57-p3-3-logical-fact-reconciliation-design-draft.md) | 文件标题仍是 draft/冻结设计，但代码已有 `0074/0075`、fact reconciliation 服务与测试。该设计文档没有因此自动变成发布验收记录。 | 任何 P3.3 发布、API/UI 接入或远程迁移，先补当前运行证据和明确授权。 |
| [`58-file-resource-processing-decoupling-plan.md`](58-file-resource-processing-decoupling-plan.md) | 原始 S1–S5 计划和后续实施回写共存；早段称 S3/S4 未实现，后段却记录本地 stored-files、下载、删除和“我的文件”。当前代码确认后段功能确实存在。 | 该文内部有时序冲突；按代码与更晚的回写解释，随后再做一次产品/部署验收。 |

本次针对文件资源、我的文件、媒体转写和 P3.1–P3.3 的聚焦 Python 回归实际运行结果为 **`134 passed`**（仅有 `starlette.testclient` 对 `httpx` 的弃用警告）。这不覆盖真实 PostgreSQL、Qdrant、对象存储、模型服务、浏览器或目标服务器。

## 发现的文档债务与“无细化真源”代码

- 根 README 的迁移编号、文档计数和若干“当前”描述已过期；它适合作为产品介绍，不可单独作为实现/部署依据。
- `docs/58` 的原始子阶段表与后续实施回写相互矛盾；必须标明时间点，不能只引用其中一段。
- 最新提交 `8a7ffcbe`（2026-09-17）一次改动了 103 个文件，混合了文件资源、图谱、知识产物、分类、媒体转写、MinIO 与后台 UI。后续改动必须按实际 owner 拆分核查，不能把这次 checkpoint 视为一个单一、已完整验收的产品阶段。
- `graph_candidate_manual_approval` 已有实现入口，但在本次审计的规划文档中未找到同等细化的当前阶段真源；启用或扩展前应先补边界、权限、审计和验收说明。
- 视频/音频转写和 MiniMax 兼容策略已有代码与 [`docs/05-configuration.md`](05-configuration.md) 配置说明，但未找到独立的、面向真实部署的当前验收记录。默认不启用，不能把它当已上线功能。

这些是“需要补真源或验收”的条目，不表示应删除已经存在的代码。

## 给智能体的安全续接规则

1. 先读根目录 [`AGENTS.md`](../AGENTS.md)，再读本页和本次要碰的规划/测试。它要求修改函数、类或方法前做 GitNexus impact analysis；若影响结论为 `UNKNOWN`，必须用文本搜索继续确认，不能把空调用方当安全。
2. 写代码前执行 `git status --short`。本审计开始时已有未跟踪的 `.claude/skills/`、`AGENTS.md`、`CLAUDE.md` 和若干 `deploy/` 草稿；它们属于用户工作，除非请求明确涉及，绝不覆盖、清理、暂存或提交。
3. 数据库事实以 `alembic heads` 和**目标环境**的 `alembic current` 为准。迁移、远程配置、部署、对象删除和外部 provider 写入都需要单独授权与恢复方案。
4. feature gate 为 `false` 只说明默认不开；开启它之前必须核验配置、依赖、provider、权限和实际 worker。反过来，代码/迁移存在也不表示远程环境已同步。
5. 后端测试使用 `\.venv\Scripts\python.exe -m pytest -q <target>`；前端使用对应的 `node admin-ui/<test>.mjs`。先跑受影响的窄测试，再决定是否扩大。不要复述历史“通过数”代替本次运行结果。
6. 文档改动也遵循事实优先：保留历史计划，不悄悄篡改；新记录应明确标为“规划”“本地代码”“本地测试”“目标环境验证”之一。

## 常用定位图

| 想理解/修改的内容 | 先看 |
| --- | --- |
| 应用路由与启动校验 | `app/main.py`、`app/config.py` |
| 上传、分块、重试、状态投影 | `app/services/import_uploads.py`、`app/api/import_uploads.py`、`app/workers/importer.py` |
| 原文件、下载、删除、我的文件 | `app/models/file_resource.py`、`app/services/file_resources.py`、`app/api/me.py`、`admin-ui/src/views/MyFiles.js` |
| 解析/媒体转写 | `app/services/import_parsing.py`、`app/services/video_transcription.py`、`app/workers/doc_converter.py` |
| 文档检索 | `app/services/retrieval.py`、`app/api/retrieval.py`、`docs/10-retrieval-api.md` |
| 图谱 | `app/services/graph_*`、`app/api/v03_graph.py` 至 `v07_entity_linking.py`、`docs/54`–`57` |
| 数据模型与演进 | `app/models/`、`alembic/versions/`、`docs/14-database-schema.md` |
| 管理后台 | `admin-ui/src/app.js`、`admin-ui/src/views/`、对应 `*.test.mjs` |
| 部署与恢复 | `deploy/`、`scripts/`、`docs/15-deployment.md`、`docs/39-v0.9-supported-deployment.md` |

## 当前任务与下一安全动作

不要自动进入“下一阶段”。当前默认入口是 PDF 确定性解析策略：先用 GitNexus 核对 `mineru_pdf.py`、`import_parsing.py`、`pdf_preflight.py`、`pdf_routing.py`、`pdf_coverage.py` 及其测试的真实变更与影响范围；再按活跃任务列出的顺序重跑资源边界、同步/异步结构一致性和目录 UI 验收。M3、生产部署、白名单扩张、Unlimited-OCR 恢复和页级跨引擎合并均须另行授权。

非 PDF 请求则保持受限目标：本地开发先恢复并验收受影响链路；部署先做目标环境只读盘点；图谱演进先补 P3.3 的运行证据；文件可靠性先区分“已保存”“已处理”“用户可见/删除”的验收状态。

本页只建立续接基线；它不授权代码、数据库、服务器或第三方系统的任何写操作。
