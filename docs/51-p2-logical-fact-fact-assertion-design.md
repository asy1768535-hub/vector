# P2 LogicalFact / FactAssertion 语义设计

状态：**P2 设计阶段完成；未授权实施**

本文件只冻结现状调查、语义契约、候选数据模型和实施 checkpoint。没有
创建 migration，没有修改 ORM、数据库、生产 service、KnowledgeRelation
主链、Publication、Retrieval 或 Temporal Query，也没有实现自动 Fact Merge。

GitNexus 已尝试运行 `npx gitnexus analyze`，但因 Node.js 依赖
`tree-sitter-kotlin` 无法加载 `node-gyp-build` 而不可用。本设计基于定义/调用方
的手工 fallback 审查；本文件新增不涉及现有生产 symbol 的修改。

## A. 当前 relation / fact 数据流

当前正式 extraction 主链为：

```text
GraphRelationCandidate
  -> validation / aggregation
  -> graph_extraction_materializer
  -> KnowledgeRelation
  -> RelationEvidence
  -> GraphPublicationItem
  -> publication reconcile/read
  -> graph catalog / retrieval / chat
```

`GraphRelationCandidate` 使用 job 范围内的
`source_candidate_id`、`relation_type_key`、`target_candidate_id`、
`proposed_properties` 和候选证据/置信度。其 candidate key 包含 ontology
version、source/target candidate key、relation type key 和 properties。

materializer 将候选端点解析为 `Entity.id`，并以 ontology version、relation
type、source/target Entity 和 properties 计算 `KnowledgeRelation.extraction_key`。
当前 `KnowledgeRelation` 是 ontology-specific projection，字段还包括
`valid_from` / `valid_to`，但这些字段没有独立的长期事实身份语义。

`RelationEvidence` 属于 KnowledgeRelation，支持 `supports`、`contradicts`、
`mentions`、`source`。证据 stale 时，RelationEvidence 会标为 `stale`；当
relation 没有 active evidence 时，KnowledgeRelation 会标为 `stale`。现有路径
不会物理删除 Entity、CanonicalEntity 或 KnowledgeRelation 来表示证据失效。

RawClaim shadow 是旁路：

```text
RawClaimV1
  -> GraphRawClaim + GraphRawClaimOccurrence
  -> GraphClaimDecision (目前仅 pending projection)
```

RawClaim 不进入当前 materializer、publication 或 retrieval 主链。candidate purge
只清理 candidate/occurrence payload，不影响 RawClaim 的 immutable retention；普通
extraction purge 保留 candidate row，因此既有 Decision 对 candidate 的审计引用可以
继续存在。只有真实 physical DELETE candidate row 时，数据库 schema 的 `ON DELETE SET
NULL` 才会置空该 FK；没有 PostgreSQL runtime 时不把它写成已实测行为。

## B. 当前模型的限制

1. `KnowledgeRelation` 的 extraction key 依赖 ontology-specific
   `RelationType.key` 和 Entity projection，不能作为跨 ontology 的稳定 predicate
   identity，也不能代表所有事实语义。
2. relation `properties` 是 opaque JSON。negation、modality、qualifier、time
   等字段即使暂时被携带，也没有 typed owner、fingerprint 规则或独立 assertion
   生命周期。
3. 当前 relation 聚合把多个来源汇成 projection/evidence 集合；它没有把“同一
   事实的不同断言”和“不同事实”分开，因此无法可靠表达时间变化或矛盾。
4. `RelationEvidence` 的 stale 语义只覆盖 projection evidence，不提供
   LogicalFact 是否仍存在或 FactAssertion 是否被 superseded 的语义。
5. RawClaimV1 已保存 `raw_predicate`、direction、negation、modality、qualifiers、
   `valid_time`、`effective_time` 和 typed evidence refs，但当前没有 RawClaim 到
   canonical fact 的 promotion/resolution 链。
6. `GraphClaimDecision` 目前只允许 `mapping_candidate` / `schema_extension_candidate`
   和 `pending`，它是 shadow proposal，不是完整的 resolution audit，也不能记录
   resolved、rejected、applied、superseded 的生产事实。
7. 不应通过字符串相似或 `relation_type_key` 反推 `raw_predicate`；原始 predicate
   若在 provider/parser 边界已丢失，后续层无法可靠恢复。

## C. 推荐的 P2 语义模型

采用三层边界：

```text
CanonicalEntity + stable PredicateIdentity + policy-selected object identity
  -> LogicalFact (长期身份)

RawClaim / GraphRelationCandidate / KnowledgeRelation projection
  -> FactAssertion (一次有来源的断言)

KnowledgeRelation + RelationEvidence
  -> 现有 ontology-specific projection/read/publication
```

核心规则：

- LogicalFact 是可长期引用的“这件事”，不携带某一次抽取的 evidence、model、
  job 或 payload。
- FactAssertion 是“某来源在某时间、以某 polarity/modality 表述这件事”，保存
  provenance 和 evidence linkage；其持久化状态保持最小集合，unsupported/conflicted
  由 support/contradiction policy 派生，stale 或 superseded 则保留生命周期标记。
- 一个 LogicalFact 可以有多个 FactAssertion；同一来源重复抽取也必须按 occurrence
  provenance 区分，不能把 evidence 数量误当成事实数量。
- 未解决的 predicate、端点或 identity-bearing qualifier 不得自动创建或合并
  LogicalFact；它们留在 pending/diagnostic boundary。
- ontology version 是语义和投影范围，不自动等于 LogicalFact identity。跨 ontology
  合并必须有显式 PredicateIdentity mapping 和端点 canonical identity。
- 所有 identity component 使用版本化、规范化、可重算的 fingerprint；不把原文、
  prompt、路径、secret 或未验证 payload 放进长期 identity。

## D. Temporal identity 决策

比较三个模型：

| 模型 | 做法 | 主要问题 |
| --- | --- | --- |
| A：时间进入所有 identity | `subject + predicate + object + valid/effective time` | 同一事实的重复来源和小区间差异造成过度拆分；时间字段被错误地当作实体身份 |
| B：时间永不影响 identity | 所有时间变化都共用一个 LogicalFact | 事件/占用/任期等确实需要分片的 predicate 无法表达 |
| C：predicate-specific policy | 默认时间属于 assertion；少数明确 predicate 使用派生 temporal identity key | 需要治理一份小而明确的 policy，但避免全局猜测 |

P2 采用 **模型 C**，但只允许四个有限 temporal class，不建立通用 DSL：

| class | LogicalFact identity | object 是否进入 identity | valid/effective time | 不同时间值 |
| --- | --- | --- | --- | --- |
| `static_fact` | subject + stable predicate + normalized object | 是 | 仅 assertion 审计字段；不作为 identity | 同一 fact 的新 Assertion |
| `measurement_slot` | subject + stable predicate + identity-bearing target/scope | target/scope 是；measured value 否 | measured value/time 仅属于 Assertion | 同一 slot 的新 Assertion；不同 value 在同 scope/time 下形成 value conflict |
| `state_fact`（默认） | subject + stable predicate + object + identity qualifiers | 是 | 仅 assertion；用于任期/状态重叠判断 | 同 object 为同一 fact；不同 object 为不同 fact |
| `event_fact`（显式 opt-in） | subject + stable predicate + event/object key + derived temporal key | 是，event/object key 必须稳定 | 由 policy 派生 temporal_identity_key | 不同 temporal key 为新 fact；相同 key 为新 Assertion |

`ceo_of` 使用 `state_fact`：2025 CEO 张三与 2026 CEO 李四是 **2 个
LogicalFact + 2 个 FactAssertion**；任期不相交所以不 conflict。相同任期的不同
CEO 仍是 2 个 LogicalFact，但属于同一 functional contradiction group。`valid_time`
和 `effective_time` 原值始终存于 FactAssertion；`event_fact` 只把规范化后的派生 key
放入 identity。缺失时间不等于全时间范围；timezone、开闭区间和精度使用固定的
UTC/ISO-8601 规范。无法规范化时 fail closed 为 pending。

因此，不能把所有时间变化强行合并，也不能把每次时间变化都拆成新事实。

## E. Predicate identity 决策

`RelationType.key` 只在 `(library, ontology_version)` 范围内有意义。P2 v1 引入
一个可治理、可落库的 **`StablePredicateIdentity` contract**：

```text
predicate_namespace       稳定命名空间/URI
predicate_key              稳定规范化 key
predicate_contract_version policy 和规范化版本
resolution_status          resolved | pending | ambiguous | rejected
source_mappings            明确列出 ontology_version + relation_type_id/key
```

`StablePredicateIdentity` 不是 `RelationType` 的别名；ontology mapping 必须显式记录
方向、适用范围和生效版本。未解决或 ambiguous 的 raw predicate 只能生成 pending
resolution input，不能进入自动 Fact Resolution、publication 或 merge。

P2 v1 **正式建立 StablePredicateIdentity**。P2.2 最小模型为：

```text
StablePredicateIdentity
  id: UUID
  library_id: UUID
  namespace: bounded string
  key: bounded string
  contract_version: bounded string
  temporal_class: static_fact | state_fact | measurement_slot | event_fact
  identity_policy_version: bounded string
  resolution_status: resolved | pending | ambiguous | rejected
  created_at / updated_at
```

ontology projection 使用独立、可追加的 mapping（而不是在 RelationType 上覆盖一个
nullable FK）：

```text
StablePredicateMapping
  id: UUID
  library_id: UUID
  stable_predicate_identity_id: UUID
  relation_type_id: UUID
  mapping_status: active | superseded | rejected
  created_at / superseded_at
```

独立 mapping 比 `RelationType.stable_predicate_identity_id` 多一个窄表，但能保留
mapping 变更历史，且不改写已有 RelationType 语义。`library_id` 是自动 resolution
scope，不重复引入 organization_id。1 个已有 RelationType 的历史 scaffold 最多创建
1 个 StablePredicateIdentity，绝不按相同 key 跨 ontology 合并；后续显式 mapping/
reconciliation 才能统一。`owns` 与 `holds_equity` 在没有 mapping 时保持不同
projection/pending；有 mapping 且方向、端点均通过时才可共用 LogicalFact identity。

## E1. Literal object identity 契约

P2 v1 只实现最小 typed canonical value contract，不建立通用 Value Engine：

| 输入 | canonical value |
| --- | --- |
| `"20%"` | `{kind: decimal, value: "0.2", unit: "ratio"}` |
| `0.2`（predicate unit policy = ratio） | 同上；与 `"20%"` 相同 identity |
| `"10亿元"`（currency policy = CNY） | `{kind: money, value: "1000000000", currency: "CNY"}` |
| `1000000000 CNY` | 同上 |
| `"2026-09-02"` | `{kind: date, value: "2026-09-02"}` |
| `true` / `false` | typed boolean |
| string | Unicode NFKC + trim；默认保留大小写，predicate 可显式 casefold |
| integer / decimal | Decimal plain form，去除无意义前导/尾零，不使用浮点 identity |

百分比、金额和日期都必须由 predicate/unit policy 提供上下文。单位、币种、精度或
locale 不明确时不做换算，保留原始 assertion 并进入 pending；不把未知单位强行合并。

## F. Qualifier / negation / modality 契约

Qualifier 采用有限的 predicate-specific 分类，并冻结以下默认示例：

| 类别 | owner | 是否进入 LogicalFact identity |
| --- | --- | --- |
| `identity_bearing` | predicate contract 指定的少数字段 | 是，经过 typed normalization 后进入 identity component |
| `assertion_bearing` | FactAssertion | 否；不同值形成同一事实的不同断言 |
| `evidence_only` | RawClaim/evidence | 否；只用于解释来源，不能参与 merge |

实际默认归类：

| qualifier | 默认层 | policy override |
| --- | --- | --- |
| 投资金额 | `assertion_bearing` | 同一事件 predicate 可声明为 `identity_bearing` |
| 地区 | `assertion_bearing` | region-scoped predicate 可声明为 `identity_bearing` |
| 股份类别 | `identity_bearing`（shareholding predicate） | 非 shareholding predicate 可降为 assertion |
| 职位范围 | `assertion_bearing` | role-slot predicate 可声明为 identity |
| 计划阶段 | `assertion_bearing`，通常映射到 modality | 不得仅凭阶段创建新 fact |
| 来源备注 | `evidence_only` | 不得进入 Fact identity |

identity-bearing 的其他例子是稳定 event/contract/account id；assertion-bearing 的
其他例子是金额、地区、职位范围；evidence-only 的其他例子是 quote、section 和
extractor rationale。

未列入 contract 的 qualifier 不得静默降级为 identity 或忽略；应保持 pending，直到
人工/治理规则明确归类。P2 v1 不建立通用 qualifier DSL，只允许有限字段白名单和
版本化规范化函数。

Negation 作为 FactAssertion 的 `polarity`：`affirmed`、`negated`、`unknown`。
它不是 LogicalFact identity 的一部分，因此同一 LogicalFact 可以同时拥有正、负断言，
由 contradiction policy 标记冲突。`公司A已投资X` 与 `公司A未投资X` 是同一
LogicalFact 的 positive/negative Assertions；时间不相交时不构成 contradiction，
例如 2025 未投资、2026 已投资是状态变化。

Modality 作为 FactAssertion 的 typed 状态，至少支持：
`planned`、`possible`、`expected`、`confirmed`、`completed`、`unknown`。
不能把 `planned`、`possible` 或 `expected` 直接发布成确定的 active fact。未来
Publication Planner 仅在 assertion status 为 active、evidence 有 active support、
predicate 已 resolved、无未解决 contradiction，且 modality policy 允许
`confirmed`/`completed`（或显式允许的其他状态）时发布；否则保留为 pending/tentative
projection。`surface_direction` 必须在 resolution 时显式规范化，并保留 RawClaim
原值用于审计。

## G. Contradiction model

矛盾是 assertion 层关系，不是删除或覆盖行为。

- 同一 LogicalFact 下，`affirmed` 与 `negated` 断言在相交时间范围内构成 polarity
  contradiction。
- 对声明为 functional/cardinality-one 的 predicate，不同 object identity 在相交
  `valid_time` 内构成 value contradiction；非 functional predicate 可以共存。
- 不同 modality 默认不是矛盾：`planned` 与 `completed` 可表示生命周期进展；只有
  predicate contract 声明状态互斥时才标记冲突或 superseded。
- contradiction 计算生成稳定的 contradiction group/key 和解释原因；不会修改
  immutable RawClaim 或已写入的 FactAssertion content。
- evidence `supports` / `contradicts` 仍保留现有 RelationEvidence 语义。FactAssertion
  的 `conflicted` 只表示断言集合的计算结果，不能把单条 `contradicts` evidence
  直接等同于全局事实为假。

## H. LogicalFact v1 候选模型

以下是实施前需要冻结的候选字段，不是 ORM 定义：

```text
LogicalFact
  id: UUID
  library_id: UUID
  subject_canonical_entity_id: UUID
  predicate_identity: {namespace, key, contract_version}
  object_identity: typed canonical entity / normalized literal / governed reference
  identity_policy_version: string
  temporal_identity_key: string | null       # 仅 event_fact
  qualifier_identity: bounded typed JSON     # 仅 identity_bearing subset
  identity_fingerprint: SHA-256
  status: active | conflicted | inactive
  created_at / updated_at
```

v1 identity 的最小公式为：

```text
library_id
+ subject canonical identity
+ resolved predicate identity
+ policy-selected object identity
+ identity-bearing qualifier component
+ optional temporal_identity_key
+ identity_policy_version
```

ontology version、job、model、prompt、raw text、evidence refs、assertion modality 和
measurement_slot 的 asserted value 不进入 LogicalFact identity。measurement_slot 的
target/scope 进入 `policy_selected_object_identity`；object/value 若无法稳定规范化，
resolution 必须 pending。

## I. FactAssertion v1 候选模型

```text
FactAssertion
  id: UUID
  library_id: UUID
  logical_fact_id: UUID
  assertion_fingerprint: SHA-256
  subject/object snapshot: bounded typed audit snapshot
  predicate_identity_snapshot: namespace/key/version
  asserted_object_kind: entity | literal | null
  asserted_object_canonical_entity_id: UUID | null
  asserted_value: bounded typed value | null  # required for measurement_slot
  polarity: affirmed | negated | unknown
  modality: planned | possible | expected | confirmed | completed | unknown
  qualifiers: bounded typed JSON             # assertion_bearing subset
  valid_time: typed interval | null
  effective_time: typed interval | null
  confidence: 0..1 | null
  status: active | stale | superseded | rejected
  source_kind: raw_claim | graph_relation_candidate | knowledge_relation | manual
  raw_claim_id / extraction_occurrence_id: nullable provenance links
  graph_relation_candidate_id: nullable provenance link
  knowledge_relation_id: nullable projection link
  evidence linkage: existing RelationEvidence or future typed bridge
  ontology_version_id / job_id / extractor provenance
  created_at / superseded_at
```

`unsupported` 不单独持久化：由 active evidence count/support policy 派生；冲突状态
由 LogicalFact 计算，不在每条 Assertion 重复存储。内容字段和 provenance 字段必须分开 fingerprint。
FactAssertion 采用 append-only
写入；生命周期变化使用受控 status/marker，不通过重写原始 assertion 内容来“修正”
历史。若 predicate 或 object 尚未 resolved，不创建正式 FactAssertion，保留 pending
resolution input 和原始 RawClaim。

`static_fact`、`state_fact` 和 `event_fact` 的 object identity 已在 LogicalFact 中，
Assertion 中的 asserted object 仅作为 bounded source snapshot，可为空。`measurement_slot`
必须把实际测量值写入 `asserted_value`；不得把 20%/30% 只塞入普通 qualifier。其 target
或 scope（例如公司 B）仍作为 identity-bearing object/scope 进入 LogicalFact。

## J. KnowledgeRelation / RelationEvidence bridge

P2 v1 保留当前 projection 主链和 owner：

```text
KnowledgeRelation -> RelationEvidence -> publication/retrieval
```

P2 v1 的最终选择是 **RelationEvidence 增加 nullable `fact_assertion_id`（方案 C）**，
同时保留原有 KnowledgeRelation owner：

- 已解析的 KnowledgeRelation 最多关联一个 v1 LogicalFact 和对应 FactAssertion；
  unresolved relation 保持 `logical_fact_id = NULL`，不阻塞现有 canonical extraction。
- FactAssertion 可记录 `knowledge_relation_id` 作为 projection provenance；
  `RelationEvidence.fact_assertion_id` 只在 resolution 后回填，原有 `relation_id`
  查询和 publication/retrieval 不变。Evidence stale 可直接触发 Assertion support
  重算，不因 projection 重建丢失事实级归属。
- 一条 KnowledgeRelation 的聚合 evidence 不自动拆成多个 LogicalFact。若未来需要
  一个 projection 映射多个 assertion，新增明确的 `FactProjection` bridge，并在独立
  checkpoint 处理，不在 P2 v1 偷换现有 owner。
- Relation stale 不删除 LogicalFact。没有 active RelationEvidence 时，关联 assertion
  可标为 `stale`；没有 active support 时由 support policy 派生为 unsupported，LogicalFact
  保留为 inactive，以便审计和
  后续新 assertion 恢复。

本桥接不改变 `Entity.id` / `KnowledgeRelation.id` 作为 publication/retrieval anchor，
也不把 CanonicalEntity 直接暴露为现有 publication item。

P2.2 的 FK 方向冻结为：

```text
StablePredicateIdentity 1 <- N StablePredicateMapping N -> 1 RelationType
StablePredicateIdentity 1 <- N LogicalFact
LogicalFact              1 <- N FactAssertion
LogicalFact              1 <- N KnowledgeRelation   (nullable logical_fact_id)
FactAssertion            1 <- N RelationEvidence   (nullable fact_assertion_id)
KnowledgeRelation        1 <- N RelationEvidence   (existing relation_id owner)
```

`FactAssertion.knowledge_relation_id` 是 nullable 的 ontology projection provenance；
一个 Assertion 只关联一个 KnowledgeRelation，只有未来出现一对多 projection 时才引入
额外 bridge table。

## K. RawClaim / ClaimDecision bridge

RawClaimV1 继续是 immutable source assertion content；它不是 LogicalFact，也不是
自动 mapping 结果。桥接顺序应为：

```text
RawClaim 或 GraphRelationCandidate
  -> explicit predicate/type/endpoint resolution
  -> FactResolutionDecision
  -> LogicalFact + FactAssertion (仅 resolved)
```

现有 `GraphClaimDecision` 保持 shadow projection 和 pending-only contract，不扩展为
生产 resolution 状态。推荐后续新增统一的 `FactResolutionDecision`（本轮只定义契约），
原因是 GraphClaimDecision 的 proposal/reason code 专门服务未知 predicate、direction
和 endpoint schema，直接复用会混淆 shadow 诊断与 canonical fact audit。

未来统一 decision 至少要能审计：

```text
pending -> resolved | rejected | superseded
```

每次 decision 必须绑定 library/revision、claim/candidate/projection provenance、
predicate mapping、object resolution、policy version、actor/producer、reason 和
fingerprint。`resolved` 表示决策与 LogicalFact/FactAssertion 写入在同一事务成功；
因此不另设无业务差异的 `applied` 状态。写入失败则事务回滚，decision 保持 pending，
而不是伪造 applied。旧的 pending GraphClaimDecision 不得被追溯性标记为 resolved；
需要新的 append-only FactResolutionDecision 记录。

## L. Lifecycle

1. **Capture**：RawClaim/occurrence immutable retention，candidate purge 不影响它。
2. **Resolve**：只对 predicate、端点、identity-bearing qualifier 和必要时间 policy
   均 resolved 的输入创建 LogicalFact/FactAssertion。
3. **Active**：assertion 有可用 evidence 且符合 modality/publication policy。
4. **Conflict**：新 assertion 与现有 assertion 在 policy 定义的范围冲突；两者都保留，
   通过 contradiction group 和 review 状态表达。
5. **Stale / unsupported**：证据 revision 失效或 purged 时，assertion 标记 stale；
   没有 active support 时由 support policy 派生为 unsupported；不物理删除 LogicalFact。
6. **Superseded**：更高版本/明确修正断言替代旧 assertion 时，旧行保留并标记
   superseded，不能覆盖原始 provenance。
7. **Inactive**：LogicalFact 只有在没有任何 active assertion/support 时才标记 inactive；
   不使用额外 historical 状态，历史性由时间和 assertion 生命周期推导。
8. **Physical deletion**：不是普通生命周期操作。若未来真的删除某个 candidate，
   仅 candidate 外键按 schema 的 `ON DELETE SET NULL` 置空；不删除 RawClaim、
   FactAssertion、LogicalFact 或 CanonicalEntity。没有 PostgreSQL runtime 时不得声称
   该 FK 行为已实测。

## M. Acceptance examples

### A. 两个来源都说公司 A 持股公司 B 20%

在 `holds_equity` contract 将主体 A、对象 B 和 `ownership_percentage=20%` 规范化
后，两条输入解析为同一个 LogicalFact。两个来源产生两个 FactAssertion（不同
occurrence/evidence provenance），都可为 active/supporting；不因来源数量创建两个事实。

### B. 来源 A 20%，来源 B 30%

v1 将 `ownership_percentage` 定为该 predicate 的 `assertion_bearing` qualifier：
两条输入共享 A/`holds_equity`/B 的 LogicalFact，但形成值不同的 FactAssertion。
若 valid/effective time 相交，contradiction policy 将其标为 value conflict；若时间
不相交，可作为历史变化共存。不能静默选较高置信度的一条并删除另一条。

### C. 2025 CEO 张三，2026 CEO 李四

`ceo_of` 是 functional predicate。对象不同会形成两个 LogicalFact；valid_time 不相交，
因此不是 contradiction。两条 assertion 保留各自任期和来源。

### D. 同一时间来源 A 说 CEO 张三，来源 B 说 CEO 李四

仍是两个 LogicalFact，但因 `ceo_of` 的 functional policy 和时间相交，属于同一
contradiction group。两边 assertion 都保留并进入 review/conflicted 计算，不能把其中
一边静默覆盖。

### E. 计划投资 vs 已投资

主体、predicate、对象和 identity-bearing qualifier 相同则共享一个 LogicalFact；
`planned` 与 `completed` 是两个 FactAssertion modality。默认它们不是矛盾；若 contract
确认是同一投资事件，completed 可使 planned superseded，否则两条都保留。planned 不得
以确定事实状态直接 publication。

### F. 未投资 vs 已投资

两条输入共享同一 LogicalFact，分别是 `negated` 和 `affirmed` assertion。时间相交时
构成 polarity contradiction；证据和来源仍分别保留，等待 resolution/review，不通过
最后写入的一条覆盖前一条。

### G. Ontology V1 `owns` vs V2 `holds_equity`

默认不合并。只有 PredicateIdentity registry 明确声明两个 ontology relation type 映射
到同一 stable predicate、方向一致且端点 canonical identity 已解析时，才可指向同一个
LogicalFact，并保留两个 ontology-specific KnowledgeRelation projection。没有该 mapping
时，两者各自 pending/独立，不能用 key 相似度自动 merge。

## N. Blocking issues

本轮冻结的 v1 contract 已足以启动 P2.2 数据模型与 migration 设计，因此：

```text
P2.2 blockers = NONE
```

下列内容是 P2.2 实施时必须按已冻结契约落地的约束，不是开工 blocker：

- StablePredicateIdentity 的 namespace/key/version 快照和显式 mapping 审计。
- 四类 temporal class、最小 typed literal canonicalization、qualifier 白名单及
  functional contradiction policy。
- modality publishability 与 planned -> completed 的 supersession 规则。
- `FactResolutionDecision` 独立于 pending-only `GraphClaimDecision`，并使用本文件的
  四态 lifecycle。
- nullable `RelationEvidence.fact_assertion_id` 和 `KnowledgeRelation.logical_fact_id`
  的 FK/retention 约束；普通 candidate purge 不改变这些审计语义。

以下是已知的运行环境限制，不阻塞 P2.2 设计开工：

- 当前没有 `VECTOR_KB_PG_TEST_DSN`。composite FK、partial unique、`ON DELETE SET NULL`、
  advisory lock、真实并发 CREATE NEW 和 PostgreSQL rollback 只能在 runtime 环境恢复后
  验证；离线/ORM/static contract 不能替代 runtime PASS。

## O. P2 checkpoint 规划

| Checkpoint | 内容 | 当前授权 |
| --- | --- | --- |
| P2-D1 | 冻结 StablePredicateIdentity、四类 temporal policy、literal/qualifier/modality/polarity 和 contradiction contract；补齐 A-G fixture 期望 | **完成** |
| P2-D2 | 冻结 LogicalFact、FactAssertion、FactResolutionDecision 和 KnowledgeRelation/RelationEvidence bridge 的字段/FK/retention 方案 | **完成** |
| P2-D3 | DB-free canonicalization/fingerprint/resolution fixtures；验证 unresolved fail-closed、重复来源、时间和冲突语义 | 待 D2 通过 |
| P2-D4 | ORM + migration + offline SQL/static contract；不得 backfill 或改写当前 projection | 需要单独实施授权 |
| P2-D5 | additive resolver 与 candidate/RawClaim 输入适配；失败只影响 semantic transaction，不影响 canonical extraction/publication | 需要单独实施授权 |
| P2-D6 | KnowledgeRelation/RelationEvidence bridge 和 decision audit；保持现有 publication/retrieval anchor | 需要单独实施授权 |
| P2-D7 | PostgreSQL runtime acceptance、并发、rollback、FK/unique enforcement 和受控 canary | 需要 `VECTOR_KB_PG_TEST_DSN` 与单独授权 |

下一阶段建议为：

```text
P2.2 AUTHORIZABLE
```

P2.2 只允许 LogicalFact、FactAssertion、FactResolutionDecision、KnowledgeRelation
bridge、必要 Evidence bridge、migration、保守历史 backfill scaffold 及其 model/migration
tests。GraphRelationCandidate 正式主链接入、RawClaim promotion、Publication/Retrieval
改造、Temporal Query、Conflict Resolution engine 和 P3 均留在后续 checkpoint。
