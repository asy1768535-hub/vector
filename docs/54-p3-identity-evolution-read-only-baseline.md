# P3.0 Identity Evolution Read-only Baseline

状态：**P3.0 只读分析已授权；P3.1 及后续实施未授权**

本文件是 P3.0 的唯一阶段真源。它冻结 P3.0 的问题边界、必须交付的
设计基线和完成条件；P3.0 的实际分析结果必须回写到本文件。与此前聊天或
临时规划冲突时，以本文件为准，直到后续经明确授权修改。

本文件的创建是对已确认规划的文档化，不是 P3.1 实施授权。

## 1. 当前基线

正式 checkpoint：`c887eaa8f61fa789741139d8bd227401397fff08`

当前状态：

```text
P0 = COMPLETED
P1 = COMPLETED_WITH_KNOWN_LIMITATIONS
P2 = COMPLETED_WITH_KNOWN_LIMITATIONS
P2 Acceptance = PASSED
Alembic head = 0070
P3 = PLANNING
P3.0 = AUTHORIZED (read-only)
P3.1+ = NOT AUTHORIZED
```

P0-P2 已建立：

```text
OntologyVersion
-> Entity projection
-> CanonicalEntity
-> StablePredicateIdentity
-> Fact Resolution
-> LogicalFact
-> FactAssertion
-> KnowledgeRelation
-> RelationEvidence
-> Lifecycle / Conflict
```

P3 的目标是让已确认需要修正的 CanonicalEntity、StablePredicateIdentity 和
LogicalFact 安全演进，同时保留历史解释能力与来源证据。P3 不重新设计 P0-P2
的基础概念。

## 2. P3.0 授权边界

P3.0 只允许：

- 读取 ORM、migration、service、materializer、API、worker、test 和现有设计文档；
- 形成并验证本文件规定的七项设计基线；
- 将只读分析结果、证据和未验证项回写到本文件。

P3.0 禁止：

- 新增或执行 migration；
- 修改 production code、ORM、数据库 schema 或运行时行为；
- 实施 P3.1/P3.2/P3.3/P3.4；
- 改写历史 FK、fingerprint、Fact、Decision、Publication 或 Retrieval；
- 进入 RawClaim promotion、Publication/Retrieval 语义变更或 P3 以外阶段。

P3.0 结束不自动授权 P3.1。P3.1 必须先完成独立 design freeze。

## 3. 历史保护原则

P3.0 及后续 P3 设计必须遵守：

```text
历史 identity 不删除。
历史 projection 不自动迁移。
历史 publication 不自动迁移。
历史 Fact / Decision / Evidence 不静默改写。
不确定性只能产生 proposal 或 pending。
Identity correction 不等于 Fact correction。
```

因此，CanonicalEntity 或 StablePredicateIdentity 的修正不能通过批量更新所有
历史 FK 来表达，也不能通过重算并覆盖历史 fingerprint 来完成 reconciliation。

## 4. P3.0 Required Outputs

P3.0 必须完成以下七份设计基线。每份结果必须区分已由当前仓库代码、migration
或测试验证的事实，和仍待后续设计冻结或 PostgreSQL runtime 验证的项目。

### Output A - Identity Impact Matrix

完整盘点 CanonicalEntity 和 StablePredicateIdentity 的直接、间接引用及所有实际
代码 consumer。不得只检查数据库 FK。

至少覆盖：

```text
Entity.canonical_entity_id
EntityResolutionDecision.canonical_entity_id

LogicalFact.subject_canonical_entity_id
LogicalFact.object_canonical_entity_id
FactAssertion.asserted_object_canonical_entity_id

KnowledgeRelation.logical_fact_id
RelationEvidence.fact_assertion_id
FactResolutionDecision.logical_fact_id
FactResolutionDecision.fact_assertion_id

StablePredicateMapping.stable_predicate_identity_id
LogicalFact.stable_predicate_identity_id
```

对每个对象记录：创建者、读取者、当前历史语义、P3 evolution 影响、可否直接更新
以及必须保留的审计路径。

### Output B - Fingerprint Impact Matrix

明确下列 evolution 对 fingerprint 的影响：

```text
Canonical merge
Canonical split
StablePredicate merge
StablePredicate split
identity_policy_version change
```

至少覆盖：

```text
LogicalFact.identity_fingerprint
FactAssertion.assertion_fingerprint
FactResolutionDecision.subject_fingerprint
FactResolutionDecision.decision_fingerprint
EntityResolutionDecision.subject_fingerprint
EntityResolutionDecision.decision_fingerprint
```

每项必须区分：

```text
historical fingerprint
current derived identity
```

历史 fingerprint 是 row 创建时的不可变审计事实；current derived identity 是依据
Evolution lineage 得出的当前解释。reconciliation 不得重写前者。

### Output C - Current / Historical Bridge and Read Contract

P3 以后同时存在两类语义：

```text
Historical identity = row 创建时正式绑定的 identity；历史 FK 不修改。
Current identity = 根据最新 Evolution lineage 得出的当前解释。
```

P3.0 必须冻结下列概念解析的语义，但不在 P3.0 编码实现：

```text
resolve_current_canonical_identity(id, context?)
resolve_current_stable_predicate_identity(id, context?)
resolve_current_logical_fact(id, context?)
```

这些函数不得假设总能返回单个 successor。它们必须返回一个显式的多结果状态：

```text
resolved        = 唯一 current successor
forked          = 存在多个可能 successor，缺少唯一分配所需上下文
pending         = evolution 或 reconciliation 尚未完成/未获确认
historical_only = 没有 current successor，保留历史查询能力
```

Split 后，调用方若没有 Entity projection、Assertion source group 或其他已经冻结的
确定性上下文，必须得到 `forked` 或 `pending`，不得任选一个 successor。只有在上下文
能够唯一分配时才可返回 `resolved`；其余情况 fail closed。

P3.0 必须明确：

- 新的 Entity Resolution、Fact Resolution 和 RawClaim Resolution 只能写入唯一
  `resolved` 的 current successor identity；
- Historical read 必须继续查询 original identity、Fact、Decision 和 Evidence；
- Current read 必须能返回 current interpreted identity 和 current reconciled Fact；
- 旧 GraphPublication 保持其 ontology-specific historical snapshot；
- P3.0 只盘点 Retrieval 当前读取 historical projection 还是 current projection，
  P3.1/P3.2 不擅自改变 Retrieval。

### Output D - Current-State Consumer Matrix

专项盘点所有 current-state consumer，至少包括：

```text
P2.4 Fact lifecycle
LogicalFact active/conflicted/inactive calculation
FactAssertion active/stale/superseded/rejected
RelationEvidence active/stale/deleted
functional sibling conflict lookup
measurement conflict lookup
polarity conflict lookup
Fact Resolution lookup
Entity Resolution candidate lookup
Publication materialization
Retrieval
Graph query / UI
```

每个 consumer 必须标记：

```text
reads_historical_identity
reads_current_identity
needs_evolution_aware_behavior
must_remain_unchanged_until_later_phase
```

特别必须冻结：Fact reconciliation 后 P2.4 lifecycle/conflict 是对 old LogicalFact，
还是对 current derived Fact projection 运行。此 contract 不得延后到 P3.3 临时决定。

### Output E - Reconciliation Bridge Contract

P3.3 必须采用 append-only reconciliation lineage。以下历史桥接禁止原地更新：

```text
FactAssertion.logical_fact_id
KnowledgeRelation.logical_fact_id
RelationEvidence.fact_assertion_id
```

概念模型为：

```text
Old Fact(s)
    -> Reconciliation Decision / Lineage
    -> Current Fact(s)
```

P3 v1 的允许范围：

```text
1 -> 1
N -> 1
1 -> N deterministic
```

`N -> M` 默认为 `pending`，除非后续另行冻结确定性 contract。

`1 -> N` 只有在 Assertion 能按 source group 或其他已冻结的确定性规则唯一 partition
时允许。任一 Assertion 无法唯一 partition 时，结果必须为 `pending`，旧 Fact 保持
不变。历史 Assertion 始终保留其原 Fact bridge；current interpretation 只能通过
reconciliation lineage 表达。

### Output F - Lock Ordering Contract

所有 P3 multi-identity operation 必须先构造完整 lock set，再以固定 total order 获取
transaction advisory locks。禁止按 source/target 的偶然输入顺序加锁。

统一排序键为：

```text
(library_id, lock_scope_type, lock_scope_key)
```

`lock_scope_type` 必须是固定 total order，至少包括：

```text
canonical_entity
stable_predicate
logical_fact
entity_projection
entity_resolution_subject
```

identity scope 的 `lock_scope_key` 为 identity UUID；`entity_projection` 为 Entity UUID；
`entity_resolution_subject` 为固定版本的 resolution subject fingerprint。P3.1 的
Entity projection reassignment 与并发 Entity Resolution 必须参与同一 contract，避免
merge/split 改归属时新抽取写出相反的 current EntityResolutionDecision。

对于重叠操作，例如 `merge A+B` 与 `merge B+C`，后进入者获得全部锁后必须重新读取
current lineage 并验证 precondition。状态已变化时必须 fail closed，返回的持久化
词汇由 P3.1 design freeze 决定，例如：

```text
PENDING
RETRYABLE_CONFLICT
STALE_OPERATION
```

不得在旧 snapshot 上继续执行。

### Output G - Canonical Split Lifecycle Contract

现有 CanonicalEntity operational status：

```text
active
pending_review
disabled
```

`disabled` 不得被重新解释为 `merged`、`split` 或 `superseded`。P3.0 必须提出一种
正式 lifecycle 方案，例如保留 CanonicalEntity.status 作为 operational status，另由
Evolution lineage 判断 identity 是否 current；或者提出独立 identity lifecycle status。

Split 后旧 CanonicalEntity 必须能够明确表示：

```text
historical identity
has successor identities
not eligible for new resolution
```

旧 identity 不删除，也不假装 disabled 等于 split。最终 schema 和 persisted vocabulary
只能在 P3.1 design freeze 决定。

## 5. P3.0 Completion and Stop Condition

P3.0 完成必须在本文件中交付 Output A-G 的实际矩阵、代码/迁移/测试证据、明确的
current-versus-historical contract，以及所有未决项。P3.0 的结果必须足以让 P3.1
design freeze 决定 Canonical merge/split/successor、Entity projection reassignment 和
Evolution Decision schema，而不依赖临时猜测。

P3.0 的停止条件是七项输出完成并经单独 checkpoint 记录。此 checkpoint 仅允许进入
P3.1 design freeze；不自动授权 P3.1 implementation、migration、P3.2/P3.3/P3.4、
RawClaim promotion、Publication 或 Retrieval 语义变更。

## 6. P3.0 Analysis Write-back

当前状态：**NOT STARTED**

P3.0 实际执行时，在本节追加：

- Output A-G 的矩阵与结论；
- 每项结论的 repository evidence；
- PostgreSQL runtime 与其他环境限制；
- 未决、pending 和 fail-closed 项；
- 是否具备进入 P3.1 design freeze 的条件。
