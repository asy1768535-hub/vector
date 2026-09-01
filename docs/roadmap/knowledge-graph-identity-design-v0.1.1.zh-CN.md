# 知识图谱增量实体统一与事实身份设计规划 v0.1.1

> 状态：设计基线（plan-only）
>
> 当前授权：P0/P1 可进入详细数据模型设计；P2 只进入契约设计；P3 不进入当前实施范围。
>
> 本文不授权代码实现、数据库迁移或历史数据变更。

## 1. 规划目标

本规划不重写现有 GraphRAG / Knowledge Graph 抽取、证据、发布和治理系统，只补充正式主链路缺少的两类长期身份：

~~~text
CanonicalEntity
LogicalFact
~~~

现有 Entity 和 KnowledgeRelation 继续作为具体 Ontology Version 下的 projection。

目标边界：

~~~text
Ontology Version 负责知识如何表达
CanonicalEntity 负责现实实体是谁
LogicalFact 负责长期事实身份
FactAssertion 负责某个来源如何陈述事实
RawClaim 负责文档中的原始陈述
Evidence 负责来源和证据生命周期
Entity / KnowledgeRelation 负责当前 Ontology projection
GraphPublication 负责对外可见快照
~~~

## 2. 当前代码事实与已有能力

当前正式抽取主链路：

~~~text
Ontology Snapshot
→ GraphExtractionJob
→ Entity / Relation Candidate
→ Entity / KnowledgeRelation
→ EntityMention / RelationEvidence
→ GraphPublication / GraphPublicationItem
~~~

当前已存在但职责不同的能力：

- matched_entity_id、normalized name、alias matching：ontology-specific Entity match。
- GraphRawClaim、GraphRawClaimOccurrence、GraphClaimDecision：Raw Claim shadow 链路。
- GraphEntityMergeCandidate、GovernanceAction、GovernanceEffect：候选和 projection-level 治理。
- revision stale、retention、coordinated purge、Publication reconcile：证据和发布生命周期。

必须保留的限制判断：

~~~text
Ontology-specific Entity Match ≠ Canonical Entity Resolution
Entity Merge ≠ 完整 identity merge service
Merge rollback ≠ 完整 merge undo
RawClaim ≠ 已进入正式 Relation 主链路
RelationEvidence ≠ Fact-level Evidence
~~~

当前代码锚点：

- 默认 extraction ontology 选择：app/services/graph_extraction_jobs.py:362
- Publication ontology 选择：app/services/graph_publication_planner.py:270
- Raw Claim shadow 持久化：app/services/graph_claim_shadow_worker.py:437
- Candidate 到正式 Entity/Relation 的 materialization：app/services/graph_extraction_materializer.py:370
- Governance Merge staging：app/services/graph_governance_actions.py:1057

## 3. 不重复建设

本规划不重新建设：

1. Schema Discovery；
2. Ontology draft / confirm / activate；
3. Ontology Snapshot；
4. GraphExtractionJob；
5. Graph Entity / Relation Candidate；
6. EntityMention；
7. RelationEvidence；
8. RawClaim / RawClaimOccurrence；
9. ClaimDecision 的 fingerprint / pending persistence；
10. Publication Snapshot；
11. Publication supersede / degrade / rollback 基础框架；
12. revision stale / retention；
13. coordinated purge；
14. GovernanceAction / GovernanceEffect。

复用不等于职责不变。上述对象在新身份层接入后，仍需补充明确的 owner、引用和生命周期契约。

## 4. 强制架构原则

### 4.1 Identity、Projection、Publication 分离

~~~text
Identity 可以跨 Ontology 连续。
Projection 不跨 Ontology 自动迁移。
Publication 不跨 Ontology 自动迁移。
~~~

实体：

~~~text
Entity / V1 ─┐
             ├── CanonicalEntity
Entity / V2 ─┘
~~~

事实：

~~~text
KnowledgeRelation / V1 ─┐
                         ├── LogicalFact
KnowledgeRelation / V2 ─┘
~~~

### 4.2 不改变现有 Publication scope

GraphPublication 和 GraphPublicationItem 继续绑定一个具体 Ontology Version。

~~~text
Publication Ontology
= Entity Projection Ontology
= Relation Projection Ontology
~~~

Retrieval / Chat 继续跟随实际读取的 Publication，不直接改为读取 Library 的 Current Ontology。

### 4.3 保守优先

~~~text
宁可暂时重复，不可批量误合并。
宁可 pending_review，不可由弱信号生成确定身份或事实。
~~~

## 5. P0：Current Ontology 写入语义与 lineage

### 5.1 Active Ontology 与 Current Ontology

~~~text
Active Ontology
= 仍处于 active 状态、可能被历史流程或 Publication 引用的版本

Current Ontology
= Library 新写入侧默认使用的唯一版本
~~~

因此：

~~~text
Active ≠ Current
~~~

同一 Library 可以暂时存在多个 active Ontology Version，但同一时刻最多只能有一个 Current Ontology。

Current Ontology 只服务于：

- 新 GraphExtractionJob；
- Schema Evolution；
- AI Discovery 继承；
- 默认的新 Publication planning；
- 其他明确属于新写入侧的流程。

Current Ontology 不直接决定：

- 历史 Publication；
- Graph Retrieval；
- Chat Graph Context；
- 已冻结 Ontology Snapshot 的 Job。

### 5.2 默认写入必须 fail closed

新写入流程不得使用 limit(1)、latest active、first active 或任意 active ontology 猜测。

~~~text
Current 存在且有效 → 使用 Current
Current 不存在 → blocked
Current 指向失效版本 → blocked
Current 状态冲突 → blocked
~~~

错误应具有机器可识别的状态，例如：

~~~text
current_ontology_missing
current_ontology_invalid
current_ontology_transition_in_progress
~~~

### 5.3 Current pointer

推荐以等价于以下字段的显式 pointer 表达 Current：

~~~text
Library.current_ontology_version_id
~~~

它不是普通声明字段，必须和 Ontology lifecycle 保持事务一致：

- 首次 activation 与 pointer 建立原子完成；
- V1 → V2 的 pointer 切换与 V2 activation 属于同一生命周期动作；
- disable Current 前必须先切换到新 Current，或明确进入 no-current 状态；
- delete Current 前必须先解除 Current 身份并检查 Job snapshot、Publication、子版本和 lifecycle references；
- 不得自动让其他 active ontology 接替 Current。

### 5.4 Ontology lineage

~~~text
首次 Discovery：V1.parent_version_id = null
基于 Current V1 演进：V2.parent_version_id = V1
~~~

parent_version_id 表示创建新版本时所基于的 Current，不因之后的 Current 切换而修改。

### 5.5 Publication 升级

~~~text
V2 activation
→ Current = V2
→ V1 Publication 仍绑定 V1
→ reconcile 发现 V1 inactive 后降级
→ V2 Publication 独立基于 V2 projection 生成
~~~

不自动迁移：

- V1 Publication 到 V2；
- V1 GraphPublicationItem 到 V2；
- V1 Entity/Relation 到 V2 projection。

### 5.6 已排队 Job

一旦 Job 冻结 ontology_snapshot，后续 Current 切换不得改变该 Job 的解释语义。

~~~text
Job 创建时使用 V1
→ Current 后来变为 V2
→ 已有 Job 仍按 V1 snapshot 完成
~~~

除非 Job 被明确取消并重新创建。

### 5.7 P0 设计验收

- 每个 Library 有唯一明确的 Current pointer，或明确处于 no-current；
- extraction、trigger、Schema Discovery、Schema Evolution、默认 Publication planning 使用同一 Current 语义；
- Retrieval / Chat 继续使用 Publication ontology；
- V1 Job 不受 V2 activation 影响；
- V1 Publication 不自动迁移；
- AI Discovery V2 的 parent 固定为创建时的 Current V1；
- Current 缺失或失效时默认写入 fail closed；
- Current 切换具备幂等性。

## 6. P1：CanonicalEntity 与 Entity projection

### 6.1 最小身份模型

候选结构：

~~~text
CanonicalEntity
- id
- library_id
- canonical_name
- normalized_name
- status
- created_at
- updated_at
~~~

身份作用域以 library_id 为数据库隔离边界；Library 已归属于 Organization。禁止跨 Library、跨 tenant 自动 resolution。

### 6.2 Entity projection

现有 Entity 保留 ontology-specific 职责，并增加可选映射：

~~~text
Entity.canonical_entity_id
~~~

目标关系：

~~~text
CanonicalEntity C1
├── Entity E1 / Ontology V1
└── Entity E2 / Ontology V2
~~~

E1 和 E2 仍然是不同 projection。Publication 继续发布 Entity projection，不直接发布 CanonicalEntity。

### 6.3 两阶段身份判断

~~~text
GraphEntityCandidate
→ ontology-specific Entity Match
→ Canonical Entity Resolution
→ Entity Projection Materialization / Reuse
→ EntityMention
~~~

matched_entity_id 仅表示当前 Library + 当前 Ontology 下的 Existing Entity match。它可以作为 Canonical Resolution 的高质量证据，但不能绕过 EntityResolutionDecision。

### 6.4 P1 Resolution 策略

仅使用确定性和可审计规则：

1. 强业务标识：统一社会信用代码、股票代码、设备 ID、项目编号、产品型号、稳定 external ID；
2. 已有 Canonical mapping；
3. Existing Entity / Alias 作为候选证据；
4. normalized name 作为候选信号；
5. 无法消歧时进入 pending_review。

P1 不启用 fuzzy、embedding 或 LLM resolver。

EntityType.key 只能作为类型兼容候选信号，不能直接冻结为跨 Ontology 的稳定 semantic type identity。类型兼容规则应支持：

~~~text
same
compatible
different
unknown
~~~

### 6.5 Resolution Decision

需要持久化 resolution history，例如：

~~~text
EntityResolutionDecision
- id
- library_id
- candidate_id / entity_id
- canonical_entity_id
- decision
- method
- confidence
- reason
- resolver_version
- created_at
~~~

语义上至少覆盖：

~~~text
auto_resolved
new_entity
pending_review
rejected
superseded
~~~

### 6.6 历史 Entity backfill

默认采用保守 1:1：

~~~text
Existing Entity E1 → CanonicalEntity C1
Existing Entity E2 → CanonicalEntity C2
~~~

不得因为 normalized name 相同就自动合并。只有稳定 external ID、确定性业务 identifier 或已有明确治理 mapping 才能复用同一个 CanonicalEntity。

### 6.7 Alias 边界

P1 继续使用：

~~~text
EntityAlias → Entity → CanonicalEntity
~~~

但 EntityAlias 仍然是 Entity-owned 的 resolution candidate source，不承诺 Canonical-level alias owner、Alias 全局唯一、Alias 跨 projection 自动同步或 Alias ownership 自动迁移。

### 6.8 P1 不做物理 Entity Merge

P1 优先通过 mapping 解决身份统一：

~~~text
Entity A.canonical_entity_id = C1
Entity B.canonical_entity_id = C1
~~~

P1 不实现：

- physical entity collapse；
- EntityMention 全量重挂；
- duplicate Relation merge；
- RelationEvidence 搬迁；
- split；
- full undo。

现有 Governance Merge 继续作为 projection-level governance，但不能承担跨 Ontology identity。

### 6.9 无关系 Entity

必须分别验收四层：

~~~text
Candidate valid
→ Entity materialize / reuse
→ EntityMention active
→ Entity-only PublicationItem 可生成
~~~

Relation count = 0 合法。成功物化、成功发布和参与关系扩展是三个不同结果。

### 6.10 CanonicalEntity 生命周期

~~~text
Document delete / revision supersede
→ Mention stale
→ Entity projection publishability 重新判断
→ CanonicalEntity 保留
~~~

CanonicalEntity 不因单个 Document、Revision、Ontology、Publication 或 Projection 失效而物理删除。可以进入 inactive、orphaned 等逻辑状态；具体枚举在详细数据模型设计中冻结。

### 6.11 P1 验收范围

- 同一 Entity 在重复抽取中 mapping 幂等；
- 不同文件的同一实体可以复用 CanonicalEntity；
- V1/V2 Entity projection ID 不同但 CanonicalEntity 相同；
- 同名不同实体不自动合并；
- 跨 Library / tenant 不自动归一；
- 历史 backfill 默认 1:1；
- 无 Relation Entity 可以 materialize、创建 Mention、生成 Entity-only PublicationItem；
- 删除一个文档或替换一个 revision 不删除 CanonicalEntity；
- pending_review 不会被误当成自动 resolution；
- Retrieval / Chat 的 Publication ontology scope 不因 CanonicalEntity 引入而改变。

## 7. P2：LogicalFact / FactAssertion / Claim Resolution 契约

P2 当前只进入设计，不冻结最终表结构，也不进行 LogicalFact migration。

### 7.1 双输入源

正式 Relation extraction path：

~~~text
GraphRelationCandidate
→ validation
→ KnowledgeRelation
→ RelationEvidence
~~~

Raw Claim shadow path：

~~~text
GraphRawClaim
→ GraphRawClaimOccurrence
→ GraphClaimDecision(pending)
~~~

当前两条路径没有正式汇合，不能假设 RawClaim 已经可以生成 KnowledgeRelation。

### 7.2 统一 Fact Resolution boundary

目标是：

~~~text
GraphRelationCandidate ─┐
                       ├→ Fact Resolution Boundary
RawClaim / ClaimDecision┘          ↓
                              FactAssertion
                                   ↓
                              LogicalFact
                                   ↓
                    KnowledgeRelation projection
~~~

不能直接采用：

~~~text
RawClaim → KnowledgeRelation
KnowledgeRelation row = FactAssertion
~~~

### 7.3 三层职责

~~~text
LogicalFact
= 长期事实身份

FactAssertion
= 某个来源在特定时间、极性、模态、限定条件下对事实的具体陈述

RawClaim / Evidence
= 原始陈述与来源证据
~~~

### 7.4 Fact Key 尚未冻结

候选身份输入包括：

~~~text
library_id
subject_canonical_entity_id
predicate_identity
object identity / value identity
~~~

以下字段不能在 P2 设计完成前直接写入 Fact Key：

~~~text
valid_from
valid_to
all qualifiers
status
negation
modality
~~~

必须先分类为：

~~~text
identity-bearing
assertion-state
evidence metadata
~~~

### 7.5 RelationEvidence 边界

当前 RelationEvidence 属于 KnowledgeRelation projection，而不是 LogicalFact。

因此 P2 必须明确：

~~~text
LogicalFact
→ FactAssertion
→ RawClaim / Evidence

LogicalFact
→ KnowledgeRelation projection
→ RelationEvidence
~~~

不能提前承诺现有 RelationEvidence 足以表达 Fact-level evidence。是否增加 Fact assertion/evidence bridge，留待 P2 详细设计决定。

### 7.6 ClaimDecision 生命周期

当前 ClaimDecision 只有 pending 语义。P2 必须定义能够表达：

~~~text
pending
resolved
rejected
superseded
applied
~~~

并回答：

- Claim 是否已经处理；
- 处理成了哪个 Assertion；
- Assertion 指向哪个 LogicalFact；
- Decision 是否已过期；
- negation、modality、valid_time、effective_time、contradiction 如何消费。

### 7.7 Fact 与 revision stale

文档或 revision 删除时：

~~~text
Evidence stale
→ FactAssertion support 重新计算
→ LogicalFact 保留长期 identity
~~~

当所有 Assertion 都失效时，LogicalFact 不立即物理删除，可进入 inactive、historical 或其他 P2 决定的状态。

supporting assertion 与 contradicting assertion 必须分开，不得把 contradiction 当作普通 support。

### 7.8 P2 阻塞决策：时间语义

以下问题关闭前：

~~~text
不得冻结 LogicalFact Fact Key
不得正式建表迁移
不得实现自动 Fact dedupe
~~~

示例：

~~~text
2025：项目 A 负责人 = 张三
2026：项目 A 负责人 = 李四
~~~

需要决定这是：

- 一个 LogicalFact + 多个 temporal Assertion；
- 两个 LogicalFact；
- 按 predicate 配置不同的 temporal identity policy。

该决定会影响 Fact Key、冲突检测、Projection、当前值查询、历史查询、Evidence stale 和 Publication policy。

## 8. P3：高级 Resolver、Schema Evolution、完整治理

P3 不进入当前实施范围，未来包括：

- fuzzy matching；
- embedding candidate search；
- LLM disambiguation；
- Schema extension 的完整流程；
- RawClaim 正式 projection；
- EntityMention 重挂；
- Duplicate Relation merge；
- RelationEvidence 处理；
- merge history；
- split；
- full undo。

Schema Evolution 优先演进已有：

~~~text
ClaimDecision
SchemaLifecycleAction
OntologyVersion lineage
~~~

不另建平行的 SchemaChangeCandidate 体系，除非后续证据证明现有 owner 无法承载。

## 9. 生命周期总原则

### Ontology 生命周期

~~~text
draft → active/current → inactive/history
~~~

### Projection 生命周期

~~~text
Entity / KnowledgeRelation / PublicationItem
~~~

跟随 Ontology、Evidence 和 Publication 变化，但不等于长期 identity 生命周期。

### Evidence 生命周期

~~~text
Document / Revision
→ EntityMention / RelationEvidence stale
~~~

继续使用现有 stale、retention、coordinated purge 和 reconcile 能力。

### Identity 生命周期

~~~text
CanonicalEntity / LogicalFact
~~~

不因单个 Document、Revision、Ontology、Publication 或 Projection 消失而立即物理删除。

## 10. 明确禁止的实现方向

当前设计和后续实现禁止：

1. 删除 Entity 的 ontology_version_id，直接把 Entity 改造成全局实体；
2. 把 Entity Linking API 原样作为跨 Ontology resolver；
3. 把 RawClaim 直接当 LogicalFact；
4. 把 KnowledgeRelation.extraction_key 当 Fact ID；
5. 把 EntityType.key 当稳定类型身份；
6. 把 normalized name 相同当作充分合并条件；
7. 把 RelationEvidence 直接当 Fact-level Evidence；
8. 把 Current Ontology 直接当 Retrieval Ontology；
9. 把 CanonicalEntity 直接加入 GraphPublicationItem；
10. 用物理 Entity Merge 代替 Canonical mapping；
11. P0/P1/P2/P3 一次性实施；
12. 在 P2 时间语义、Assertion 和 Evidence ownership 未冻结前创建 LogicalFact migration。

## 11. 授权顺序与阶段门槛

~~~text
P0：Current Ontology 写入语义与 lineage
↓
P0 设计验收
↓
P1：CanonicalEntity 与 Entity projection mapping
↓
P1 设计验收
↓
重新审核 P2
~~~

当前授权：

~~~text
P0 + P1：进入详细数据模型设计
P2：只进入契约设计
P3：暂不进入
~~~

详细数据模型设计完成前，不开始代码实现；P2 blocking decision 关闭前，不创建正式 LogicalFact migration。

## 12. 当前数据流

~~~text
Document / Revision
        │
        ▼
EvidenceUnit
        │
        ▼
GraphExtractionJob
        │
        ├── frozen Ontology Snapshot
        └── Current Ontology 只决定新写入默认版本
        │
        ▼
Entity / Relation Candidate
        │
        ├────────────────────────────┐
        │                            │
        ▼                            ▼
Ontology-specific Entity Match    RawClaim
        │                            │
        ▼                            ▼
Canonical Entity Resolution       ClaimDecision
        │                            │
        ▼                            │
CanonicalEntity                     │
        │                            │
        ▼                            │
Ontology-specific Entity           │
        │                            │
        ▼                            │
EntityMention                        │
                                     │
Relation Candidate ────────────────┐ │
        │                          │ │
        ▼                          ▼ ▼
KnowledgeRelation              P2 Fact Resolution Boundary
        │                          │
        ▼                          ▼
RelationEvidence             FactAssertion → LogicalFact
        │                          │
        └──────────────┬───────────┘
                       ▼
          Ontology-specific Relation Projection
                       │
                       ▼
             GraphPublication / Items
                       │
                       ▼
                 Retrieval / Chat
~~~

其中：

~~~text
Current Ontology 控制新写入默认 Schema。
GraphPublication.ontology_version_id 控制实际读取的 Projection Schema。
CanonicalEntity / LogicalFact 不直接替代现有 Publication Item。
~~~

## 13. 设计结论

当前项目不需要重做 GraphRAG。现有系统继续负责：

~~~text
Schema
Extraction
Evidence
Revision
Publication
Governance
Rollback 基础框架
~~~

本规划补充：

~~~text
Canonical Entity Identity
Logical Fact / Assertion Contract
~~~

本 v0.1.1 只冻结 P0/P1 的设计方向和 P2 的问题边界，不冻结 P2 最终表结构，不授权任何代码、数据库或历史数据实施。
