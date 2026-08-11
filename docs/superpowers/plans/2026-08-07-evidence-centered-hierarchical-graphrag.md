# Evidence-Centered Hierarchical GraphRAG 规划

更新时间：2026-08-07  
状态：Phase 0 已完成，Phase 1 待实施设计  
适用项目：`vectorDatabase`

## 1. 决策摘要

项目后续采用“文件类型感知解析 + 分层证据索引 + Claim Graph + Canonical Graph + Rule Candidate + 长上下文编排”的路线。

核心原则：

1. 先忠实保存文件说了什么，再决定它在业务本体中叫什么。
2. 长上下文提供理解容量，向量、关键词、重排和图谱负责选择证据。
3. 文件格式解析可以固定，业务实体类型和关系类型不能固定。
4. 法律、医疗、资产等内容默认是带来源的文档主张，不直接升级为现实事实。
5. 本体、事实主张、规则和 Agent Skill 分层处理，不由一个提示词一次生成。
6. 现有 discovery、confirmation、frozen snapshot、worker、materializer 和 publication 生命周期继续复用，不进行大爆炸重写。

### 1.1 规划依据

- `D:\wechat\xwechat_files\wxid_5l74ukfo2apw22_a585\msg\file\2026-08\人和致远 ZERON体系简介(2).pdf`：知识蒸馏、业务本体、确定性规则、多 Agent 与人工确认应分层建设。
- `docs/superpowers/plans/2026-07-10-v0.4-graph-extraction-pipeline.md`：现有 extraction、candidate、evidence 和 worker 基础。
- `docs/superpowers/plans/2026-07-15-v0.5-active-graph-publication.md`：publication 和 active snapshot 边界。
- `docs/superpowers/plans/2026-07-16-v0.6-published-graph-retrieval-m1.md` 至 `m6`：published graph retrieval 基础。
- 2026-08-06 至 2026-08-07 的真实 Flash artifact、Required E2E 和 Automatic E2E 记录：当前质量与产品阻塞的事实基线。

## 2. 当前真实基线

### 2.1 已经证明可用的部分

- `deepseek-v4-flash` 真实 discovery、Schema validation、frozen snapshot 和 formal extraction 已有成功记录。
- `required` 测试库已完成 Schema 确认、相同 snapshot/hash 绑定和真实 materialization。
- 当前系统可以保存 evidence unit、entity mention、entity、relation 和 relation evidence。
- Library 已有 `disabled | explore | governed` 模式以及 `required | automatic` 确认策略。
- GitNexus 不可用时，`AGENTS.md` 已允许使用人工调用链、定向测试和 diff 门禁作为 fallback。

### 2.2 Required E2E 当前状态

| 项目 | 值 |
|---|---|
| library | `8c7f8f9a-efa3-4ac3-835a-3b5bb55d2ffb` |
| batch | `b2c7564b-8541-45b5-8962-14d0b04c0727` |
| discovery run | `b24f7aa9-3d08-49c2-b0bf-c940d1857712` |
| ontology | `83ae34db-5f20-4cbf-a490-88e2a3949588` |
| snapshot hash | `c09dcd9e5bbf21ef04369824b9f006a3482ce268d17970edb749b3f099ae635` |
| Schema | 57 concepts、61 entity types、48 relation types、76 constraints |
| jobs | 1 succeeded、1 partially_succeeded |
| materialized | 91 entities、27 relations、131 mentions、27 relation evidence、16 evidence units |
| provider/model | `deepseek/deepseek-v4-flash` |

两个 job 使用相同 ontology version 和 snapshot hash，证明 required 确认和冻结边界有效。

尚未通过的关键链路：

```text
27 materialized relations
-> automatic publication skipped(no_valid_relation)
-> no publication
-> published Graph API unavailable
-> page entity count 0
-> canvas unavailable
```

一个 job 仍包含既有 `unit_failures`。在查清失败 unit、候选状态和 publication eligibility 之前，不能把 Required E2E 判定为成功。

### 2.3 Automatic E2E 当前状态

| 项目 | 值 |
|---|---|
| library | `509ab113-b6a4-4b86-ae26-793e5d678d0a` |
| policy | `automatic` |
| batch/jobs/discovery | 尚未产生 |

浏览器文件选择器超时并发生连接重置。不得伪造上传，也不得用历史批次替代 Automatic E2E。

### 2.4 当前质量基线

最新已报告成功真实评测的关键指标：

- entity recall：`0.95`
- strict/semantic relation recall：`0.1667`
- semantic type accuracy：`0.5263`
- alias merge rate：`1.0`

关系主要问题不是 parser/export 丢弃，而是重复 endpoint、谓词、方向和 canonical naming 不一致。因此继续叠加单一提示词规则的收益已经很低，需要拆分 raw claim extraction 与 canonical mapping。

## 3. 问题定义与边界

### 3.1 要解决的问题

系统需要从陌生、多格式、多领域文件中建立可追溯、可版本化、可检索、可确认的知识层，并支持精确事实、章节分析、整份文件分析、跨文件关系和规则问答。

### 3.2 本规划包含

- 文件类型感知解析和统一 evidence locator。
- Document -> Section -> Chunk/Structured Unit 分层索引。
- 动态 Schema discovery 和确认。
- 原始事实主张与 canonical graph 分离。
- evidence-backed entity linking 和 relation mapping。
- 条件、例外、生效时间和适用范围的 Rule Candidate。
- 向量、关键词、reranker、图谱联合检索。
- 128K-256K 模型的自适应 context packing。
- 产品 API、数据库、页面和真实模型验收。

### 3.3 本规划暂不包含

- 根据三个示例领域建立生产业务词表。
- 自动执行处罚、诊断、资产处置或法律结论。
- 从未确认的规则直接生成可执行 Agent Skill。
- 把所有文档默认完整塞入长上下文。
- 为提高指标修改 gold、semantic alignment 或 scorer。

## 4. 目标架构

```text
原始文件
  -> 文件类型适配器
  -> EvidenceUnit + Document/Section/Chunk 层级
  -> 多层索引(document/section/chunk/structured)
  -> Concept Inventory
  -> AI Schema Draft
  -> required/automatic confirmation
  -> Frozen Ontology Snapshot
  -> Claim Extraction(raw predicate + evidence + qualifiers)
  -> Entity Linking
  -> Canonical Relation Mapping
  -> Constraint/Evidence Validation
  -> Canonical Graph / Extension Candidates
  -> Publication
  -> Graph API / Hierarchical RAG / UI Canvas
```

规则和 Skill 使用旁路：

```text
EvidenceUnit + Claim Graph
  -> Rule Candidate(condition/exception/scope/effective time)
  -> human confirmation
  -> Confirmed Rule
  -> future Skill/Agent workflow
```

## 5. 文件类型感知解析

格式适配器只固定“怎样恢复结构和位置”，不固定“业务上有什么实体和关系”。

| 文件类型 | 必须保留的结构 |
|---|---|
| 原生 PDF | 页码、文本块、标题、表格、脚注、阅读顺序、坐标 |
| 扫描 PDF/图片 | OCR 文本、页码、bounding box、OCR confidence、图像引用 |
| DOCX | 标题层级、段落、表格、页眉页脚、批注/修订（可用时） |
| XLSX/CSV | sheet、row、column、cell、公式结果、表头路径、数据类型 |
| 发票/票据 | 原始版面位置、字段名和值、金额/税额/编号/日期、识别置信度 |
| 邮件/聊天 | thread、message、sender、recipient、timestamp、reply/quote 关系 |
| JSON/API 数据 | JSON Pointer/record key、字段类型、来源系统、采集时间 |

所有适配器归一为统一 evidence locator：

```text
organization_id
library_id
document_id
revision_id
evidence_unit_id
document_type
section_path
page/sheet/row/column/cell/message
bounding_box(optional)
content/content_hash
source_time/ingested_at
parser_version
```

## 6. 四层知识模型

### 6.1 Ontology Layer

保存经过确认或自动批准的 entity type、relation type、direction、endpoint constraint、attribute definition 和版本快照。

Ontology 只定义允许如何表达业务，不保存“某份文件声称了什么”。

### 6.2 Claim Graph

Claim Graph 是抽取的第一落点。关系至少保存：

```text
source_mention
raw_predicate
target_mention
surface_direction
negation
modality(confirmed/alleged/possible/planned...)
valid/effective time
evidence_refs
extractor/prompt/model version
```

Claim 不因当前 Schema 无法映射而消失。不能安全映射时进入 relation mapping candidate 或 schema extension candidate。

### 6.3 Canonical Graph

Canonical Graph 只保存经过 entity linking、relation mapping、direction、endpoint、constraint 和 evidence 校验的实体与关系。

每条 canonical relation 必须能反查：

- 原始 claim。
- mapping decision/confidence。
- frozen ontology snapshot/hash。
- 原始 evidence unit 和精确位置。

### 6.4 Rule Layer

规则不能退化为普通 relation。Rule Candidate 至少包含：

```text
subject/scope
required/prohibited/permitted action
conditions
exceptions
effective_start/effective_end
jurisdiction/organization scope
source evidence
confirmation state
```

医疗、法律和资产规则默认 required confirmation。自动批准只能由 Library 明确配置，不能通过关键词猜测风险等级。

## 7. 提示词职责拆分

| Prompt | 只负责 | 禁止负责 |
|---|---|---|
| Concept Inventory | 概念、标识符、别名证据、关系表达、规则线索 | materialization、最终事实判断 |
| Schema Synthesis | 聚类并定义 entity/relation types 和 constraints | 抽取具体实体实例 |
| Schema Repair | 修复一次完整非法 Schema | patch、静默删除 constraint |
| Semantic Refinement | 处理重复类型、谓词语态和 endpoint 语义 | 引入领域 allowlist |
| Claim Extraction | mention、raw predicate、endpoint、方向、时间、否定、evidence | 强迫所有 claim 映射成功 |
| Canonical Mapping | raw predicate -> frozen relation type | 不确定时编造 relation key |
| Rule Extraction | condition、exception、scope、effective time | 直接生成可执行处罚/诊断/处置 |

每个阶段独立记录 request/response hash、token、latency、finish reason、parser result 和失败原因。

## 8. 分层检索与长上下文

### 8.1 入库索引

每个 revision 建立：

- Document 索引：标题、摘要、文档类型、主体、时间。
- Section 索引：标题路径、章节摘要、父文档。
- Chunk 索引：原文、邻接关系、父章节。
- Structured 索引：表格行、票据字段、消息记录、JSON record。
- Graph 索引：entity、claim、canonical relation、rule 和 evidence link。

### 8.2 查询流程

```text
用户问题
-> query router(scope + intent + answer shape)
-> vector + keyword + structured + graph candidates
-> reranker
-> optional graph expansion(默认 1-2 hops)
-> 回取原始 evidence
-> 去重并保留冲突来源
-> context packer
-> 128K-256K answer model
-> 带证据定位的答案
```

### 8.3 查询路由

| 问题类型 | 主检索单元 | 建议有效上下文 |
|---|---|---:|
| 精确事实 | structured field/chunk + parent heading | 2K-10K |
| 章节问题 | section summary + relevant chunks | 10K-40K |
| 整体问题 | document summary + selected sections | 30K-100K |
| 跨文件问题 | document shortlist + sections + graph evidence | 60K-180K |
| 必须完整阅读 | 分段读取与归纳，必要时全文 | 不超过实测有效窗口 |

128K-256K 是容量上限，不是每次请求的填充目标。Context packer 默认保留至少 20% 窗口给 system prompt、工具结果和输出。

Query router 低置信度时必须允许多路召回，不能因单次误分类只搜索一个层级。

## 9. Publication Gate

Publication 必须有可解释的逐阶段计数：

```text
materialized relations
-> candidate status eligible
-> evidence eligible
-> confidence eligible
-> ontology/constraint eligible
-> publication items
-> active publication
```

每个被拒绝项必须记录稳定 reason code。`no_valid_relation` 不能只有总错误而没有各门禁计数。

一般产品可以单独决定是否允许 entity-only publication；本规划的完整图谱 E2E 必须至少发布一条有效、带 evidence 的 relation，不通过 entity-only 绕过验收。

## 10. 实施阶段

### Phase 0：关闭当前 E2E 缺口

状态：**已完成（2026-08-07）**

完成证据：

- Required publication `ce9f4c0a-922a-4e4d-96d4-51b386a2a05d` 已 active，91 entities、27 relations 和全部 evidence references 已发布，页面显示 `91 个实体 · 27 条关系`，canvas 正常。
- Automatic batch `14cd4e52-f19d-481b-9f93-f2cf4aed8567`、discovery run `84027a2a-c6b2-468b-a020-48ab1b956da1` 已完成，publication `ee813b2e-1462-40c5-baf3-b48ee618a039` 已 active，页面显示 `3 个实体 · 2 条关系`，canvas 正常。
- 根因是 materializer 生成 draft facts 后，auto-publication 错误使用 `include_drafts=False`，planner 因此排除全部实体和关系。修复只让同一闭环内的 draft facts 进入既有 evidence、confidence、endpoint 和 frozen Schema 校验，没有放宽门禁。
- 定向回归 108 passed，Ruff 和 `git diff --check` 通过；未重跑真实评测，未修改 gold、scorer 或关系提示词。

残余观测项：Automatic 原始 import row 仍显示 `processing/embedding`，但正式 `job_projection` 为 `succeeded/completed` 且无错误。该投影一致性问题不阻断 Phase 0 图谱闭环，但应在后续独立任务中修复，不能混入 Evidence Hierarchy。

目标：在改变关系架构前得到可信产品基线。

工作：

1. 重放 Required 的 27 条 materialized relations 到 publication eligibility。
2. 输出每个 rejection reason 和阶段计数，定位 `no_valid_relation` 根因。
3. 审计 partially_succeeded job 的唯一 unit failure。
4. 只修真实生产根因，不放宽 evidence/constraint 门禁。
5. 完成 Required publication、Graph API 和 canvas。
6. 恢复 Automatic 文件上传，完成独立 batch 的全链路 E2E。

Gate：Required 和 Automatic 均产生 active publication，Graph API 非空，页面实体/关系数与 API 一致，canvas 可见。

### Phase 1：Evidence Hierarchy

目标：统一不同文件类型的位置、结构和版本语义。

工作：

1. 审计现有 DocumentBlock、Chunk、EvidenceUnit、OCR/table metadata，优先复用。
2. 定义统一 evidence locator 和 parent/child contract。
3. 为 PDF、DOCX、XLSX/CSV、图片至少各建立一个结构 fixture。
4. 建立 document/section/chunk/structured 多层索引。

Gate：任一检索结果可以稳定返回 revision、章节/页/表格位置和 content hash。

### Phase 2：Claim Graph Shadow Path

目标：不再因 canonical Schema 选择错误而丢失原始关系。

工作：

1. 优先扩展现有 candidate 边界，保存 raw predicate、surface direction 和 qualifiers。
2. Claim Graph 先 shadow write，不改变当前线上 publication。
3. 保留所有合法 evidence-backed claim，包括无法 canonicalize 的 claim。
4. 增加 claim replay artifact。

Gate：raw relation recall、endpoint、direction 和 evidence 可以独立评分；旧 canonical path 无行为回归。

### Phase 3：Canonical Mapping

目标：将“有没有抽到关系”和“关系叫什么”彻底分开。

工作：

1. 基于 frozen Schema 映射 raw predicate。
2. 使用 relation description、endpoint type、direction 和 evidence sentence 联合判断。
3. 不确定时进入 mapping candidate，不使用 `related_to`。
4. Schema 变化时允许从 immutable claim 重新映射，不重跑 OCR/原文抽取。
5. 通过 shadow comparison 后再切换 publication 输入。

Gate：canonical mapping 达标并证明 rejection 不会静默删除 raw claim。

### Phase 4：Rule Candidate

目标：支持医保规则、司法时效、工资支付条件、合同条件和例外。

工作：

1. 增加独立 rule protocol 和状态机。
2. Rule Candidate 必须有 evidence、scope、condition、exception 和 effective time。
3. 默认 required confirmation。
4. Rule 不进入普通 relation scorer。

Gate：三领域 fixture 均能抽取规则和例外，未确认规则不会被执行或作为确定事实回答。

### Phase 5：Hierarchical Retrieval and Context Packing

目标：让查询按问题范围选择合适证据，而不是固定 chunk 或默认全文。

工作：

1. 增加 query router，但保留低置信度多路 fallback。
2. 联合 vector、keyword、structured、graph retrieval。
3. reranker 在同一证据协议上排序。
4. graph expansion 必须回到 evidence，不把 graph edge 当无来源答案。
5. context packer 处理去重、冲突、时间版本、token 预算和 citation map。

Gate：精确事实、章节、整体、跨文件四类查询分别通过真实评测。

### Phase 6：真实领域 Pilot

目标：从合成 20/18 数据集转向真实业务可用性。

最少建立三套独立、人工标注且不进入 prompt 的数据集：

- 不良资产/法律：卷宗、合同、权属、担保、资金、司法时效。
- 医疗/医保：政策、规则、诊疗材料、药品说明、适用条件和例外。
- 工程工资/票据：人员、用工、考勤、工资、资金、支付、发票或表格。

Gate：每套数据独立报告 extraction、mapping、retrieval、answer 和 citation 指标，不使用跨领域生产 allowlist。

## 11. 评测体系

### 11.1 Extraction

- entity/mention recall。
- alias merge precision 和 recall。
- raw relation recall。
- endpoint linking accuracy。
- direction accuracy。
- negation/modality/time accuracy。
- evidence validity。

### 11.2 Canonicalization

- canonical predicate accuracy。
- constraint accuracy。
- unknown mapping rejection precision。
- schema extension candidate precision。
- end-to-end semantic relation precision/recall。

### 11.3 Retrieval and Answer

- document/section/chunk recall@K。
- reranker NDCG/MRR。
- graph expansion evidence precision。
- answer correctness。
- citation correctness 和 source locator validity。
- contradiction preservation。
- unsupported claim rate。

### 11.4 首个 Pilot 建议门槛

| 指标 | 建议门槛 |
|---|---:|
| entity recall | >= 0.90 |
| alias merge precision | >= 0.95 |
| raw relation recall | >= 0.80 |
| endpoint accuracy | >= 0.90 |
| direction accuracy | >= 0.90 |
| canonical predicate accuracy | >= 0.70 |
| evidence validity | 1.00 |
| end-to-end semantic relation recall | >= 0.60 |
| citation locator validity | 1.00 |
| unsupported high-risk claim | 0 |

门槛需要在真实 Pilot 标注集上确认。不得用扩大 semantic alignment 的方式代替生产质量提升。

## 12. 长上下文验证

模型声明支持 128K-256K 不等于在所有位置同等可靠。上线前必须在实际部署配置上测试：

- 32K、64K、128K、256K 的关键证据召回。
- 证据位于开头、中间和结尾的位置敏感性。
- 多文件同名主体混淆。
- 不同版本和相互矛盾条款。
- 表格与正文联合回答。
- latency、吞吐、显存和单次成本。

通过测试得到 `effective_context_limit`，Context packer 使用实测有效窗口，不直接采用模型宣传上限。

## 13. 迁移与发布策略

1. 不删除现有 canonical extraction 路径。
2. Claim Graph 先 shadow write，禁止直接驱动 production answer。
3. 使用同一 evidence 输入比较旧路径与新路径。
4. Canonical mapper 达标后，按 Library feature flag 灰度切换 publication 输入。
5. 原始 claim 保持 immutable；重新映射产生新 mapping version。
6. parser、prompt、model、ontology、mapper、retriever、reranker 和 context policy 全部版本化并写入 artifact。
7. 回滚只切换读取/发布版本，不删除 evidence 或 claim。

## 14. 风险与控制

| 风险 | 控制 |
|---|---|
| Schema 类型爆炸 | evidence support、语义聚类、人工确认、core/extension 分层 |
| 长上下文噪声稀释 | rerank、去重、层级扩展、动态 token budget |
| 图谱错误放大 | claim/canonical 分层、edge 必须回到 evidence |
| 同名实体误合并 | identifier 优先、歧义候选、禁止无证据模糊合并 |
| 法律/医疗结论越权 | claim modality、rule confirmation、来源和时间强制展示 |
| Publication 静默为空 | 阶段计数、稳定 rejection reason、非空产品 gate |
| 模型/提示词漂移 | frozen config/hash、真实 artifact、shadow comparison |

## 15. 下一执行任务

下一个执行智能体只实施 Phase 0，不立即开始 Claim Graph migration：

```text
Required 27 relations
-> publication eligibility replay
-> rejection reason counts
-> minimal production fix
-> active publication
-> Graph API
-> canvas
-> Automatic fresh batch E2E
```

Phase 0 完成并形成可信基线后，再为 Phase 1/2 单独写实施计划和 migration 设计。不得在 Phase 0 顺便继续堆叠关系 prompt 补丁。

## 16. 整体完成定义

以下条件全部满足后，才能宣称本规划对应的 GraphRAG 能力完成：

1. Required 和 Automatic 产品 E2E 均通过。
2. 文件类型适配器输出统一、可定位的 EvidenceUnit。
3. Raw claim 不因 Schema mapping 失败而丢失。
4. Canonical relation 可追溯到 claim、mapping version 和原文 evidence。
5. Rule Candidate 与普通 relation 分离，并遵守确认策略。
6. 四类查询路由和长上下文编排通过真实评测。
7. 三个领域 Pilot 达到确认后的质量门槛。
8. Graph API、RAG answer 和 UI citation/canvas 使用同一 publication/evidence 边界。
9. 所有真实结果保留可审计 artifact，失败不伪造指标。
