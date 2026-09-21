# 当前架构与 Owner 地图

> 更新日期：2026-09-20。本页是代码结构的续接入口，不是目标环境运行报告。来源为 `app/main.py`、`app/config.py`、`admin-ui/src/app.js`、`alembic/versions/` 和当前测试目录；运行、部署和外部 provider 必须在目标环境另行验证。

## 产品边界

本项目是多租户知识资产与检索服务：它保存用户文件，异步解析并向量化，按库和组织权限提供检索、管理后台和受控扩展接口。它不负责上层业务工作流编排；管理后台的 Chat 只是本服务的一个消费者。

## 当前系统图

```text
管理后台 / Dify 兼容客户端 / 受控 API 客户端
                  │  Cookie / API Key / 权限检查
                  ▼
        FastAPI：app/main.py（路由、启动校验、/console）
          │                 │                     │
          ▼                 ▼                     ▼
 PostgreSQL            Qdrant               文件对象存储
 身份、权限、任务、      每库 collection      local / 远端 provider
 revision、审计、状态   向量与检索 payload      FileResource
          │
          ▼
 Importer → Embedder → 可选图谱 / 知识产物 / 分类 worker
          └→ Cleanup（删除与 Qdrant outbox）
```

API 启动时会执行安全、对象存储和多项 feature-gate 校验，并启动 Casbin 与心跳；这解释了“代码存在”不等于“某能力在当前环境已启用”。远端对象存储配置不可用时启动会拒绝继续；本地默认存储是较宽松的开发路径。

## 主链路

### 文件摄入

```text
选择文件/文件夹
  → 分块上传会话与预检
  → FileResource 已验证保存
  → DocumentImportJob 排队/处理
  → 解析、结构块、切片
  → EmbeddingJob → Qdrant
  → 可选图谱、知识产物、分类
```

原文件保存和知识处理是两个状态：`FileResource.storage_status=available` 不代表解析、向量化或可检索已成功。处理失败时不能把已保存文件描述为丢失。

### 检索与问答

```text
请求 → 身份解析 → Casbin / 库范围校验 → 检索服务
     → query embedding → Qdrant（可选 keyword / rerank / graph 路径）
     → 受控 records 或 Chat 证据上下文
```

Dense 检索是基础能力。Hybrid、query rewrite、rerank、联邦检索、图谱检索和外部同步都必须以配置、权限、依赖和目标环境证据确认，不能仅凭对应路由已注册推定可用。

## Owner 地图

| 领域 | 首要 owner | 续接时先看 |
| --- | --- | --- |
| 应用装配、启动校验、静态后台 | `app/main.py` | `app/config.py`、`tests/test_console_ui_selection.py` |
| 配置与 feature gate | `app/config.py` | `.env.example`、对应 `test_*_contract.py` |
| 身份、API Key、库级授权 | `app/auth/`、`app/casbin/` | `app/api/api_keys.py`、`app/api/me.py`、`docs/06-authentication.md`、`docs/07-permissions.md` |
| 上传、导入、原文件与个人任务 | `app/api/import_uploads.py`、`app/services/import_uploads.py` | `app/api/documents.py`、`app/api/me.py`、`app/workers/importer.py` |
| PDF/图片解析与当前路由任务 | `app/services/import_parsing.py` | `pdf_extract.py`、`pdf_preflight.py`、`pdf_routing.py`、`pdf_coverage.py`、`mineru_pdf.py` |
| 向量化与清理 | `app/workers/embedder.py`、`app/workers/cleanup.py` | embedding 服务、Qdrant cleanup outbox 模型 |
| 检索与 Chat | `app/api/retrieval.py`、`app/services/retrieval.py` | `app/api/chat.py`、`app/services/chat_answer.py` |
| 图谱、发布与实体链接 | `app/api/v03_graph.py`–`v07_entity_linking.py`、`app/services/graph_*` | `docs/54`–`57`、专题验收与 feature-gate 测试 |
| 知识产物与分类 | `app/workers/knowledge_artifacts.py`、`app/workers/classifications.py` | `app/api/classification_*.py`、相关 models/services |
| 数据模型与迁移 | `app/models/`、`alembic/versions/` | `alembic heads`；本次只读核验为 `0076 (head)` |
| 管理后台 | `admin-ui/src/app.js`、`admin-ui/src/views/` | [`48-frontend-button-function-inventory.md`](./48-frontend-button-function-inventory.md)、`admin-ui/src/api.js` |
| 运维、部署与恢复 | `scripts/`、`deploy/` | `docs/15-deployment.md`、`docs/27-backup-restore-runbook.md`；先看文档状态 |

## 运行与发布边界

- `scripts/status_local.ps1` 是本地状态的只读入口；最近记录并不代表现在的服务状态。
- `scripts/start_local.ps1` / `stop_local.ps1` 会改变本机进程，使用前需确认 `.env` 和当前授权。
- `deploy/` 中存在多份候选 Dockerfile 和辅助脚本，其中部分处于未提交工作树；它们不是自动认可的发布路线。
- 数据库事实以 `alembic heads` 加目标环境 `alembic current` 为准；本地 head 不能证明远端已迁移。

## 阅读旧文档的方式

`02-architecture.md`、`14-database-schema.md`、`15-deployment.md` 与 `16-testing.md` 保留各自专题内容，但含有历史迁移、部署或测试时点。查阅前先看 [`documentation-status.md`](./documentation-status.md)；需要验收时按 [`quality/verification-matrix.md`](./quality/verification-matrix.md) 取得本次证据。
