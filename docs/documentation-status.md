# 文档状态清单

> 更新日期：2026-10-07（发布状态与仓库同步）。历史计划保留原时点，当前结论以对应新鲜核验为准。

## 状态含义

- **当前入口**：新会话优先阅读，且明确了证据边界；
- **当前专题**：适合定位某个 owner，但仍需以代码/测试/环境核验；
- **当前任务**：只约束当前已确认工作，不等于已发布；
- **历史/专题证据**：保留当时判断，不能直接推出当前部署或验收；
- **需复核**：包含已知旧时点、已失效的事实或候选工作树内容，使用前必须重新验证。

## 优先阅读

| 文档 | 状态 | 用途 |
| --- | --- | --- |
| [`quality/deployment-gap-status-20261007.md`](./quality/deployment-gap-status-20261007.md) | 当前发布核验入口 | 按角色区分已上线、待发布和未验证，记录本次 Git 归档范围；不等于启用 PDF 开关。 |
| [`../AGENTS.md`](../AGENTS.md) | 当前入口 | 会话规则、GitNexus、脏工作树和授权边界 |
| [`AGENT_README.md`](./AGENT_README.md) | 当前入口 | 当前代码/运行基线、风险和下一安全动作 |
| [`current-architecture.md`](./current-architecture.md) | 当前入口 | 主链路、owner 和运行边界 |
| [`decisions.md`](./decisions.md) | 当前入口 | 长期设计决策与非目标 |
| [`quality/verification-matrix.md`](./quality/verification-matrix.md) | 当前入口 | 本次验收应取得的证据 |
| [`handoffs/README.md`](./handoffs/README.md) | 当前入口 | 跨会话交接格式 |

## 专题文档的适用范围

| 文档或组 | 状态 | 使用规则 |
| --- | --- | --- |
| `05-configuration.md`、`06-authentication.md`、`07-permissions.md`、`08-libraries.md`、`09-document-ingest.md`、`10-retrieval-api.md`、`11-worker.md`、`12-admin-ui.md` | 当前专题 | 先以当前 owner 与测试确认字段、路由、默认值和实际启用状态。 |
| `13-api-reference.md` | 当前专题 | 把它当定位目录；修改接口前以 router/schema 和契约测试为准。 |
| `14-database-schema.md` | 需复核 | 仍含 `0060` 等旧迁移时点；schema 以 models、Alembic 和目标环境为准。 |
| `15-deployment.md`、`27-backup-restore-runbook.md` | 需复核 | 含特定部署假设；`deploy/` 中候选 Dockerfile 不自动成为官方路线。 |
| `16-testing.md` | 需复核 | 保留测试分层说明；通过数、测试集和命令覆盖范围必须以本次执行为准。 |
| `02-architecture.md`、`03-project-structure.md` | 历史/专题证据 | 用于历史设计和目录定位；当前系统地图以 `current-architecture.md` 为入口。 |
| `39-v0.9-*`、`mcp-knowledge-adapter.md`、`external-graph-sync.md`、`public-api-*.md` | 当前专题 / feature-gated | 仅在相应能力、权限和部署路线已被明确选择时阅读。 |
| `30`–`34`、`40`–`47`、`49`–`58`、`superpowers/` | 历史/专题证据 | 有日期的设计、评估或验收材料；必须保留原时点，不能替代当前代码或环境证据。 |
| `roadmap/pdf-understanding-improvement-plan-20260918.zh-CN.md` 与 `.pi/plan/确定性-pdf-解析策略实施计划-20260920-0017.md` | 当前任务 | PDF M2 收尾与确定性路由 V1 的范围和非目标；不授权 M3/生产部署。 |

## 文档维护规则

1. 发现“当前”字段与代码、迁移或环境冲突时，先在本页标记 `需复核`，不要篡改历史记录。
2. 新的跨 owner 架构或长期约束先更新 `decisions.md`，详细机制只更新一个专题 owner。
3. 新功能在实施前至少有任务记录、owner、范围、非目标和验收路径；完成时在 handoff/验收记录写入新鲜证据。
4. 文档不记录 `.env`、凭据、服务器地址、真实业务样本、完整 provider payload 或用户数据。
