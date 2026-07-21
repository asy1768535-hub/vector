# 多部门知识检索基础设施路线图 v3

> 项目：`vectorDatabase`  
> 定位：面向公司多个部门，提供可靠摄入、权限隔离、可评测检索和可追踪来源的知识检索基础设施。  
> 版本：v3  
> 更新日期：2026-06-22

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

