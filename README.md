# Vector Knowledge Base

面向公司内部的**向量知识检索底座**：可靠地摄入文档、做权限受控的检索，并以 **Dify 外部知识库兼容**的统一接口供上层系统（Dify / FastGPT / 办公系统 / Agent）调用。

> **状态：`v0.1.0-internal-pilot`（内部试运行，非正式推广）。** 仅用于单部门试运行 1~2 周，再逐步开放。
> 它的定位是「摄入 + 检索的底座」，不与 Dify/RAGFlow 比编排/应用能力——上层负责工作流与答案生成，本服务负责把**对的片段**可靠地交出去。

> 📚 详细设计与操作见 [`docs/`](./docs/README.md)（按功能拆分，24 篇）。本 README 是**项目介绍 + 部署交付入口**。

---

## 1. 项目定位与当前版本

- **是什么**：多知识库、库级权限隔离的向量检索服务。每个「库」物理隔离（独立 Qdrant collection），按 `(用户, 库, 动作)` 三元组授权。（尚无正式的 tenant/部门模型，隔离粒度是「库 + 库级权限」。）
- **当前版本**：`v0.1.0-internal-pilot`，数据库迁移已到 **`0010`**，pytest 基线 **154 项通过**（另 14 项 PG 集成用例需测试库时才跑）。
- **技术栈**：FastAPI + SQLAlchemy 2.0 (async) + Alembic ｜ Qdrant ｜ PostgreSQL 队列（`FOR UPDATE SKIP LOCKED`）｜ Embedding 走 OpenAI 兼容 `/v1/embeddings`（默认本地 **bge-m3**）｜ 认证 fastapi-users（JWT cookie + API Key 双通道）｜ 权限 Casbin。

## 2. 从初始版到当前版增加了什么

| 维度 | 初始版 | 当前版 |
|---|---|---|
| 文件摄入 | 以纯文本为主 | TXT / MD / JSON / CSV / **DOCX / XLSX / PDF**（DOCX 内嵌图片 + PDF 扫描页 OCR、表格感知切块） |
| Worker | 1 个 Embedding Worker | **Embedding + Cleanup 两个 Worker** |
| 检索 | Dense | Dense + **可选 Rerank** + 双分数（vector_score / rerank_score）+ score_threshold 语义 |
| 文档身份 | 简单内容去重 | **external_id / content_hash** 明确身份规则 |
| 更新 | 可能残留旧向量 | **revision + job generation**，旧向量按代际过滤即不可见 |
| 删除 | 即时直接清 Qdrant | **tombstone + Outbox + 重试清理**（逻辑即时、物理异步幂等） |
| 重建 | 直接操作 collection | **三阶段重建 + 失败恢复**状态机 |
| 健康检查 | DB / Qdrant | DB / Qdrant / **Embedding / Rerank / OCR** 自检 |
| 测试 | 少量单元测试 | 单元 + PG 集成 + E2E + **六步闭环验收** |
| 发布 | 无模板 | **`.env.example` + 内部试运行清单 + 验收脚本** |

> 概要止于此；逐项设计见 [docs/20 一致性](./docs/20-revision-and-deletion-consistency.md)、[docs/19 源库补全](./docs/19-source-enrichment.md) 等。

## 3. 当前架构

```
   Dify / FastGPT / 办公系统 / Agent
                │  (Bearer API Key, Dify 兼容)
                ▼
        ┌──────────────────┐        ┌──────────────┐
        │   API (8100)     │───────▶│  PostgreSQL  │  库/文档/分片/任务/审计
        │  FastAPI         │        │  + 队列表    │  revision / outbox
        └───────┬──────────┘        └──────┬───────┘
                │ embed/search             │ SKIP LOCKED 抢任务
                ▼                          ▼
        ┌──────────────┐         ┌───────────────────┐   ┌───────────────────┐
        │ Embedding    │         │ Embedding Worker  │   │ Cleanup Worker    │
        │ (bge-m3)     │◀────────│  转向量→写 Qdrant │   │ 消费 outbox→删 Qdrant │
        └──────────────┘         └─────────┬─────────┘   └─────────┬─────────┘
                                           ▼                       ▼
                                   ┌───────────────────────────────────┐
                                   │            Qdrant (每库一 collection)│
                                   └───────────────────────────────────┘
```

三类长驻进程：**API**、**Embedding Worker**、**Cleanup Worker**。详见 [docs/02 架构](./docs/02-architecture.md)。

## 4. 已具备功能

- 多知识库管理，每库独立 Qdrant collection、独立 embedding 模型/地址/切分参数。
- 多格式文件摄入（见 §5），异步转向量、任务可监控可重试。
- 文档**更新**（revision 代际）与**删除**（tombstone + outbox 物理清理）的一致性保证。
- 库**重建**三阶段状态机，崩溃可恢复，重建期间检索不返回半成品。
- Dense 检索 + 可选 Rerank + score_threshold + metadata 过滤；跨库**源库正文补全**。
- 检索效果**评测尺子**（hit@k / MRR / Recall）与**六步闭环验收**脚本。
- Casbin 细粒度权限、API Key 双通道、审计日志、`/health` 五维自检。
- 零构建管理后台 console（建库/授权/摄入/任务/审计/问答/运行状态）；前端依赖和图标均本地化，页面运行时零公网 CDN 请求。见 [docs/12](./docs/12-admin-ui.md)。

## 5. 支持的文件格式

| 类型 | 说明 |
|---|---|
| `.txt` `.md` `.markdown` | 纯文本 / Markdown |
| `.json` `.csv` | 结构化（单对象 / 数组 / 多行，自动取 text/content 列） |
| `.docx` | Word：段落 + 表格；可选 **表格感知切块**（库级 `docx_table_aware`）、内嵌图片 OCR |
| `.xlsx` | Excel：按工作表表格感知切分（`.xls` 旧格式不支持，请另存为 `.xlsx`） |
| `.pdf` | 文字层直接提取；开启库级 OCR 后逐页支持**扫描页与混合 PDF**（图片页渲染→OCR，带 `【第 N 页】` 标记） |

OCR 按库级 `ocr_enabled` 开启（需装 OCR 依赖：`pip install -e ".[ocr]"`）。详见 [docs/09 文档摄入](./docs/09-document-ingest.md)。

## 6. 文档摄入、更新与删除机制

- **身份规则**：带 `external_id` → 身份 =(库, external_id)，按它 upsert；不带 → 身份 =(库, content_hash)，按内容去重。DB 层活动行唯一索引兜底并发。
- **更新（reingest）**：`current_revision +1`，旧 job 标 superseded，检索按 `current_revision` 过滤——**旧向量立即不可见**，不依赖同步删 Qdrant。
- **删除**：单事务 tombstone（软删）+ supersede 在途 job + 入 `qdrant_cleanup_outbox`。提交即检索不可见；**Cleanup Worker** 幂等消费 outbox 做物理删除，失败指数退避重试。

详见 [docs/20 修订与删除一致性](./docs/20-revision-and-deletion-consistency.md)。

## 7. 检索、Rerank 与评测

- **检索**：`POST /retrieval`（Dify 兼容）。Dense 召回 + 有界 overfetch + 可见性过滤（回查 PG 丢弃陈旧/越库/已删）+ 源库正文补全。
- **Rerank（可选，默认关）**：`RERANK_PROVIDER` = `standard`（Infinity/TEI/Jina/Cohere 兼容）或 `dashscope`；结果带 `vector_score` 与 `rerank_score` 双分数。
- **score_threshold**：`retrieval_setting.score_threshold`（0~1）过滤低分结果。
- **评测**：`scripts/eval_retrieval.py` 固定评测集跑真实链路，输出 hit@1/3/5、MRR、Recall，可对比 rerank 开/关（评测集为业务数据，不入仓库；格式见 `eval/README.md`）。

详见 [docs/10 检索接口](./docs/10-retrieval-api.md)。

## 8. 权限、安全与外部 API

- **认证**：JWT Cookie（后台）+ Bearer **API Key**（Dify/外部）双通道，Key 仅 bcrypt 存储、明文只显示一次。
- **权限**：Casbin `(user, library, action)`，action ∈ `read/insert/delete/admin`，超管直通。
- **隔离**：每库独立 collection；敏感配置不入仓库（`.env` 已 gitignore）。

外部对接关键接口（完整见 [docs/13 API 参考](./docs/13-api-reference.md)）：

| 路径 | 方法 | 鉴权 | 说明 |
|---|---|---|---|
| `/retrieval` | POST | API Key | **Dify 兼容**检索 |
| `/libraries/{slug}/documents` | POST | insert | 摄入文本入库（异步 embed） |
| `/libraries/{slug}/documents/{document_id}` | PUT | insert | 更新文档（reingest，revision +1） |
| `/libraries/{slug}/documents/{document_id}` | DELETE | delete | 删除（tombstone + outbox 清理） |
| `/libraries/{slug}/import-file` | POST | insert | 上传文件摄入（多格式） |
| `/libraries/{slug}/query` | POST | read | 内部检索（带双分数/metadata） |
| `/libraries/{slug}/jobs/{id}` | GET | read | 轮询摄入任务状态 |
| `/health` | GET | 公开 | DB/Qdrant/Embedding/Rerank/OCR 自检 |

## 9. 快速开始（三个进程）

```bash
# 1. 依赖
pip install -r requirements-dev.txt

# 2. 配置（从模板复制，按需填写；切勿提交真实 .env）
cp .env.example .env

# 3. 建库 + 迁移（迁移到 head=0010）
createdb -h <host> -p <port> -U postgres vector_kb
alembic upgrade head

# 4. 首位超级管理员
python scripts/bootstrap_admin.py --email admin@example.com --password CHANGE_ME --username admin

# 5. 启动三个长驻进程（各一个终端）
python -m app.main                       # API（端口读 .env，默认 8100）
python -m app.workers.embedder --watch   # Embedding Worker
python -m app.workers.cleanup  --watch   # Cleanup Worker

# 6. 后台：浏览器开 http://<host>:8100/console/
```

详见 [docs/04 快速开始](./docs/04-quickstart.md)。

## 10. 数据库迁移与首次管理员

- `alembic upgrade head` → **head = `0010`**，共 10 个迁移。
- 业务表 9 张 + `casbin_rule`：`sys_users` / `sys_api_keys` / `sys_libraries` / `documents` / `chunks` / `embedding_jobs` / `sys_audit_log` / **`rebuild_operations`**（0009）/ **`qdrant_cleanup_outbox`**（0010）。
- 首管：`scripts/bootstrap_admin.py`（输出 `superuser created: ...`）。
- 升级注意：0009 活动唯一索引创建前需先清理历史违规活动行（见 docs/20 §11.1）。

字段详见 [docs/14 数据库 Schema](./docs/14-database-schema.md)。

## 11. 六步发布验收

发布版自带端到端验收脚本，走真实 HTTP API：

```bash
ACC_API_KEY=<有 insert/read/delete 权限的明文Key> python scripts/acceptance.py --slug <库slug>
```

逐步验证：① 上传文档 → ② 任务变 `done` → ③ 明确内容检索命中 → ④ 改后重传：新内容可检索、旧内容不可见 → ⑤ 删除后立即检索不到 → ⑥ Cleanup Worker 完成 Qdrant 物理清理。全 ✅ 即通过。

单元/逻辑测试：`pytest`（基线 154 passed / 14 skipped）。详见 [docs/16 测试](./docs/16-testing.md)。

## 12. 服务器部署入口

- **上线门槛清单（必读）**：[docs/22 内部试运行清单](./docs/22-internal-pilot-checklist.md)——8 条服务器门槛 + 六步验收 + embedding provider pin。
- **生产部署细节**：[docs/15 部署](./docs/15-deployment.md)——三类 systemd 单元（api / embedder / cleanup）、Nginx、备份恢复、监控。
- **Embedding Provider 必须二选一并固定**：本地 bge-m3（推荐、免费）或远程（如阿里云 DashScope，需确保账户余额——欠费会同时阻断 embedding 与 rerank）。切换模型会改变向量空间，已建库需**重建**。

## 13. 当前边界与暂未实现

- 检索默认 **Dense、不重排**；本地 Reranker 尚未部署（开启重排可提升精度，属增强项非阻塞项；此前的提升数据来自 DashScope qwen3-rerank，不代表本地 bge-reranker 已验证）。
- 无 Hybrid（稀疏+稠密融合）检索。
- 扫描件 PDF 需**开启库级 OCR**（默认关）；`.xls` 旧格式不支持（请另存为 `.xlsx`）；不还原图片表格行列结构、不做票据字段结构化。
- Embedding 为单点：试运行务必 pin 住 provider（推荐本地 bge-m3）。
- 当前适合**单 API 进程**；多副本部署前需增加 Casbin Watcher（或主动同步），保证权限策略跨进程一致。

这些都**不是上线阻断项**——等真实部门使用暴露具体问题后再决定优先级。

## 14. 完整文档索引

| # | 文档 | 内容 |
|---|---|---|
| 01 | [项目介绍](./docs/01-introduction.md) | 是什么、解决什么 |
| 02 | [架构总览](./docs/02-architecture.md) | 全景、数据流、关键设计 |
| 03 | [项目结构](./docs/03-project-structure.md) | 目录与文件职责 |
| 04 | [快速开始](./docs/04-quickstart.md) | 三进程跑起来 |
| 05 | [配置说明](./docs/05-configuration.md) | 全部 `.env` 项 |
| 06 | [认证系统](./docs/06-authentication.md) | JWT cookie + API Key |
| 07 | [权限系统](./docs/07-permissions.md) | Casbin (user, library, action) |
| 08 | [库管理](./docs/08-libraries.md) | per-library collection |
| 09 | [文档摄入](./docs/09-document-ingest.md) | 多格式 / 切分 / 身份 / 异步 |
| 10 | [检索接口](./docs/10-retrieval-api.md) | Dify spec + rerank + 过滤 |
| 11 | [Worker](./docs/11-worker.md) | 队列消费 |
| 12 | [管理后台 UI](./docs/12-admin-ui.md) | console 页面 |
| 13 | [API 参考](./docs/13-api-reference.md) | 全 endpoint 速查 |
| 14 | [数据库 Schema](./docs/14-database-schema.md) | 表 + 字段 |
| 15 | [部署](./docs/15-deployment.md) | systemd / Nginx / 备份 |
| 16 | [测试](./docs/16-testing.md) | 跑测试 + 加测试 |
| 17 | [FAQ](./docs/17-faq.md) | 踩坑速查 |
| 18 | [使用说明](./docs/18-usage-guide.md) | 日常操作手册 |
| 19 | [源库正文补全](./docs/19-source-enrichment.md) | 跨库回查正文 |
| 20 | [修订与删除一致性](./docs/20-revision-and-deletion-consistency.md) | revision / outbox / 重建 |
| 21 | [批次 A 实施方案](./docs/21-batch-a-implementation-plan.md) | 一致性实现记录 |
| 22 | [内部试运行清单](./docs/22-internal-pilot-checklist.md) | 上线门槛 + 验收 |

> 检索评测尺子说明见 [`eval/README.md`](./eval/README.md)。
