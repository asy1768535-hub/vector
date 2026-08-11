# Phase 1 完成后的 Phase 2 实施计划：Claim Graph Shadow Path

> 状态：Phase 1 已完成；Phase 2 仅规划，未实现。
>
> 日期：2026-08-07
>
> 本计划建立在 Phase 0 publication baseline 和 Phase 1 Evidence Hierarchy M0-M5 已通过的基础上。它只规划 raw claim 的保留与 shadow replay，不实现 Canonical Mapping、Rule Candidate、Hierarchical Retrieval、Context Packing 或 UI 改造。

## 1. 目标与不变条件

### 1.1 目标

在现有 graph extraction 之后增加一个可审计的 Claim Graph shadow path：凡是模型给出、引用了有效证据、且通过 revision/job/unit scope 校验的 raw claim，都要被保留，即使它不能映射到 frozen Schema 的 canonical relation，也不能可靠链接到 canonical entity。

目标流：

```text
文件/revision
  -> extraction unit
  -> raw model claim
  -> mention/evidence scope validation
  -> raw claim shadow persistence
  -> mapping candidate/schema-extension candidate
  -> 现有 candidate/validation/materializer/publication（保持原路径）
```

Raw claim 是“文档中的主张”，不是未经证据支持的现实事实。Claim Graph 不得把 claim 直接当作 published canonical relation。

### 1.2 零回归条件

- 现有 canonical candidate、materializer、publication planner、active publication 和 Graph API 的输入输出保持不变。
- Claim shadow 写入失败不能让 parser、extraction unit、job、materialization 或 publication 失败；失败只进入结构化 shadow error 计数和脱敏日志。
- shadow path 不改变现有模型 prompt、gold、semantic alignment、scorer 或 frozen Schema constraint。
- 现有 evidence scope、current revision、visibility/security 规则继续生效。
- claim、evidence 和 revision 都是 immutable 输入；关闭 shadow read/write 不删除历史 claim/evidence。
- 法律、医疗、资产只作为 fixture，不进入生产 allowlist、prompt 词表或 canonical 规则。

## 2. 只读架构审计结论

### 2.1 已审计复用点

以下是本次 fallback 审计确认的定义和调用边界：

| 边界 | 当前复用点 | 直接流程 |
|---|---|---|
| 模型协议 | `app/schemas/graph_extraction.py` 的 `GraphExtractionPayload`、`ExtractedEntityPayload`、`ExtractedRelationPayload` | provider response -> `graph_extraction_parser.parse_graph_extraction_output` |
| 解析/批处理 | `app/services/graph_extraction_parser.py`、`graph_extraction_batch_eval.py` | extraction unit -> JSON decode/schema validate -> batch map |
| 原始出现 | `GraphEntityOccurrence`、`GraphRelationOccurrence` 的 `raw_payload` | worker `_persist_candidate_result` -> occurrence/candidate aggregation |
| candidate | `GraphEntityCandidate`、`GraphRelationCandidate` | `stage_unit_candidate_occurrences` -> `recompute_job_candidate_aggregates` -> validation/routing |
| candidate evidence | `GraphEntityCandidateEvidence`、`GraphRelationCandidateEvidence` 与 `graph_candidate_evidence.py` | context ref/quote -> evidence resolution -> revision/chunk/block scope |
| 任务范围 | `GraphExtractionJob`、`GraphExtractionUnit` | library/document/revision/job/unit、idempotency、model/prompt/parser/config hashes |
| 正式证据 | `EvidenceUnit`、`EntityMention`、`RelationEvidence` | materializer/evidence read/publication |
| materialization | `graph_extraction_materializer.py` | 只有 validated、有效 ontology、有效 evidence 和可用 endpoint 才进入 canonical rows |
| publication | `graph_publication_planner.py`、activation/reconcile | 只读取正式 Entity/KnowledgeRelation/RelationEvidence，不应读取 shadow claim |
| 评测 | `scripts/graph_discovery_eval.py`、`graph_extraction_eval_runtime.py` | 脱敏 exchanges、prediction export、strict/semantic scoring、immutable artifact |

### 2.2 现有 candidate 边界是否足够

现有边界适合暂存 extraction 过程，但不足以作为长期 Claim Graph：

1. `GraphRelationOccurrence.raw_payload` 能保留一次模型 JSON，但 occurrence 由 `extraction_unit_id` 约束，candidate 聚合仍要求 frozen `relation_type_key`、source/target candidate 和 ontology version。
2. `GraphRelationCandidate` 的 `relation_type_key`、source/target candidate 是 canonical-oriented 字段；未知谓词、未知 endpoint 或不满足 frozen constraint 的 claim 会在 validation/routing 失败，不能仅靠它保留“原始但合法有证据”的 claim。
3. candidate evidence 当前主要保存 `context_ref`、quote、resolved evidence/chunk/block/source span 和 validation status；没有一等的 surface direction、negation、modality、qualifiers、valid/effective time 或 raw predicate 字段。
4. occurrence/candidate 的生命周期和 job 关联适合重试、聚合和 purge，不等同于跨 mapping 版本长期 immutable retention；删除 job/candidate 可能连带丢失 raw payload。
5. 现有 `EvidenceUnit`、`EntityMention`、`RelationEvidence` 保存的是已解析或已 materialize 的证据关系，不能承载没有 canonical relation 的 claim。

因此计划采用两阶段决策：M0/M1 先用纯 contract 和 candidate-boundary fixture 证明能否无损提取；若要满足跨 mapping、不可 canonicalize 保留、独立 replay 和 immutable retention，必须使用 additive claim persistence。当前审计的推荐是：保留 occurrence/candidate 作为短期过程数据，同时在 M2 以后增加最小 Claim 表；不要把 `raw_payload` 直接升格为长期公开 API。

## 3. Raw Claim Contract V1

### 3.1 记录结构

计划新增领域无关的 `RawClaimV1` 纯协议，字段应按以下分组组织。具体 Python 类型、长度和 JSON schema 在 M0 固化；未实现前不得假设生产已有这些字段。

```json
{
  "claim_id": "uuid",
  "library_id": "uuid",
  "document_id": "uuid",
  "document_revision_id": "uuid",
  "revision_no": 1,
  "content_scoped_claim_fingerprint": "sha256",
  "extraction_occurrence_id": "uuid",
  "extraction_occurrence_fingerprint": "sha256",
  "job_id": "uuid",
  "extraction_unit_id": "uuid",
  "source_mention": {
    "local_id": "string",
    "surface": "string",
    "entity_type_hint": "string|null",
    "evidence_ref": "string"
  },
  "raw_predicate": "string",
  "target_mention": {
    "local_id": "string",
    "surface": "string",
    "entity_type_hint": "string|null",
    "evidence_ref": "string"
  },
  "surface_direction": "source_to_target|target_to_source|undirected|unknown",
  "negation": {"value": false, "evidence_ref": "string|null"},
  "modality": {"value": "string|null", "evidence_ref": "string|null"},
  "qualifiers": [{"key": "string", "value": "JSON-safe", "evidence_ref": "string|null"}],
  "valid_time": {"start": "ISO-8601|null", "end": "ISO-8601|null", "evidence_ref": "string|null"},
  "effective_time": {"start": "ISO-8601|null", "end": "ISO-8601|null", "evidence_ref": "string|null"},
  "evidence_refs": [{"evidence_id": "uuid", "chunk_id": "uuid|null", "block_id": "uuid|null", "source_span": "object"}],
  "extractor_version": "string",
  "prompt_version": "string",
  "model_provider": "string",
  "model_name": "string",
  "model_config_hash": "sha256",
  "prompt_content_hash": "sha256",
  "parser_version": "string",
  "normalization_rule_version": "string",
  "ontology_snapshot_hash": "sha256|null",
  "claim_schema_version": "raw_claim_v1"
}
```

`modality.value` 和 qualifier keys 是模型输出/文档证据中的动态值，不是固定枚举。只有协议状态、方向 token、长度、JSON-safe、时间格式和 scope 是固定约束。模型没有证据时应为 `null`/空数组，不得推断。

### 3.2 不变量

- source/target mention 必须来自同一 extraction unit 的声明或可验证的 unit-local mention；不能用字符串相似度补造 endpoint。
- raw predicate 必须是非空、bounded 的原始表述；不能在 raw claim 层改写成 `related_to` 或其他通用谓词。
- `surface_direction` 记录文档表面语义，不由 canonical constraint 反推；无法确定时使用唯一稳定 token `unknown`。`unknown` claim 不得交换 endpoint，也不得进入 canonical relation；不能用不存在的方向猜测替代它。
- 每条 claim 至少有一个 evidence ref；每个 evidence ref 必须回到同一 library、document、document revision、job 和 extraction unit scope，并通过 Phase 1 locator/quote/hash 验证。单条 raw claim 不得跨 document 或 revision 引用证据。
- 跨文档关系不在本 Phase 合并：两个文档分别生成各自 scope 的 raw claims，后续 Phase 3 才能基于独立 claims 做 entity/canonical 聚合。
- claim 可以是 negated、alleged、possible、planned 等文档主张；否定和 modality 不能被压成 `supports` 事实。
- qualifier、时间和 mention payload 必须 bounded、JSON-safe，不复制完整文档、OCR 大数组或 storage locator。
- revision、job、unit、ontology snapshot、model/prompt/config 版本是 claim provenance，不允许后续 mapping 覆盖。
- raw claim core 永久 immutable；mapping、schema-extension 和 validation 结果不能覆盖 core 字段。
- `content_scoped_claim_fingerprint` 是稳定的内容/范围键：对 library、document、revision、source/target surface、raw predicate、surface direction、qualifiers、时间和 evidence identity 的 canonical JSON 求 hash；不包含 job、unit、model、prompt 或 config。它用于同一 revision 中对同一 surface claim 分组。
- `extraction_occurrence_id` 和 `extraction_occurrence_fingerprint` 是可审计的抽取发生键：包含 content-scoped fingerprint、job、unit、extractor、model、prompt、parser 和 config hashes。每次 rerun 都产生新的 occurrence；是否将相同 content fingerprint 的 occurrence 复用到同一 claim core，由 M1/M2 gate 决定，但任何情况下都不能丢失 occurrence provenance。
- 数据库唯一性必须分别约束 claim core 的 library/revision/content fingerprint，以及 occurrence 的 library/revision/extraction fingerprint；不能用包含 job/model 的 occurrence fingerprint 代替 content 分组键。
- raw claim 的有效性和 canonicalization 状态分离：`evidence_valid`、mapping decision 和 publication status 都是独立的 versioned projection/event，不是 raw claim core 的可变列。

## 4. 存储方案与推荐

### 4.1 方案 A：扩展现有 occurrence/candidate JSON

做法：在 `GraphRelationOccurrence.raw_payload` 或 candidate `proposed_properties` 增加 raw predicate、direction、modality、qualifiers、时间和 claim schema version；保留现有 candidate status。

优点：无需新表，复用 worker、scope、幂等和现有测试；适合 M0 contract spike 和短期 shadow replay。

缺点：字段不结构化且无法稳定查询；candidate 仍被 frozen ontology 和 endpoint 约束；purge/job 删除可能丢失 claim；无法清楚区分 raw claim 与 canonical candidate；长期 replay 需要依赖脆弱的过程 JSON。

### 4.2 方案 B：additive `raw_graph_claims` persistence

建议在 M1 gate 通过后设计最小 additive persistence，名称、字段和 migration 在实施阶段另行评审，本计划不生成 migration。逻辑上至少分为 immutable claim core、extraction occurrence 和独立 decision projection；可以在 migration 评审时决定是否用两张表或受约束的关联表，但不能把可变状态写回 raw claim core。至少应包括：

- claim core：`id`、`content_scoped_claim_fingerprint`、`claim_schema_version`、`library_id`、`document_id`、`document_revision_id`、`revision_no`、raw mention/predicate/direction/qualifier/time/evidence fields；
- extraction occurrence：`extraction_occurrence_id`、`extraction_occurrence_fingerprint`、`claim_id`、`job_id`、`extraction_unit_id`、extractor/prompt/model/parser/config/ontology hashes；
- source/target mention 的 bounded JSON 或稳定 mention-local reference；`raw_predicate`、`surface_direction`；
- negation、modality、qualifiers、valid/effective time 的 JSONB/结构化字段；
- evidence refs、locator hash/ID、quote hash，而不是不受限的原文复制；
- decision projection/event：独立引用 `claim_id` 和可选 occurrence，记录 evidence validation、mapping、schema-extension 的 decision version、reason、actor/run 和 created_at；不得更新 raw claim core；
- `UNIQUE(library_id, document_revision_id, content_scoped_claim_fingerprint)` 用于 claim core，`UNIQUE(library_id, document_revision_id, extraction_occurrence_fingerprint)` 用于 occurrence，避免跨 library/revision 碰撞；
- 按 `library_id, document_revision_id`、`job_id, extraction_unit_id` 和 projection decision/version 建索引，只有查询测量证明必要时才增加状态索引。

约束：外键必须限制到同 library/document/revision 的合法 scope；claim core 和 occurrence 的原始字段不可 update/delete 覆盖；decision projection/event 只能追加新版本。清理策略只能标记 retention/purge 状态并保留审计 hash，不能随 job cascade 默默删除已启用 shadow 的 claim。旧 revision 默认可读但仍服从权限和 retention policy。

### 4.3 推荐和决策 gate

推荐 B 作为满足 Phase 2 目标的长期边界，但不预先假定一定要立刻建表：

1. M0 先冻结 contract，并用 fixture 验证现有 occurrence/candidate 能否表达全部字段。
2. M1 运行短期 candidate-boundary replay，证明未知 predicate/endpoint、negation/modality/time、单 revision scope、content fingerprint 分组、occurrence 幂等和 candidate purge 的边界。
3. 只要 M1 证明任一字段会丢失、受 ontology 拒绝、无法独立 replay 或会随 job 删除，M2 才提交 additive claim table 设计和 migration proposal。
4. 如果 M1 能在不改变 canonical path 的情况下证明全部不变量，仍必须记录该证据；不得因为少写一张表而省略 immutable/raw claim retention contract。

## 5. Shadow Write 设计

### 5.1 Flag

增加前先复用现有 settings/library feature-flag 模式，建议提供：

- global `graph_claim_shadow_enabled=false`；
- library override `claim_graph_shadow`，三态 `inherit|enabled|disabled`；
- 独立的 shadow read/export flag，默认关闭，不能以 read flag 隐式打开 write。

默认关闭必须与现有 settings 默认值、健康检查和部署文档一致。automatic/required Schema confirmation 不得隐式开启 claim shadow。

### 5.2 双写位置和失败隔离

正式 extraction 成功处理 model payload 后，在现有 candidate staging 的旁路调用 `build/validate_raw_claims_v1`：

```text
provider response
  -> existing parse and candidate write (authoritative)
  -> shadow claim normalize/validate/persist (best effort)
```

shadow 写入应使用独立事务/savepoint 或独立异步 outbox，取决于当前 worker 延迟和事务边界；不得让 shadow exception rollback existing candidate/evidence。必须记录 `shadow_write_attempted/succeeded/failed/skipped` 计数和稳定 error code，不记录正文、quote、raw hash、storage path 或 secret。

延迟边界：同步旁路默认只做 bounded JSON/hash/scope 校验，p95 增量预算应在 fixture/real shadow 之前测量并写入配置；超预算时切换独立队列或跳过 shadow，不阻塞 extraction。不得在每条 claim 触发无界文档读取。

### 5.3 不能 canonicalize 的 claim

- source/target 可证实但 relation key 不在 frozen Schema：写入 raw claim，创建 `mapping_candidate` 记录/JSON（建议先作为 claim status projection），不进入 canonical relation。
- endpoint 可证实但类型未知：写入 raw claim，创建 `schema_extension_candidate`，附建议类型、原始 evidence refs、失败 reason；不创建通用 `Entity`。
- evidence 无效、scope 不一致、quote/hash 不匹配：不写入“合法 claim”；保存脱敏 failure artifact/counter，不能绕过 evidence gate。
- direction 不确定：保留原始 surface direction 和 ambiguity reason，禁止用交换 endpoint 提升 recall。
- 本 Phase 不实现 mapper、schema extension approval、自动 canonicalization 或 publication 读入。

## 6. Claim Replay Artifact

### 6.1 Artifact 结构

复用现有 graph discovery artifact 的 immutable/new filename、脱敏和 hash 模式，增加独立 `claim_shadow` 段，不改变旧 artifact schema 的既有字段语义。记录：

- artifact/run ID、fixture/library/revision/job/unit scope；
- claim schema、extractor/prompt/model/parser/config/ontology versions/hashes；
- request/response 只保留脱敏摘要、hash、token、latency、finish reason；不保存 API key、完整正文、quote 或未脱敏 response；
- raw claim core count、extraction occurrence count、evidence-valid count、invalid/rejected count 及稳定 reason counts；
- mapping candidate/schema-extension candidate count；canonical path count；shadow write failure count；
- content-scoped fingerprints、occurrence IDs/fingerprints、mention/evidence IDs 可使用短 hash/UUID，不能暴露 storage identity。

### 6.2 指标

对每个 fixture 和真实 shadow 分开报告，不覆盖旧 scorer 指标：

- raw relation recall/precision（按 content-scoped claim 分组，另报告 occurrence count）；
- source endpoint accuracy、target endpoint accuracy；
- surface direction accuracy；
- evidence validity/locator validity；
- negation accuracy；
- modality accuracy；
- qualifier accuracy；
- valid/effective time accuracy；
- unknown predicate retention rate、unknown endpoint retention rate、unknown direction retention rate；
- canonicalization coverage（诊断指标，不作为 raw extraction recall）；
- shadow write success/failure rate、p50/p95 latency、claim dedup rate。

gold fixture 可在 eval 层增加 raw-claim annotations，但不得修改现有 gold、semantic alignment 或 scorer 来制造 canonical 分数。strict canonical relation metrics 与 raw-claim metrics 必须同时存在。

## 7. Rollout、回滚与数据保留

1. **Fixture gate**：资产/涉法、医疗、普通资料各一套，另有未知 predicate、被动方向、unknown direction、否定/modality、表格/JSON locator fixture；两个文档分别生成独立 scope 的 claims，验证 claims 和 evidence 不串 document/revision/unit；证明领域只在数据中出现。
2. **Real-model shadow gate**：在隔离测试 library 开启 shadow，使用一次新的真实运行；canonical extraction/publication 仍走旧路径，失败 artifact 不伪造指标。
3. **Library canary**：按 library flag 灰度，观察 shadow success、invalid evidence、dedup、p95 和 publication baseline。
4. **Rollback**：先关闭 shadow read/write flag；旧 canonical/publication 继续使用现有路径。已写 claim/evidence 保留，只停止新写入和 shadow 查询。若必须清理，仅提供带 scope/fingerprint/hash guard 的计划或 dry-run，不做无条件 delete。
5. **Schema/model 变更**：claim contract 或 extractor 版本变化产生新版本/新 fingerprint，不更新旧 claim；replay 可比较不同版本。
6. **验收**：Phase 0 Required/Automatic publication baseline 在每个 rollout gate 继续通过；不能用 claim 数量或 entity-only publication 替代 canonical relation gate。

## 8. 可执行里程碑

### M0：现状 contract 与边界 fixture

**范围**：新增纯 `RawClaimV1` 协议/校验设计与 DB-free fixture，不接生产写路径，不建 migration。

**文件/符号**：优先在 `app/schemas/` 或独立 `app/services/` contract module；测试放 `tests/`。不得修改现有 extraction schema 的强制字段语义。

**测试**：合法 active/passive/unknown predicate、`unknown` direction、negation/modality/qualifier/time、同 revision evidence refs；非法 scope、跨 document/revision/unit evidence、空 predicate、未注册 direction token、超长/非 JSON-safe；确认 `unknown` 不交换 endpoint 且不能进入 canonical；M0 EvidenceLocatorV1 round-trip。

**风险**：HIGH（共享 extraction contract）；协议层失败可能改变 parser 行为。

**门禁/完成定义**：GitNexus impact 或 fallback caller audit、focused tests、Ruff、diff check；现有 graph extraction tests 全通过；确认 contract 可表达但尚未持久化 raw claim。

### M1：Candidate-boundary replay 与存储决策

**范围**：只用 fixture/内存或现有 occurrence JSON 做可丢弃 spike，验证 raw claim 从 parser/batch map 到 candidate/evidence 的字段保真和 scope/idempotency；不改 publication。

**文件/符号**：审计并必要时在 `graph_extraction_parser.py`、`graph_extraction_batch_eval.py`、`graph_candidate_aggregation.py` 增加纯 helper；任何生产写改动前重新做 impact。

**测试**：未知 relation key 不丢 raw predicate；端点未知不变成 `Entity`；`unknown` direction 不交换 endpoint 且不进入 canonical；同一 content fingerprint 的 rerun 分组与 occurrence fingerprint 幂等；跨 document/revision/unit 不混淆；candidate purge/failed job 后检查保留缺口；shadow latency 测量。

**风险**：HIGH（candidate aggregation/validation/worker）；如果需要改现有 JSON contract，需先报告兼容风险。

**门禁/完成定义**：产出可审计的“candidate reuse sufficient/insufficient”报告。按本次审计预期会判定长期 persistence 不足；未通过前不得建表。

### M2：最小 Claim persistence 与 shadow writer

**范围**：仅在 M1 判定不足后实施 additive claim core/occurrence persistence 及独立 migration；实现 immutable core、双 fingerprint、scope、evidence refs、版本字段、独立 decision projection、默认关闭 flag 和 best-effort shadow write。

**文件/符号**：新增 model/migration/settings/worker旁路服务/运维文档；复用现有 job/unit/evidence locator helper。不得改变 candidate/materializer/publication 的成功条件。

**测试**：migration upgrade/downgrade design review；apply/rollback transaction isolation；相同 content fingerprint 的 rerun occurrence 分离/可追溯；不同 revision separation；decision projection 追加新版本且不修改 core；shadow failure does not fail extraction；bounded payload/no secret；superseded historical revision read；flag off zero writes。

**风险**：CRITICAL（数据库、worker 事务、immutable retention）；必须先向用户报告 impact 和 rollback 方案。

**门禁/完成定义**：新的 claim rows 与现有 candidate/evidence 可按 ID/fingerprint 追踪；旧测试和 Phase 0 baseline 通过；没有任何 publication 输入变化。

### M3：Mapping/schema-extension candidate projection

**范围**：只生成引用 immutable claim core 的 versioned `mapping_candidate`/`schema_extension_candidate` projection/event；不更新 raw claim、不实现 canonical mapper、自动批准或 materialization。

**测试**：unknown predicate、unknown endpoint type、`unknown` direction 加 ambiguity reason、missing evidence 的分类；extension candidate 不进入 Entity/KnowledgeRelation/GraphPublication。

**风险**：HIGH（容易误接入 materializer/publication）。

**门禁/完成定义**：raw claim core 和 extraction occurrence 即使 mapping 失败仍可读；decision projection 可按版本追踪且不修改 core；canonical path 统计和行为与 baseline 完全一致。

### M4：Replay artifact 与评测

**范围**：增加 claim shadow artifact 的脱敏序列化、replay loader 和独立 raw metrics；保留旧 scorer 和现有 artifact 兼容。

**测试**：artifact sanitization、immutable filename、failed run artifact、hash/token/latency、strict canonical 与 raw metrics 分离；fixture 三领域和不同 revision 的隔离统计，禁止 evidence/claim 跨 revision 互引。

**风险**：MEDIUM/HIGH（评测误差或脱敏泄漏）；不得把 semantic alignment 扩大成生产规则。

**门禁/完成定义**：能从 artifact 重放 raw claim 计数和指标，失败时保留真实 failed artifact，不伪造成功。

### M5：隔离真实 shadow 与 library 灰度

**范围**：在隔离新 library/batch 上开启一次真实模型 shadow，再进行单 library canary；不切换 canonical publication 输入，不重跑历史生产任务。

**测试/验收**：provider/model/prompt/config hash、claim/evidence scope、raw metrics、shadow error/p95、Phase 0 Required/Automatic baseline；数据库和页面只验证旧 canonical graph 未回归。

**风险**：HIGH（真实成本、敏感数据、数据库写入）；必须确认授权、隔离 DSN 和 artifact 脱敏。

**门禁/完成定义**：真实 shadow 成功且可追溯；关闭 flag 后旧路径仍可用；无未授权 claim read/write；Phase 2 gate 报告 raw retention 和 baseline 状态。

## 9. 明确排除

- Phase 3：raw predicate 到 canonical relation 的 mapper、语义等价和方向映射。
- Phase 4：Rule Candidate、条件/例外/执行规则及人工确认。
- Phase 5：query router、graph expansion、hierarchical retrieval、context packing、长上下文模型。
- UI、Graph Canvas、公开 Claim API 和页面交互改造。
- 任何资产、法律、医疗或其他行业 allowlist、关系白名单、固定词表。
- 修改 gold、semantic alignment、现有 scorer 以提升 canonical 指标。
- 真实模型调用、数据库 migration、产品灰度和 E2E；这些只属于后续执行里程碑，不属于本规划动作。

## 10. 第一条可执行指令建议

```text
只执行 Phase 2 M0：先按 AGENTS.md 尝试 GitNexus impact；失败则记录错误并用 rg 审计 graph_extraction_parser、GraphExtractionPayload、GraphRelationOccurrence、GraphRelationCandidate、GraphRelationCandidateEvidence、GraphExtractionWorker 的直接调用者和 HIGH 风险流程。只新增 DB-free RawClaimV1 contract、纯校验/规范化 helper 和 fixture 测试；不改生产写路径、不建 migration、不改 prompt/gold/scorer、不调用真实模型、不进入 M1。完成后报告 candidate 边界是否能无损表达 raw predicate、direction、negation、modality、qualifiers、time、evidence 和 revision/job/unit provenance，并停止等待审核。
```
