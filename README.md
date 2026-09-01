# Vector Knowledge Base

面向公司内部的**向量知识检索底座**：可靠地摄入文档、做权限受控的检索，并以 **Dify 外部知识库兼容**的统一接口供上层系统（Dify / FastGPT / 办公系统 / Agent）调用。

> **当前真相（核对日期：2026-08-29）**：`pyproject.toml` 的 package version 是 `0.1.0`；代码迁移 head 是 **`0062`**。参考部署数据库在 2026-08-12 的 `alembic current` 为 `0060`，不能代表当前目标环境。文档中的 `v0.x`、`M*` 等是阶段/设计标签，不是 package version。部署到目标数据库时仍应执行 `alembic heads` 与 `alembic current`，以目标环境输出为准。
> 当前代码范围已经超出早期的“只有向量检索”试运行：向量摄入/检索仍是核心，并包含按 feature gate 或 rollout 控制的图谱、分类、知识产物、Evidence/claim shadow、公开 API/MCP 与组织能力。未启用的能力不应仅因代码或迁移存在就被视为已上线。
> 它仍不是 Dify/RAGFlow 的编排/应用层——上层负责工作流与答案生成，本服务负责把**对的片段**和受治理的知识产物可靠地交出去。

> 📚 详细设计与操作见 [`docs/`](./docs/README.md)。截至 2026-08-12，`docs/` 下有 63 个 Markdown 文件（根目录 47 个、子目录 16 个）；该索引区分当前活文档与历史记录。本 README 是**项目介绍 + 部署交付入口**。

---

## 1. 项目定位与当前版本

- **是什么**：多知识库、库级权限隔离的向量检索服务。每个「库」物理隔离（独立 Qdrant collection），按 `(用户, 库, 动作)` 三元组授权。（尚无正式的 tenant/部门模型，隔离粒度是「库 + 库级权限」。）
- **当前版本**：package version 为 `0.1.0`（见 `pyproject.toml`），数据库迁移 head 为 **`0062`**。测试基线不在这里固定数字；带日期和命令的最近验证见 [docs/16 测试](./docs/16-testing.md)。
- **技术栈**：FastAPI + SQLAlchemy 2.0 (async) + Alembic ｜ Qdrant ｜ PostgreSQL 队列（`FOR UPDATE SKIP LOCKED`）｜ Embedding 走 OpenAI 兼容 `/v1/embeddings`（默认本地 **bge-m3**）｜ 认证 fastapi-users（JWT cookie + API Key 双通道）｜ 权限 Casbin。

## 2. 从初始版到当前版增加了什么

| 维度 | 初始版 | 当前版 |
|---|---|---|
| 文件摄入 | 以纯文本为主 | TXT / MD / JSON / CSV / **DOCX / XLSX / PDF**（DOCX 内嵌图片 + PDF 扫描页 OCR、表格感知切块） |
| Worker | 1 个 Embedding Worker | Embedding / Cleanup 为基础队列 worker；另有 Importer、Graph Extraction、Knowledge Artifact、Classification 入口，按能力启用 |
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

长驻进程按部署能力确定：至少是 **API + Embedding Worker + Cleanup Worker**；文件导入需要 Importer，启用图谱、知识产物或分类时再启动对应 worker。详见 [docs/02 架构](./docs/02-architecture.md)。

## 4. 已具备功能

- 多知识库管理，每库独立 Qdrant collection、独立 embedding 模型/地址/切分参数。
- 多格式文件摄入（见 §5），异步转向量、任务可监控可重试。
- 文档**更新**（revision 代际）与**删除**（tombstone + outbox 物理清理）的一致性保证。
- 库**重建**三阶段状态机，崩溃可恢复，重建期间检索不返回半成品。
- Dense 检索 + 可选 Rerank + score_threshold + metadata 过滤；跨库**源库正文补全**。
- 检索效果**评测尺子**（hit@k / MRR / Recall）与**六步闭环验收**脚本。
- Casbin 细粒度权限、API Key 双通道、审计日志、`/health` 五维自检。
- 图谱 schema / extraction / publication / retrieval、实体链接与外部同步；知识产物与分类 taxonomy / decision runtime；Evidence locator 及 claim shadow 的 raw claim、decision projection、canonical mapping 持久化。上述能力由配置或 rollout 控制，默认状态以 `.env` 和部署 profile 为准。
- 零构建管理后台 console（建库/授权/摄入/任务/审计/问答/运行状态）；前端依赖和图标均本地化，页面运行时零公网 CDN 请求。见 [docs/12](./docs/12-admin-ui.md)。

## 5. 支持的文件格式

| 类型 | 说明 |
|---|---|
| `.txt` `.md` `.markdown` `.rst` `.log` `.ini` `.cfg` `.conf` | 纯文本；支持 UTF-8、UTF-16 BOM、GB18030、Big5 |
| `.json` `.yaml` `.yml` `.xml` `.html` `.htm` `.csv` `.tsv` | 结构化与网页文本；HTML 自动忽略脚本和样式 |
| `.doc` `.docx` | Word：旧 `.doc` 在异步批量新增中由 LibreOffice 临时转为 DOCX 解析；正式文件与证据仍绑定原 DOC。DOCX 支持段落、表格、可选 **表格感知切块**（库级 `docx_table_aware`）和内嵌图片 OCR |
| `.pptx` | PowerPoint：按幻灯片提取文本与表格 |
| `.xls` `.xlsx` | Excel：按工作表表格感知切分；旧 `.xls` 通过受限 `xlrd` 解析 |
| `.pdf` | 文字层直接提取；开启库级 OCR 后逐页支持**扫描页与混合 PDF**（图片页渲染→OCR，带 `【第 N 页】` 标记） |
| `.bmp` `.jpeg` `.jpg` `.png` `.tif` `.tiff` `.webp` | 独立图片：文件名始终进入检索文本；开启库级 OCR 后提取文字，并保留图片区域坐标、置信度与原文件证据绑定 |

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

## 9. 快速开始（按启用能力启动进程）

```bash
# 1. 依赖
pip install -e ".[dev]"

# 2. 配置（从模板复制，按需填写；切勿提交真实 .env）
cp .env.example .env

# 3. 建库 + 迁移（当前代码 head=0062；以目标环境 alembic heads/current 为准）
createdb -h <host> -p <port> -U postgres vector_kb
alembic upgrade head

# 4. 首位超级管理员
python scripts/bootstrap_admin.py --email admin@example.com --password CHANGE_ME --username admin

# 5. 启动基础长驻进程
python -m app.main                       # API（端口读 .env，默认 8100）
python -m app.workers.embedder --watch   # Embedding Worker
python -m app.workers.cleanup  --watch   # Cleanup Worker

# 文件上传/导入链路需要
python -m app.workers.importer --watch

# 按 feature gate 启用时再启动
python -m app.workers.graph_extractor --watch
python -m app.workers.knowledge_artifacts --watch
python -m app.workers.classifications --watch

# 6. 后台：浏览器开 http://<host>:8100/console/
```

Remote immutable revision-file storage is optional. Local storage is built in; configured
MinIO or Alibaba OSS deployments install their lazy SDK adapters with:

```bash
pip install -e ".[object-storage]"
```

Revision-file retention governance is also default-off. Set
`REVISION_RETENTION_ENABLED=true` and enable the Library policy to record replacement-ready
deadlines, bounded graph impact, and pre-expiry notices. Migration `0027` does not delete
objects or rows; physical cleanup remains a separately gated executor.

详见 [docs/04 快速开始](./docs/04-quickstart.md)。

## 10. 数据库迁移与首次管理员

- `alembic upgrade head` → 当前代码 **head = `0062`**；截至 2026-08-29，`alembic/versions/` 有 61 个 revision 文件。revision 编号不是产品版本，也不能用旧的“10 个迁移”描述当前部署。
- 当前 schema 已超出初始库表：除文档/分片/队列/审计/Casbin 外，还包含 revision/file/import、Evidence、图谱与 publication、知识产物、分类、组织/授权、公开 API operations，以及 claim shadow / canonical mapping 等迁移产物。完整当前结构以迁移和 ORM 为准，见 [docs/14](./docs/14-database-schema.md)。
- 首管：`scripts/bootstrap_admin.py`（输出 `superuser created: ...`）。
- 升级注意：0009 活动唯一索引创建前需先清理历史违规活动行（见 docs/20 §11.1）。

字段详见 [docs/14 数据库 Schema](./docs/14-database-schema.md)。

## 11. 六步发布验收

发布版自带端到端验收脚本，走真实 HTTP API：

```bash
ACC_API_KEY=<有 insert/read/delete 权限的明文Key> python scripts/acceptance.py --slug <库slug>
```

逐步验证：① 上传文档 → ② 任务变 `done` → ③ 明确内容检索命中 → ④ 改后重传：新内容可检索、旧内容不可见 → ⑤ 删除后立即检索不到 → ⑥ Cleanup Worker 完成 Qdrant 物理清理。全 ✅ 即通过。

单元/逻辑测试：执行 `pytest -q`；不要把历史测试数字当作当前基线。带日期的验证快照见 [docs/16 测试](./docs/16-testing.md)。

## 12. 服务器部署入口

- **上线门槛清单（必读）**：[docs/22 内部试运行清单](./docs/22-internal-pilot-checklist.md)——8 条服务器门槛 + 六步验收 + embedding provider pin。
- **生产部署细节**：[docs/15 部署](./docs/15-deployment.md)——API、基础 worker、按能力启用的 importer/图谱/知识产物/分类 worker、Nginx、备份恢复、监控。
- **Embedding Provider 必须二选一并固定**：本地 bge-m3（推荐、免费）或远程（如阿里云 DashScope，需确保账户余额——欠费会同时阻断 embedding 与 rerank）。切换模型会改变向量空间，已建库需**重建**。

## 13. 当前边界与暂未实现

- 检索默认以 Dense 为基线；Hybrid、Query Rewrite、Chat、Rerank 已有代码与配置入口，但是否可用取决于部署的 feature gate、provider 自检和 rollout，不应默认假设全部开启。
- 扫描件 PDF 和独立图片需**开启库级 OCR**（默认关）；旧 `.doc` 仅支持异步批量新增，单文件替换和批量替换仍需另存为 `.docx`；不还原图片表格行列结构、不做票据字段结构化。
- Embedding 为单点：试运行务必 pin 住 provider（推荐本地 bge-m3）。
- 当前适合**单 API 进程**；多副本部署前需增加 Casbin Watcher（或主动同步），保证权限策略跨进程一致。

这些都**不是上线阻断项**——等真实部门使用暴露具体问题后再决定优先级。

## 14. 核心活文档索引

| # | 文档 | 内容 |
|---|---|---|
| 01 | [项目介绍](./docs/01-introduction.md) | 是什么、解决什么 |
| 02 | [架构总览](./docs/02-architecture.md) | 全景、数据流、关键设计 |
| 03 | [项目结构](./docs/03-project-structure.md) | 目录与文件职责 |
| 04 | [快速开始](./docs/04-quickstart.md) | 按启用能力启动 API / worker |
| 05 | [配置说明](./docs/05-configuration.md) | 全部 `.env` 项 |
| 06 | [认证系统](./docs/06-authentication.md) | JWT cookie + API Key |
| 07 | [权限系统](./docs/07-permissions.md) | Casbin (user, library, action) |
| 08 | [库管理](./docs/08-libraries.md) | per-library collection |
| 09 | [文档摄入](./docs/09-document-ingest.md) | 多格式 / 切分 / 身份 / 异步 |
| 10 | [检索接口](./docs/10-retrieval-api.md) | Dify spec + rerank + 过滤 |
| 11 | [Worker](./docs/11-worker.md) | worker 入口、队列与 feature gate |
| 12 | [管理后台 UI](./docs/12-admin-ui.md) | console 页面 |
| 13 | [API 参考](./docs/13-api-reference.md) | 全 endpoint 速查 |
| 14 | [数据库 Schema](./docs/14-database-schema.md) | 初始表与当前迁移边界 |
| 15 | [部署](./docs/15-deployment.md) | 当前 head、systemd / Nginx / 备份 |
| 16 | [测试](./docs/16-testing.md) | 跑测试 + 加测试 |
| 17 | [FAQ](./docs/17-faq.md) | 踩坑速查 |
| 18 | [使用说明](./docs/18-usage-guide.md) | 日常操作手册 |
| 19 | [源库正文补全](./docs/19-source-enrichment.md) | 跨库回查正文 |
| 20 | [修订与删除一致性](./docs/20-revision-and-deletion-consistency.md) | revision / outbox / 重建 |
| 21 | [批次 A 实施方案](./docs/21-batch-a-implementation-plan.md) | 一致性实现记录 |
| 22 | [内部试运行清单](./docs/22-internal-pilot-checklist.md) | 上线门槛 + 验收 |

> 检索评测尺子说明见 [`eval/README.md`](./eval/README.md)。
> 当前活文档与历史设计/验收记录的边界、以及完整 63 文件计数见 [`docs/README.md`](./docs/README.md)。
