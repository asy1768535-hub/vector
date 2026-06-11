# 文档索引

按功能拆分；每篇都自洽，可独立阅读。建议新人按 1→4 的顺序读完核心概念后，再按需查阅。

## 项目总览

| # | 文档 | 适合谁 |
|---|---|---|
| 01 | [项目介绍](./01-introduction.md) | 第一次接触 |
| 02 | [架构总览](./02-architecture.md) | 想知道系统怎么搭起来的 |
| 03 | [项目结构](./03-project-structure.md) | 想知道每个文件夹/文件是干嘛的 |
| 04 | [快速开始](./04-quickstart.md) | 想 10 分钟跑起来 |
| 05 | [配置说明](./05-configuration.md) | 改 `.env` 之前必读 |
| **18** | **[使用说明（操作手册）](./18-usage-guide.md)** | **日常怎么用 ← 推荐先看这篇** |

## 核心功能

| # | 文档 | 主题 |
|---|---|---|
| 06 | [认证系统](./06-authentication.md) | fastapi-users JWT cookie + API Key 双通道 |
| 07 | [权限系统](./07-permissions.md) | Casbin RBAC，(user, library, action) 三元组 |
| 08 | [库管理](./08-libraries.md) | 多租户隔离：每库一个 Qdrant collection |
| 09 | [文档摄入](./09-document-ingest.md) | 切分 / 入队 / 幂等 / 异步 embed |
| 10 | [检索接口](./10-retrieval-api.md) | Dify external knowledge base spec |
| 11 | [Embedding Worker](./11-worker.md) | `FOR UPDATE SKIP LOCKED` 队列消费 |
| 12 | [管理后台 UI](./12-admin-ui.md) | 零构建 Vue 3 SPA |

## 参考资料

| # | 文档 | 用途 |
|---|---|---|
| 13 | [API 完整参考](./13-api-reference.md) | 所有 endpoint 速查 |
| 14 | [数据库 Schema](./14-database-schema.md) | 8 表 + casbin_rule 字段说明 |
| 15 | [部署 / 生产清单](./15-deployment.md) | 上线前要检查的项 |
| 16 | [测试](./16-testing.md) | 跑测试 + 加测试 |
| 17 | [常见问题](./17-faq.md) | 踩坑速查 |

---

完整架构决策记录见 `C:\Users\Administrator\.claude\plans\dify-stateless-muffin.md`。
