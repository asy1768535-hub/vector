# 多部门知识检索基础设施路线图 v3

> 项目：`vectorDatabase`
>
> 定位：面向公司多个部门，提供可靠摄入、权限隔离、可评测检索和可追踪来源的知识检索基础设施。
>
> 版本：v3
> 更新日期：2026-07-29

---

## 1. 最终目标

本项目最终不是一个“上传文档后直接生成答案”的应用，也不应发展成另一个 Dify、RAGFlow 或 Agent 平台。

它应该成为公司内部稳定、可复用的 **知识检索底座**：

> 各部门通过后台或 API 同步资料，系统完成解析、切块、向量化、关键词索引、融合召回、重排、权限控制、评测和监控；Dify、办公系统、客服系统及 Agent 平台调用统一检索 API，获得可验证、可引用、可追踪的知识片段。

### 1.1 对业务人员

- Word、Excel、PDF、Markdown、JSON、CSV 等文件可以稳定入库。
- 制度、条款、表格字段、报表组件、编号和专业术语能够被准确召回。
- 每条结果都能看到文档、章节、表格、版本、更新时间和片段位置。
- 文档更新后旧内容不再被召回，删除后向量能够可靠清理。
- 没有权限的用户看不到、查不到、上传不了对应知识库。
- 失败原因可在后台查看，不需要直接查询数据库。

### 1.2 对外部系统

- 使用 API Key 调用摄入、任务查询和检索接口。
- 使用 `external_id` 做长期幂等同步。
- 内容未变化时 no-op，内容变化时生成新版本并重新处理。
- 支持 Dify 外部知识库协议，也提供通用 REST API。
- 返回稳定的数据结构、明确错误码和可追踪请求 ID。

### 1.3 对开发和运维

- API、Worker、PostgreSQL、Qdrant、Embedding、Rerank、OCR 状态可见。
- pending、processing、done、failed、队列年龄和处理耗时可见。
- 每次修改模型、chunk、OCR、rerank 或 hybrid 策略，都能运行同一套评测。
- 更新、删除、重建等跨 PostgreSQL/Qdrant 操作具备补偿和重试能力。
- 迁移、配置和安全错误在启动阶段明确暴露。

---

## 2. 产品边界

### 2.1 本项目负责

- 多格式文件摄入和结构化解析。
- 文档身份、版本、更新、删除和重新处理。
- Dense、关键词、混合召回、RRF 和 rerank。
- 库级与租户级权限隔离。
- 检索评测、任务监控和运行诊断。
- Dify 和其他业务系统的统一接入。

### 2.2 本项目不负责

- 最终自然语言答案生成。
- Prompt 编排和 Agent 工作流。
- 通用网盘或完整办公文档管理。
- 企业门户和统一搜索前台。
- 全量知识图谱平台。
- 业务系统中的原始数据治理。

这些能力由 Dify、业务应用或独立平台负责，本项目只提供可靠的知识摄入与检索能力。

---

## 3. 当前架构

```text
后台 / 外部系统
       |
       v
认证 + API Key + Casbin 权限
       |
       v
文件解析 -> 标准化正文 -> 分片 -> PostgreSQL 文档/分片/job
                                      |
                                      v
                                   Worker
                                      |
                                      v
                              Embedding -> Qdrant

查询 -> Dense/Keyword 候选 -> RRF -> Rerank -> Dify/业务系统
```

### 3.1 已具备能力

- FastAPI、PostgreSQL、Qdrant、异步 Worker。
- Casbin 库级权限和 API Key。
- Dify 外部知识库兼容接口。
- Source enrichment 跨库正文补全。
- Dense 检索和标准/DashScope rerank。
- TXT、Markdown、JSON、CSV、DOCX、XLSX、文字版 PDF。
- DOCX 段落、章节、表格及可选图片 OCR。
- XLSX 工作表和表格感知切块。
- `external_id` upsert、任务查询和失败重试。
- 启动自检、Worker degraded、健康检查和任务统计。
- Hit@K、MRR、Recall@K 评测工具。
- 当前自动化测试基线：`112 passed`。

### 3.2 已完成的近期加固

- 用户 metadata 不能覆盖 Qdrant 系统字段。
- `chunk_overlap < chunk_size` 双层校验。
- 批量导入返回 success/partial/失败明细。
- 文件后缀白名单和大小写规范化。
- 公开注册默认关闭。
- metadata filter 支持完整算子，未知算子返回 422。
- 文件大小上限已配置，但仍需改为分块读取，才能真正避免大文件一次性占用内存。

### 3.3 当前关键缺口

- 部署数据库需要确认并升级到 Alembic head。
- 默认 JWT 密钥必须禁止用于可联网部署。
- `external_id` 与 content hash 的身份规则尚未完全统一。
- 并发摄入缺少数据库唯一约束与冲突恢复。
- 重建 collection 会错误激活历史 jobs。
- 文档缺少可靠的原文件/标准化正文留存。
- 重嵌入缺少 document revision/job generation，旧 Worker 可能写回旧向量。
- 删除 Qdrant points 失败后没有持久化补偿任务。
- 真实业务评测集尚未形成稳定基线。
- 当前“多租户”主要是多知识库权限，还没有正式 tenant/department 模型。

---

## 4. 建设原则

1. **正确性先于功能数量**：身份、更新、删除和权限不能依赖“通常不会并发”。
2. **评测先于调参**：没有统一数据集，不频繁切换模型和参数。
3. **检索与答案生成解耦**：本项目返回知识片段，上层负责组织答案。
4. **默认安全**：默认关闭公开注册、OCR 和实验能力，禁止默认密钥部署。
5. **故障可恢复**：跨 PostgreSQL/Qdrant 操作使用版本、outbox 或补偿任务。
6. **能力可关闭、可回退**：rerank、hybrid、OCR 失败时保留基础服务。
7. **按真实瓶颈建设**：GraphRAG、多模态、冷热存储只有在评测或规模证明需要时启动。

---

## 5. 推荐实施路线

## 阶段 0：形成可发布基线

### 目标

把当前大批改动整理成可迁移、可回滚、可验收的版本。

### 工作内容

- 将上传大小限制改为分块读取，超过上限立即停止并返回 413。
- 生成强随机 `JWT_SECRET`；非本地部署禁止默认密钥启动。
- 生产 HTTPS 设置 `COOKIE_SECURE=true`。
- 检查 `0001 -> Alembic head` 全链迁移。
- 实际部署数据库执行 `alembic upgrade head`。
- 清理 Ruff 明确问题和无用导入。
- 将当前改动按功能拆分提交。
- 默认关闭 OCR、rerank、公开注册时，API 与 Worker 仍可正常启动。

### 验收标准

- 全量 pytest 通过。
- Python 编译和前端语法检查通过。
- 新空数据库可从 `0001` 升级到 head。
- 已有数据库升级后 API、后台和 Worker 可启动。
- 不使用默认 JWT 密钥。
- Git 工作区形成明确版本检查点。

---

## 阶段 1：真实端到端验收与初版评测

### 目标

证明真实文件、真实服务和真实权限链路能够跑通，并建立第一份检索基线。

### 端到端验收

- 创建知识库并测试 embedding 配置。
- 使用后台和 API Key 分别上传 TXT、DOCX、XLSX、PDF。
- 验证 job：pending -> processing -> done。
- 验证 PostgreSQL chunk 数与 Qdrant points 数一致。
- 调用库内查询和 Dify `/retrieval`。
- 重复上传同一 `external_id`。
- 验证内容不变 no-op、内容变化重新入队。
- 验证权限不足返回 403。
- 验证 rerank 故障时回退。
- 验证删除后检索不再返回旧内容。

### 初版评测集

先建立 20-30 条高质量真实问题，不被“必须先凑够 100 条”阻塞：

| 类型 | 首批建议 |
|---|---:|
| 普通制度/说明 | 5 |
| Word/Excel 表格 | 5 |
| 编号、条款号、专业术语 | 5 |
| OCR 图片文字 | 3 |
| 更新与删除 | 3 |
| 权限隔离 | 独立硬性测试 |

稳定后扩展到 100 条以上，并划分开发集与保留验证集。

### 验收标准

- 真实链路完整跑通。
- 输出 dense 与 dense+rerank 基线报告。
- 权限隔离测试必须 100% 通过，不能作为平均准确率处理。

---

## 阶段 2：文档身份与生命周期可靠性

### 目标

确保外部部门可以长期同步资料，不重复、不串号、不残留旧向量。

### 2.1 文档身份规则

明确两种摄入语义：

- **提供 `external_id`**：只按 `(library_id, external_id)` upsert。
- **未提供 `external_id`**：才按 `(library_id, content_hash)` 做内容去重。
- 不同 `external_id` 即使正文完全相同，也代表不同业务文档。

建议使用 PostgreSQL 部分唯一索引：

```text
(library_id, external_id)
WHERE external_id IS NOT NULL AND deleted_at IS NULL

(library_id, content_hash)
WHERE external_id IS NULL AND deleted_at IS NULL
```

迁移前先扫描历史重复记录，输出可操作报告，不能直接让唯一索引迁移在生产库失败。

### 2.2 并发与版本

- 捕获唯一约束 `IntegrityError`，重新读取并发请求中的胜出记录。
- 增加 document revision。
- 每条 embedding job 记录目标 revision。
- Worker 写 Qdrant 前检查 revision，旧 job 自动标 superseded。
- 文档状态只允许由当前 revision 的 job 更新。

### 2.3 重建与重新解析

- 重建 collection 不修改历史 job。
- 每篇有效文档只创建一条新的 rebuild job。
- 区分“重新 embedding”和“重新解析+重新切块”。
- 保存标准化正文；需要保留原文件时使用受控文件存储/对象存储，不把大文件直接塞进业务表。
- 保存 `mime_type`、文件大小、原文件名、parser version、ingest strategy。

### 2.4 删除补偿

- 文档删除在 PostgreSQL 落 tombstone。
- 同事务写入 outbox/cleanup job。
- 清理 Worker 删除 Qdrant points，失败自动重试。
- 检索侧在最终一致窗口内具备防止已删除文档返回的保护策略。

### 验收标准

- 并发上传同一 `external_id` 只产生一个活动文档。
- 不同 `external_id` 的相同正文不会串号。
- 快速连续更新不会让旧向量复活。
- 重建不会重复消费历史任务。
- 删除失败会进入补偿队列并最终清理。
- 能从保存的源内容重新解析和切块。

---

## 阶段 3：混合检索 + RRF

### 目标

补齐条款号、资产编号、设备编号、表格字段和专业术语的精确召回能力。

### 推荐链路

```text
query
  |-- dense vector search
  |-- keyword / sparse search
  v
RRF fusion
  v
rerank
  v
Top-K + 来源 + 多路分数
```

### 技术选择

| 方案 | 适用条件 | 注意点 |
|---|---|---|
| Qdrant sparse vector | 本地 bge-m3 能稳定返回 sparse 权重 | 与现有 Qdrant 统一，但依赖模型能力 |
| PostgreSQL 全文检索 | 希望先做轻量关键词基线 | 中文需要合适分词方案，不能直接假设内置 FTS 足够 |
| 独立 BM25 服务 | 数据量和检索复杂度明显增长 | 运维成本最高，当前不优先 |

阿里云 embedding 是临时方案。最终 hybrid 实现应等本地模型能力明确后决定，避免为临时服务绑定架构。

### 实施内容

- 为 chunk 保存标准化关键词正文。
- dense 和 keyword 分别召回候选。
- 使用 RRF 融合，不直接相加不同尺度的原始分数。
- 融合后复用现有 rerank。
- 返回 `dense_rank/keyword_rank/rrf_score/rerank_score` 等诊断字段。
- 库级 hybrid 开关和候选参数。

### 验收标准

- 编号、条款和专业术语类问题显著优于 dense only。
- 普通语义问题不能明显退化。
- rerank 或 sparse 服务故障时可回退。
- 必须用阶段 1 的同一评测集输出 A/B 报告。

---

## 阶段 4：文档质量分级与多格式增强

### 目标

不同类型和质量的文档自动选择合适解析策略，避免垃圾 chunk 静默入库。

### 质量字段

- `text_extract_quality`
- `table_density`
- `ocr_required`
- `ocr_confidence`
- `layout_complexity`
- `manual_review_required`
- `ingest_strategy`
- `parser_version`

### 策略

| 文档情况 | 策略 |
|---|---|
| TXT、Markdown、干净 PDF | `plain_text` |
| 段落型 Word | `section_aware` |
| 表格型 Word/Excel | `table_aware` |
| 扫描 PDF | `ocr_basic` |
| 低清晰度或手写件 | `manual_review` |
| 不支持或高风险格式 | `reject` |

### 后续格式能力

- 扫描 PDF OCR。
- Word 表格单元格图片、页眉页脚和文本框。
- Excel 图片和复杂合并单元格。
- Parser 失败原因和人工复查队列。

### 验收标准

- 系统能够解释每篇文档采用了哪种解析策略。
- OCR 低置信度不会直接当作高质量文本入库。
- 不同策略的效果可通过评测对比。

---

## 阶段 5：正式多租户治理与外部同步

### 目标

从“多知识库权限”演进为真正的多部门治理。

### 能力

- tenant/department 实体。
- 知识库明确归属部门。
- 部门管理员与平台管理员分离。
- API Key scopes、过期、轮换和撤销。
- 租户级文档、容量、请求和并发配额。
- 限流、请求 ID、调用日志和审计。
- 租户级数据导出、删除和保留策略。
- 敏感文件、OCR 文本和原文件的访问控制。

### 验收标准

- 任意跨租户访问测试必须 100% 拒绝。
- API Key 只能访问授权租户和知识库。
- 配额与限流不会影响其他租户。
- 所有高风险操作有审计记录。

---

## 阶段 6：领域元数据与业务排序

### 目标

从通用语义相关性演进为可解释的业务相关性。

### 通用元数据

- `doc_type`
- `business_domain`
- `source_system`
- `effective_date`
- `version`
- `department`
- `security_level`

### 领域扩展

- 法律：法条号、案号、法院、裁判日期、案由、引用关系。
- 不良资产：债权人、债务人、抵押物、金额、执行状态、处置阶段。
- 施工安全：设备类型、违规项、风险等级、整改要求、法规依据。

### 排序原则

- 不直接把 dense、BM25、rerank 等不同尺度的原始分数线性相加。
- 优先使用 RRF、规则 boost、归一化或 reranker 特征。
- 所有业务权重都可关闭、可解释、可评测。

---

## 6. 条件触发的实验路线

以下内容不是主线承诺，只有在评测或业务需求证明价值后才启动。

### 6.1 GraphRAG

适合强关系问题的小范围试点，例如法律引用、债权债务、设备违规与整改关系。

触发条件：

- hybrid+rerank 对关系型问题仍明显不足。
- 已有独立关系问题评测集。
- 能控制实体消歧和关系污染。

### 6.2 多模态理解

适合施工现场照片、合同签章、报表截图和 PDF 图表。

触发条件：OCR 文本无法满足业务问题，且业务能承担模型成本和隐私风险。

### 6.3 热冷存储和增量索引

触发条件：

- 文档达到几十万级或 chunk 达到千万级。
- 队列长期堆积。
- 全量重建成本不可接受。
- 每日更新量成为明确瓶颈。

---

## 7. 测试体系

### 7.1 单元测试

- 文件解析、表格和 OCR。
- splitter 和参数边界。
- external_id/content hash 身份规则。
- payload 保留字段。
- rerank、RRF 和 filter 映射。
- eval 指标。
- Worker revision 判断。

### 7.2 集成测试

- FastAPI + PostgreSQL。
- Worker + PostgreSQL + Qdrant。
- Alembic 从空库和已有版本升级。
- 唯一约束下的并发摄入。
- outbox/cleanup 失败重试。
- rerank、embedding、OCR 故障降级。

### 7.3 端到端测试

```text
建库 -> 授权 -> 上传 -> Worker -> Qdrant -> 检索 -> Dify
                                      |
                                      v
                             更新 -> 删除 -> 补偿
```

### 7.4 检索质量测试

- dense only
- dense + rerank
- keyword only
- hybrid + RRF
- hybrid + RRF + rerank
- 领域增强排序

指标：Hit@1、Hit@3、Hit@5、MRR、Recall@K，以及表格、编号、OCR 分类指标。

---

## 8. 运行与安全指标

上线前必须为以下项目确定具体目标值，不能只写“稳定”或“较快”：

| 类别 | 指标 |
|---|---|
| 查询 | p50/p95/p99 延迟、错误率、超时率 |
| 摄入 | 成功率、单文档处理耗时、每分钟 chunk 数 |
| 队列 | pending 数、最老任务年龄、重试次数 |
| 一致性 | 更新后旧向量消失时间、删除最终完成时间 |
| 权限 | 越权测试通过率必须 100% |
| 可用性 | API/Worker/Qdrant/Embedding 可用率 |
| 安全 | 默认密钥检测、API Key 过期轮换、审计覆盖率 |
| 恢复 | PostgreSQL/Qdrant/原文件备份与恢复演练 |

目标值应根据真实数据量和部门 SLA 确定，不能在没有压测数据时拍脑袋填写。

---

## 9. 下一轮具体任务

### 第一组：关闭当前发布风险

1. 将上传改为分块读取并补 413 测试。
2. 生成强 JWT secret，加入默认密钥启动保护。
3. 确认并升级 Alembic head。
4. 整理并提交当前 112-test 基线。

### 第二组：身份和任务正确性

1. 明确 external_id/content hash 去重规则。
2. 历史重复数据预检。
3. 增加部分唯一索引和并发冲突恢复。
4. 修复 rebuild：每文档只创建一条新 job，不修改历史 job。

### 第三组：生命周期设计

1. document revision/job generation。
2. 原文件或标准化正文保存方案。
3. 删除 outbox/cleanup worker。
4. 重新解析与重新 embedding 的接口边界。

完成以上三组后，再正式实现 hybrid search。

---

## 10. 最终验收标准

| 能力 | 验收标准 |
|---|---|
| 摄入 | 支持格式稳定解析，异常返回明确错误和失败明细 |
| 身份 | external_id 并发同步不重复、不串号 |
| 版本 | 更新只允许当前 revision 生效，旧向量不会复活 |
| 删除 | PostgreSQL 与 Qdrant 最终一致，失败可补偿 |
| 检索 | dense、keyword、hybrid、rerank 可配置和回退 |
| 来源 | 结果可追溯到文档、章节、表格、版本和更新时间 |
| 权限 | 跨库、跨租户和 API Key 越权测试 100% 拒绝 |
| 评测 | 有真实数据集、版本化报告和策略对比 |
| 运维 | 组件状态、任务、队列年龄、耗时和错误可见 |
| 安全 | 无默认密钥、公开注册默认关闭、密钥可轮换 |
| 外部接入 | Dify 和业务系统可稳定摄入、轮询和检索 |

---

## 11. 最终一句话定位

> `vectorDatabase` 是面向公司多个部门的知识检索基础设施：资料能可靠进入，文档身份和版本不会混乱，问题能通过可评测的多路检索准确找回，来源能够核查，权限能够隔离，故障能够恢复，上层 Dify、办公系统、客服系统和 Agent 平台可以放心调用。

---

## 12. 结构化文档解析与质量治理专项规划

> 本节是 2026-07-29 增补的正式专项规划，细化并替代阶段 4 中关于文档质量和多格式解析的早期描述。
> 面向智能体的英文执行版见：`docs/roadmap/vector_knowledgebase_roadmap_v3.en.md`。
> 中英文版本必须使用相同的里程碑编号 `SDP-M1` 至 `SDP-M8`；发生冲突时，以中文版的产品边界和英文版的工程约束共同解释，不允许智能体自行扩大范围。

### 12.1 为什么现在做

项目已经具备以下基础，不需要重新建设：

- `Document -> DocumentRevision -> DocumentBlock -> Chunk -> EvidenceUnit` 正式数据链路。
- `current_revision_id/latest_revision_id` 双指针、原文件快照和 Revision 生命周期。
- `DocumentBlock.parent_block_id/content/position/parser_name/parser_version` 等结构字段。
- PDF 逐页文本与 OCR、DOCX 段落/标题/表格、XLSX Sheet/行号解析。
- Dense + Keyword + RRF + Reranker 检索链路。
- Summary/Outline Knowledge Artifact。
- 精确 `content_hash` 去重和 `external_id` 身份规则。

当前真正的问题是：

1. 上游解析器已经产生页码、表格和章节等结构，但正式写入路径仍把 Block 固定写成 `paragraph`、Chunk 固定写成 `text`，解析器身份仍是 `legacy/v0.2-m2`。
2. PDF 主要依赖 `pypdf.extract_text()` 和逐页 OCR，没有稳定表达阅读顺序、表格单元格、图片/图注、公式和页内坐标。
3. 文档进入 Embedding 前没有正式、可版本化的解析质量报告和自动门禁。
4. 现有解析测试验证具体函数行为，但没有独立的真实文档基准、Gold 标注、指标计算和冻结发布门槛。
5. Summary/Outline 已经生成，但尚未作为文档级/章节级召回层参与主检索。
6. 只有精确去重，没有近似重复候选组和版本族识别。

### 12.2 不可违反的原则

1. **解析结果是正式知识资产**：Markdown 和扁平文本只能作为导出或调试格式，不能成为唯一中间表示。
2. **原文不可恢复性**：解析阶段丢失的表格关系、阅读顺序和来源位置，后续 Embedding 或 LLM 无法可靠恢复。
3. **无人审核约束**：生产系统不能产生无人处理的 `needs_review` 队列；不确定性必须降低召回率，不能降低事实精度。
4. **先评测后换引擎**：未建立同一基准上的 A/B 数据前，不以 Docling、PyMuPDF 或其他解析器名称替代质量证明。
5. **Revision 不可变**：解析器、配置、质量报告、Block 和 Chunk 都绑定一个 Revision；重新解析必须生成新 Revision。
6. **发布前门禁**：未通过解析质量门禁的 Revision 不得进入 Embedding、图谱抽取、摘要、分类或正式检索。
7. **兼容优先**：现有 Dify、检索、Evidence、Graph、清理和 Revision 可见性契约不得被结构化解析改造破坏。
8. **结构不扁平化**：表格、图片、公式和组合证据不能为了复用文本表而被错误表示成多个相互独立的事实。

### 12.3 目标链路

```text
原文件不可变快照
  -> 文件类型与安全检测
  -> Parser Router
  -> ParsedDocumentV1 + ParsedBlockV1[]
  -> ParseQualityReportV1
  -> 自动质量门禁
       |-- accepted: 允许继续
       |-- degraded: 仅满足冻结的最低安全门槛时继续
       `-- rejected: 停止下游处理，保留原因与可重试信息
  -> DocumentRevision + DocumentBlock
  -> 结构感知 Chunk + EvidenceUnit
  -> Embedding / Summary / Outline / Classification / Graph
  -> Dense + Keyword + RRF + Reranker
```

### 12.4 标准结构化中间层

#### ParsedDocumentV1

至少包含：

```text
contract_version
parser_name
parser_version
parser_config_hash
source_file_hash
mime_type
page_count
blocks
diagnostics
```

#### ParsedBlockV1

至少包含：

```text
block_id
parent_block_id
sequence
block_kind
raw_block_type
text
structured_content
title_path
page_start/page_end
source_start/source_end
position/bbox
parse_confidence
attributes
```

首版支持的 `block_kind`：

```text
document_title
heading
paragraph
list
table
figure
caption
formula
page_header
page_footer
footnote
unknown
```

约束：

- `block_id` 在同一 Revision 内稳定且唯一，不使用随机顺序作为长期身份。
- `parent_block_id` 必须指向同 Revision 的先前或合法父 Block，不允许环。
- `structured_content` 保存表格行列、列表层级、图片引用等结构；`text` 是检索投影，不替代结构。
- `position` 首版继续复用 JSONB，稳定后再决定是否拆独立列；避免为尚未校准的数据先做大迁移。
- `parse_confidence` 只能表示解析器对当前 Block 的提取置信度，不能冒充事实真实性。

### 12.5 解析质量报告与自动门禁

每个 Revision 必须有一个不可变 `ParseQualityReportV1`，至少记录：

```text
parser route and fallback chain
total_page_count
text_page_count
ocr_page_count
text_coverage
block_count by kind
table_count
figure_count
formula_count
unknown_block_count
unparsed_page_count
reading_order_confidence
source_location_coverage
low_confidence_block_count
warning_codes
hard_failure_codes
quality_score
quality_status
policy_version
```

生产状态只允许：

| 状态 | 含义 | 下游行为 |
|---|---|---|
| `accepted` | 通过冻结门槛 | 允许创建 Chunk 和下游 Job |
| `degraded` | 存在非致命损失，但仍满足最低门槛 | 按策略允许；检索结果携带降级标记 |
| `rejected` | 无法证明解析结果可用 | 不进入 Embedding 和正式知识链路 |

以下情况必须自动拒绝，不等待人工：

- 文件损坏、加密且无法读取。
- 所有页面无有效文本且 OCR 无结果。
- 结构或来源坐标违反契约。
- Block 父子关系成环或跨 Revision。
- 关键页面未解析且超过冻结门槛。
- 解析器输出包含非有限置信度、越界坐标或不受支持的结构。

初期不得拍脑袋冻结 `quality_score` 数值。先在 Gold 数据集计算各指标分布，再形成独立、版本化、可回滚的 `parse-quality-policy-v1`。

### 12.6 Parser Router

首批路由：

| 文件类型 | 首选路径 | 回退路径 |
|---|---|---|
| TXT/Markdown | 原生文本适配器 | 无；编码失败即拒绝 |
| JSON/CSV | 结构化记录适配器 | 严格模式失败即拒绝 |
| PDF 原生文本 | 布局感知 PDF 适配器 | 当前 pypdf 页级适配器 |
| PDF 扫描件 | 页面渲染 + OCR | OCR 不可用或超限即拒绝 |
| DOCX | OOXML 顺序、标题、表格、图片适配器 | 受控扁平文本兼容路径 |
| XLSX | Sheet/区域/合并单元格适配器 | 首行表头兼容路径 |
| PPTX | 页面、文本框、表格、备注适配器 | `SDP-M8` 前不支持即明确拒绝 |

路由必须冻结：

```text
router_version
selected_parser
selected_parser_version
fallbacks_attempted
parser_config_hash
reason_codes
```

多解析器不是“全部运行后让 LLM 选择”。只有在评测证明收益时，才允许对特定文档类别运行有限 A/B 或确定性 fallback。

### 12.7 Block、Chunk 与 Evidence 写入

- `PreparedChunk` 扩展或替换为携带 Block 投影的严格 DTO。
- `DocumentBlock.block_kind/parent_block_id/content/position/parser_*` 必须来自可信 Parser Adapter，不再固定写死。
- 一个 Block 可以产生多个 Chunk；一个 Chunk 可以关联多个 Block；继续使用 `chunk_blocks`。
- Chunk 必须保留：

```text
chunk_kind
title_path
page_start/page_end
source_start/source_end
position
source_span_hash
parser identity
quality status
```

- 表格 Chunk 可以重复表头用于召回，但 Evidence 的 source span 必须指向真实原始区域。
- 图谱抽取、引用和 Evidence Resolver 继续绑定服务器拥有的 Revision、Block、Chunk 和 source span。
- 旧 Revision 保留原 parser identity；禁止用新解析器身份覆盖历史行。

### 12.8 解析评测体系

建立独立目录：

```text
eval/document_parsing/
  datasets/
  fixtures/
  gold/
  policies/
  results/
  scripts/
```

首批 Gold 类型：

- 文字版单栏 PDF。
- 双栏论文 PDF。
- 扫描 PDF。
- 混合文本/扫描页面。
- 跨页表格、合并单元格和重复表头。
- 页眉页脚、脚注、复杂编号列表。
- 图片与图注。
- 公式。
- DOCX 标题、表格、文本框和图片。
- XLSX 多 Sheet、多级表头、隐藏行列和合并单元格。
- 损坏、加密、乱码和空文件。

核心指标：

```text
text completeness
heading hierarchy accuracy
block type macro F1
table structure accuracy
reading order accuracy
page localization accuracy
source span exact/overlap accuracy
OCR character/word error rate
parse rejection correctness
downstream retrieval Hit@K delta
```

发布流程：

```text
calibration
  -> freeze parser/config/policy/dataset hashes
  -> three independent post-freeze runs
  -> compare current accepted baseline
  -> release or reject
```

不得只用合成测试通过作为生产解析器发布依据。

### 12.9 文档质量、来源权威度与排序

不建立人工审核队列。文档级质量由两类信号组成：

1. **解析质量**：系统计算，来自 `ParseQualityReportV1`。
2. **来源权威度**：由受信摄入配置或业务系统元数据声明，并经过 Schema 校验。

建议字段：

```text
source_authority
publication_status
effective_date
expiration_date
is_official
quality_policy_version
```

排序原则：

- 权威度不能把不相关文档排到相关文档之前。
- 先做候选过滤和 Rerank，再在可解释范围内做小幅业务排序。
- 所有 boost 必须可关闭、可审计、可评测。
- `draft/expired/rejected` 默认不能进入正式检索。

### 12.10 重复文件与版本族

保留现有精确规则：

- 有 `external_id`：按业务身份 upsert。
- 无 `external_id`：按精确 `content_hash` 去重。

新增候选识别，不自动合并：

```text
normalized_text_hash
source_external_id
supersedes_revision_id
duplicate_group_id
near_duplicate_score
duplicate_reason
```

区分：

- 完全相同文件。
- 格式改变但标准化内容相同。
- 同一业务文档的新 Revision。
- 不同来源的近似副本。
- 内容有实质变化的相关文档。

近似重复只生成自动分组和检索去冗余信号；在没有可靠业务身份时，不删除、不覆盖、不合并 Evidence。

### 12.11 分层检索

现有 Summary/Outline 先作为资产保留，解析质量稳定后再做实验：

```text
query
  -> document/summary candidate retrieval
  -> section/heading candidate retrieval
  -> chunk retrieval within candidate scope
  -> Dense + Keyword + RRF
  -> Reranker
  -> Evidence and visibility checks
```

进入正式实现的触发条件：

- 全局 Chunk 检索在“总结整篇、跨章节比较、某文档全部资料”问题上有稳定失败数据。
- Summary/Outline 与 Revision、权限和生命周期完全绑定。
- 分层检索在同一保留集上显著提高目标指标，且普通事实问题不明显退化。

不得只索引摘要，也不得在没有评测时替换当前主检索。

### 12.12 多模态后续边界

`SDP-M8` 之前只做结构保真和 OCR，不承诺通用视觉理解。后续候选能力：

- 图片、图注和正文引用关系。
- 图表数据和视觉问答。
- 公式原文、LaTeX 投影和位置。
- PPTX 页面、文本框、表格、备注。

启用条件：

- OCR/文本解析无法回答的真实问题已形成独立评测集。
- 本地或受控模型满足隐私、成本和延迟要求。
- 图片资产、权限、删除和 Revision 生命周期已纳入现有治理。

### 12.13 里程碑与依赖

#### SDP-M1：契约与基准盘点

- 冻结 `ParsedDocumentV1/ParsedBlockV1/ParseQualityReportV1` Schema。
- 记录当前每种格式的真实输出和结构损失。
- 建立首批 Gold 文档、标注规范和 evaluator 骨架。
- 不改变生产写路径。

验收：契约严格、额外字段拒绝、样本和指标可重复运行。

#### SDP-M2：结构化写入

- Parser Adapter 输出严格 DTO。
- 正式写入真实 `block_kind/parent/content/position/parser identity`。
- 保持 Chunk、Evidence、Revision、Dify 和现有检索兼容。
- 增加 legacy backfill/read compatibility，不改写历史语义。

验收：DOCX/XLSX/PDF 结构在数据库中可检查，Evidence 定位不退化。

#### SDP-M3：质量报告与自动门禁

- 持久化 Revision 级质量报告。
- Embedding 前执行 fail-closed 门禁。
- 诊断 API 和后台显示安全错误码、计数和策略版本。
- `rejected` 不创建下游 Job；`degraded` 行为由冻结策略控制。

验收：没有新的无人处理 review 队列；失败可重试但不会静默入库。

#### SDP-M4：解析路由与格式增强

- PDF 原生/扫描/复杂布局路由。
- DOCX 标题、列表、表格、图片/图注增强。
- XLSX 多级表头、合并单元格和区域识别。
- 只有基准证明后才接入新第三方 Parser。

验收：每个路由可解释、可回放、可降级，资源上限和错误边界明确。

#### SDP-M5：评测冻结与发布门槛

- 完成 Gold evaluator、校准、冻结策略和三次独立运行。
- 同时报告解析指标和下游 Retrieval/Evidence 指标。
- 形成 parser release evidence。

验收：新路径相对 accepted baseline 有可复核收益，无关键兼容性退化。

#### SDP-M6：版本族与检索去冗余

- 增加 normalized hash 和近似重复候选。
- 生成 duplicate/version family，不自动合并。
- 检索结果可按同族折叠，但保留来源和版本。

验收：精确身份规则不变，近似识别误合并率满足冻结门槛。

#### SDP-M7：分层检索实验

- 为 Summary、Section 和 Chunk 建立可回退候选链。
- 权限、Revision、Publication 和 Evidence 过滤贯穿所有层。
- 与当前 Hybrid 主链做 A/B。

验收：目标问题提升，普通问题无明显退化，关闭开关后完全回到当前行为。

#### SDP-M8：多模态试点

- 只选择一个有真实 Gold 数据的业务场景。
- 绑定图片/图表/公式资产与 Revision、权限和 Evidence。
- 评估本地模型和受控外部模型。

验收：价值、成本、隐私和删除一致性全部满足要求后才扩大。

依赖顺序：

```text
SDP-M1 -> SDP-M2 -> SDP-M3 -> SDP-M4 -> SDP-M5
                              |
                              +-> SDP-M6
SDP-M5 + SDP-M6 -> SDP-M7
SDP-M5 -> SDP-M8
```

### 12.14 数据迁移与兼容策略

- 新表/列必须先 nullable 或具备明确默认值，再通过受控 backfill 收紧。
- 历史 `legacy/v0.2-m2` Block 不伪装成新 Parser 输出。
- 旧 Revision 继续可读、可引用、可清理；新 Revision 使用新契约。
- Qdrant payload 增加字段时保持旧 payload 可见性和过滤兼容。
- Parser release 不原地重写当前 Revision；需要重解析时创建新 Revision，并经过现有发布切换。
- 回滚只切回旧 current Revision，不删除新失败 Revision 和诊断记录。

### 12.15 可观测性、安全与资源约束

必须提供：

```text
parse duration by parser/file type
accepted/degraded/rejected counts
OCR pages and failures
unknown/unparsed block counts
quality score distribution
fallback count
parser version distribution
downstream jobs blocked by parse gate
```

安全要求：

- Parser 错误不回显正文、密钥、内部路径或原始模型响应。
- 解压型格式限制文件数、展开大小、嵌套层数和压缩比。
- PDF/OCR 限制页数、DPI、像素、时间和内存。
- 第三方 Parser 必须在受控进程或容器运行，并有超时和资源上限。
- 原文件、Block、OCR 文本和评测样本沿用 Library/Organization 权限和保留策略。

### 12.16 明确不做

- 不因为论坛建议立即更换 Embedding、Qdrant、RRF 或 Reranker。
- 不把所有文档同时交给多个昂贵解析器。
- 不用 LLM 猜测缺失表格单元格、页码或来源坐标。
- 不建立依赖人工逐条核实的解析队列。
- 不在没有 Gold 数据时微调 Embedding/Reranker。
- 不把 Summary 当作唯一索引。
- 不在同一里程碑同时实现解析重构、近似去重、分层检索和多模态。

### 12.17 下一步执行顺序

下一次进入实现时只启动 `SDP-M1`，产出：

1. 严格 Schema 与示例。
2. 当前 Parser 能力/损失矩阵。
3. Gold 标注规范。
4. 最小 evaluator CLI。
5. 首批真实 fixture 清单。
6. `SDP-M2` 所需迁移影响分析。

`SDP-M1` 验收前，不进入生产写路径修改。

---

## 13. 后台前端可靠性与代码治理专项规划

本专项处理后台页面中已经确认的导航、状态表达和代码可维护性问题。目标不是重做一套界面，而是让用户看到的内容与真实接口状态一致，让后续逐页确认和修改能够在稳定边界内进行。

英文智能体执行规划位于：

```text
docs/roadmap/vector_knowledgebase_roadmap_v3.en.md
```

对应里程碑编号为 `UIR-M1` 至 `UIR-M4`。中文本文负责产品决策、优先级和用户可见结果；英文规划负责实施边界、文件范围、测试和验收要求。

### 13.1 已确认的产品决策

1. 登录成功后的第一个页面固定为“智能问答”。
2. 访问后台根路由时必须自动进入智能问答，不能出现只有侧栏、主区域空白的页面。
3. 当前后台按 PC 页面建设，不新增手机端或窄屏响应式适配任务。
4. 图谱区域必须按照真实接口结果显示，不能同时提示“不可用或不存在”又展示实体。
5. 新增、删除、重试、修改等写操作由用户逐页确认，确认到具体页面后再进入真实后端联调和修改。
6. 以下四个前端 API 方法暂时保留，不纳入清理范围：

```text
myPermissions
listMyOrganizations
getGraphGovernanceAction
getGraphPublication
```

7. 代码拆分必须保持现有接口、权限、字段和用户行为，不能借重构改变业务规则。

### 13.2 当前需要解决的问题

#### 默认入口不可靠

后台根路由当前可能只显示整体布局和侧栏，主内容区域为空。文档、代码和测试对默认首页也存在不同说法。

目标结果：

- 根路由和登录成功后的默认跳转统一进入智能问答。
- 路由代码、导航测试和后台说明文档采用同一个约定。
- 用户仍可通过菜单进入其他有权限的页面。

#### 图谱状态表达不真实

图谱页面目前可能把“功能未启用”“没有图谱数据”“状态接口失败”和“部分数据仍可用”混成同一条提示。

目标状态必须严格区分：

| 实际情况 | 页面行为 |
|---|---|
| 请求成功且存在实体/关系 | 显示真实图谱，不显示不可用提示 |
| 请求成功但没有实体和关系 | 不渲染空图谱，显示明确的暂无数据状态 |
| 图谱功能未启用 | 不渲染图谱，显示功能未启用 |
| 图谱主数据请求失败 | 不把失败伪装成无数据，显示加载失败和重试 |
| 辅助状态接口失败但主数据有效 | 保留主数据展示，仅标明部分状态暂不可用 |

任何提示都必须由真实响应、功能开关或明确错误产生，不能根据默认值猜测。

#### 异步页面状态不完整

页面不得只在“有数据”时表现正常。所有读取页面最终都应明确处理：

```text
idle
loading
success-with-data
success-empty
partial-error
fatal-error
refreshing
```

写操作还应处理：

```text
submitting
success
failure
conflict
```

本专项先统一读取状态和重试语义。写操作的真实联调按用户逐页确认推进，不在一次大改中批量假定。

#### 超大组件难以继续维护

当前需要优先关注：

```text
GraphGovernance.js
Import.js
SchemaLifecycle.js
KnowledgeCatalog.js
Chat.js
Documents.js
```

拆分目标是形成清晰的页面编排层、状态/请求层、纯数据转换层和可复用展示组件。行数减少不是单独验收标准；行为不变、依赖清楚、测试可定位才是验收标准。

### 13.3 页面状态统一原则

1. `loading` 期间保留稳定页面骨架，不显示“暂无数据”。
2. 只有请求成功并确认结果为空时，才显示空状态。
3. 请求失败必须显示错误状态，不得清空数据后伪装成空状态。
4. 刷新失败时，如已有可用旧数据，应保留旧数据并提示刷新失败。
5. 多接口页面必须区分主数据失败和辅助数据失败。
6. 重试按钮必须重新调用对应真实读取接口，并防止重复并发请求。
7. 路由或知识库切换后，过期请求结果不得覆盖新页面状态。
8. 错误文案不得暴露内部路径、堆栈、密钥或原始服务响应。

### 13.4 代码拆分与清理原则

- 先冻结并测试现有行为，再拆分组件。
- 优先提取纯函数、状态投影和请求协调逻辑，不先做全局样式重写。
- 每个阶段只拆一个明确功能区域，避免一次修改全部大型页面。
- 不创建没有实际复用价值的通用组件。
- 删除代码前必须同时检查静态引用、动态属性调用、测试、预览模式和文档约定。
- 本次明确保留的四个 API 方法不得因“当前没有生产引用”而删除。
- 不删除后端路由，除非后续有单独的接口废弃计划和兼容性证明。
- 不把移动端适配、视觉换肤或新的业务功能混入本专项。

### 13.5 里程碑与小规划

#### UIR-M1：默认入口与状态基线

- 将后台根路由和登录后默认入口统一为智能问答。
- 同步导航代码、测试和后台说明文档。
- 建立页面读取状态清单，记录每个页面已有和缺失的加载、空数据、错误及重试行为。
- 冻结四个保留 API 方法和“不做移动端”的边界。

验收：直接打开后台、登录后进入后台和刷新根路由都能进入智能问答，不再出现空白主区域。

#### UIR-M2：统一加载、空数据与错误状态

- 建立小而明确的异步状态投影约定。
- 先选择一个简单列表页和一个多接口页面完成实现样板。
- 再按页面逐步迁移，避免一次性改写全部页面。
- 增加失败、空结果、刷新失败、重复请求和过期响应测试。

当前进度（2026-07-29）：第一批样板页已经完成。用户管理页不再把请求失败显示成“暂无用户”；运营总览能够区分全部失败和部分失败，保留成功数据，并只重试失败的读取项。第二批搜索、知识库列表和 API Key 的读取状态实现及自动化测试已经完成；知识库列表和 API Key 的 PC 刷新检查通过。搜索失败态仍需在只保留一个 5599 预览进程的干净环境中补一次浏览器复核，复核完成前不算通过完整浏览器门禁。

验收：页面不会把“正在加载”“没有数据”和“请求失败”显示成同一种结果，重试会调用真实读取接口。

#### UIR-M3：图谱真实状态与组件拆分

- 按 13.2 的状态表重做图谱状态投影。
- 主数据有效时不得因辅助状态失败而隐藏图谱。
- 没有实体和关系时不渲染空画布。
- 将 `GraphGovernance.js` 按图谱浏览、实体、关系、审核、发布和请求协调边界逐步拆分。

验收：图谱提示与真实数据一致；现有权限、治理命令、发布流程和接口字段不变；相关回归测试全部通过。

#### UIR-M4：其余大型页面治理与安全清理

- 按风险和用户确认顺序拆分 `Import.js`、`SchemaLifecycle.js`、`KnowledgeCatalog.js`、`Chat.js` 和 `Documents.js`。
- 逐页核实展示字段、读取接口和已确认的写操作。
- 只删除经过引用、测试、预览和兼容检查后确认无用的代码。
- 保留本专项明确列出的四个 API 方法。

验收：大型页面具备清楚的模块边界；不存在已确认的无效展示；没有删除仍受支持的接口能力。

依赖顺序：

```text
UIR-M1 -> UIR-M2 -> UIR-M3 -> UIR-M4
```

`UIR-M1` 可以在单独获得实施确认后启动。它不得与结构化解析 `SDP-M1` 合并为同一个实现任务。

### 13.6 验证要求

每个 `UIR` 里程碑至少需要：

- 修改符号前执行 GitNexus upstream impact analysis。
- HIGH 或 CRITICAL 风险在编辑前明确报告。
- 页面状态使用行为测试验证，不只检查源码字符串。
- 相关 `admin-ui` Node 测试通过。
- 使用 PC 视口进行浏览器验证。
- 检查浏览器控制台错误和未处理 Promise。
- 涉及接口时核对 FastAPI 路由和响应 Schema。
- 提交前运行 `gitnexus_detect_changes()` 和 `git diff --check`。

写操作只有在用户确认对应页面后，才增加真实后端联调验收。

### 13.7 明确不做

- 不做手机端或窄屏响应式适配。
- 不删除 `myPermissions`、`listMyOrganizations`、`getGraphGovernanceAction`、`getGraphPublication`。
- 不一次性重写整个后台。
- 不在组件拆分过程中更换 Vue、Element Plus 或当前零构建架构。
- 不用统一空数组掩盖权限错误、网络错误或后端异常。
- 不在未经逐页确认时批量改动新增、删除、重试和修改操作。

### 13.8 当前执行顺序

1. `UIR-M1` 已完成：默认入口、导航测试、后台说明和页面异步状态清单已经同步。
2. `UIR-M2` 第一批已完成：用户管理和运营总览已经建立读取状态样板。
3. `UIR-M2` 第二批实现已完成：搜索、知识库列表和 API Key 只修改读取状态，没有改写操作；英文证据见 `docs/roadmap/admin-ui-reliability/uir-m2-batch-b-evidence.en.md`。
4. 先在干净的 5599 预览进程上补验搜索失败态，再开始第三批 `Chat.js`、`Documents.js`、`Permissions.js`、`Jobs.js`。
5. `UIR-M2` 完成前，不启动 `UIR-M3` 图谱状态重做和大型组件拆分。
