# 01 · 项目介绍

## 它是什么

**Vector Knowledge Base** 是一个通用的多租户向量知识库服务。业务方（医学、法律、内部项目…）作为独立的「库」接入，提交文本后系统自动向量化，并通过 **Dify 外部知识库标准接口**（`POST /retrieval`）查询。

## 解决了什么问题

- **每个领域都要重搭一套向量库**？→ 一套服务承载 N 个领域库，物理隔离不冲突
- **不同业务方共享同一份索引数据有风险**？→ Casbin 显式三元组授权，库与库之间完全隔离
- **接 Dify 还要自己写 SDK**？→ 直接兼容 Dify「外部知识库」标准 spec，零额外适配
- **embedding 是耗时操作，HTTP 同步阻塞用户**？→ 异步队列 + 独立 worker，提交立即返回 job

## 核心特性

| 特性 | 实现 |
|---|---|
| 多租户库隔离 | 每库一个 Qdrant collection（`lib_<slug>`，例 `lib_medical`） |
| 双通道认证 | Web 后台走 JWT cookie；外部调用走 API Key（Bearer） |
| 细粒度权限 | `(user, library, action)`：read / insert / delete，由 Casbin 落到 `casbin_rule` 表 |
| 异步 embedding | PostgreSQL `FOR UPDATE SKIP LOCKED` 队列 + 独立 worker 进程，可水平扩 |
| Dify 兼容 | `POST /retrieval` 字段严格按 Dify external knowledge base spec |
| 自助管理 | 内置 Vue 3 SPA 后台，零构建，覆盖用户/库/权限/文档/任务/审计 8 个页面 |

## 谁应该用

- **AI 应用工程师**：要在 Dify / LangChain / 自研 Agent 接入一个「带权限隔离的私有向量知识库」
- **平台团队**：要给多个业务线提供统一的向量服务，每条业务线自管文档但不能跨域看数据
- **企业内部 KB 团队**：要把医学/法律/财务/HR/客服 KB 全放进一套服务，按部门授权

## 不适合谁

- **只有一个 KB、不需要多租户**：直接用单 Qdrant + LangChain RetrievalQA 就够，本项目对你来说过重
- **要做语义路由 / Agent 编排 / RAG-as-Service**：这是检索后端，不是 RAG 框架；上层用 Dify / LlamaIndex
- **海量库（>1 万个）**：当前用 per-library collection，超过几千要切到 single-collection + library_id filter（已在 `services/qdrant.py` 留了抽象）

## 技术栈一句话

Python 3.10+ · FastAPI · SQLAlchemy 2.0 async · Alembic · Qdrant · bge-m3 · fastapi-users · Casbin · Vue 3 + Element Plus（CDN）

详见 [02 架构总览](./02-architecture.md)。
