# 关键设计决策索引

> 更新日期：2026-09-20。这里只保存仍有效决策的结论、范围与证据入口；详细机制只在各自 owner 文档中维护。代码、迁移、测试和目标环境实时输出优先。

| 决策 | 状态 | 结论 | 详细证据 / owner |
| --- | --- | --- | --- |
| 开发真源根 | 有效 | `docs/` 是可提交的开发真源根；`.trellis/` 和 `.pi/` 是本机任务/规划空间。不得新建平行 `dev-docs/` 或改写历史计划。 | [`README.md`](./README.md)、[`AGENT_README.md`](./AGENT_README.md)、[`AGENTS.md`](../AGENTS.md) |
| 向量隔离 | 有效 | 每个知识库一个 Qdrant collection，而非共享 collection 加过滤。 | [`02-architecture.md`](./02-architecture.md) |
| 异步队列 | 有效 | Embedding/导入链路使用 PostgreSQL 队列与 `FOR UPDATE SKIP LOCKED`，不额外引入 Redis/Celery。 | [`02-architecture.md`](./02-architecture.md)、[`11-worker.md`](./11-worker.md) |
| 原文件与知识处理状态 | 有效 | `FileResource.storage_status` 与解析/向量化状态独立；已保存不等于已处理，处理失败不得误报文件丢失。 | [`AGENT_README.md`](./AGENT_README.md)、[`58-file-resource-processing-decoupling-plan.md`](./58-file-resource-processing-decoupling-plan.md) |
| Feature gate | 有效 | 默认关闭只代表未默认启用；启用前必须核验配置、依赖、provider、权限、worker 和目标环境，代码存在不表示上线。 | [`AGENT_README.md`](./AGENT_README.md)、[`05-configuration.md`](./05-configuration.md) |
| PDF 解析路由 V1 | 当前任务约束 | 使用有界预检、显式规则和文档级单一主解析器；MinerU 仅受配置与库白名单双重授权；预检未知或失败不升级远端。 | [`roadmap/pdf-understanding-improvement-plan-20260918.zh-CN.md`](./roadmap/pdf-understanding-improvement-plan-20260918.zh-CN.md)、[Pi 计划](../.pi/plan/确定性-pdf-解析策略实施计划-20260920-0017.md) |
| PDF V1 非目标 | 当前任务约束 | 不引入 Laya/Jev/LLM 路由、GPU 新模型、页级跨引擎正文拼接、数据库迁移、生产部署或白名单扩张。 | 同上 |

## 决策更新规则

- 只有影响架构、数据、权限、外部依赖、运行拓扑或长期维护边界的结论写入本页。
- 每行必须链接到唯一的详细 owner；本页不复制字段、API 或实施步骤。
- 被替代的结论保留并标记“已替代”，附新决策链接和日期；不要悄悄改写历史。
- 每次新增决策都写明状态：`有效`、`当前任务约束`、`待目标环境验证` 或 `已替代`。
