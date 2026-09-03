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
P3.0 = COMPLETED_WITH_KNOWN_LIMITATIONS (static read-only baseline)
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

## 6. P3.0 Analysis Method and Evidence Boundary

当前状态：**COMPLETED_WITH_KNOWN_LIMITATIONS**

分析基线是本文件记录的正式 checkpoint `c887eaa8f61fa789741139d8bd227401397fff08`。
P2 核心 model、Fact Resolution、Fact Lifecycle 与 migration 文件在工作区未被修改；
个别 API/worker 有用户在途改动，涉及这些边界时本分析以正式 checkpoint 的代码语义为准。

GitNexus MCP 在当前会话不可用；此前执行 `npx gitnexus analyze` 因
`tree-sitter-kotlin` 无法加载 `node-gyp-build` 失败。因此本次使用手工 fallback：
对 ORM、Alembic、直接服务调用链和已有 unit test 逐项静态检查。没有修改 production
symbol，也没有运行 migration、数据库写入或 runtime integration。

`VECTOR_KB_PG_TEST_DSN` 当前未配置。因此下列结论是 migration/schema/service
contract 的静态结论，不是 PostgreSQL runtime 实测：composite FK、partial unique
index、`ON DELETE` 行为、transaction advisory lock、真实并发与 rollback。

## 7. Output A - Identity Impact Matrix (Actual)

### A.1 Direct references and semantic owners

| Object / bridge | Current creator and consumer | Historical meaning | P3 treatment |
| --- | --- | --- | --- |
| `Entity.canonical_entity_id` | `graph_extraction_materializer` writes it after Entity Resolution; Fact Resolution reads it as subject/object identity. | Current ontology-specific projection assignment, not an immutable decision record. | P3.1 may deterministically reassign this current projection only with a new append-only resolution/evolution decision. |
| `EntityResolutionDecision.canonical_entity_id` | `canonical_entity_resolution` creates active/superseding decisions; candidate lookup reads the active decision. | The canonical target selected when that decision was made. | Never rewrite or delete; a new decision must supersede the old one. |
| `LogicalFact.subject_canonical_entity_id`, `object_canonical_entity_id` | Fact-plan compiler writes them; Fact Lifecycle groups and conflicts by them. | Historical Fact identity inputs. Both composite FKs use `RESTRICT`. | P3.1/P3.2 must not update them. P3.3 creates current interpretation through reconciliation lineage. |
| `FactAssertion.asserted_object_canonical_entity_id` | Fact-plan compiler writes the asserted entity object. | Historical assertion object identity; FK is `SET NULL` only on physical delete. | No P3 rewrite; include it in current derived interpretation. |
| `StablePredicateMapping.stable_predicate_identity_id` | Active mapping is the entry point of Fact Resolution. | RelationType-to-predicate mapping history; current model has `active/superseded/rejected`. | P3.2 must append/supersede mapping lineage, not rewrite historical Facts. |
| `LogicalFact.stable_predicate_identity_id` | Fact-plan compiler writes it; Fact Lifecycle reads policy and temporal class. | Historical Fact predicate identity; composite FK is `RESTRICT`. | P3.2 must not update it; P3.3 reconciles affected Facts. |
| `FactAssertion.logical_fact_id` | Fact materialization writes it; lifecycle uses it to collect assertions. | Immutable historical assertion-to-Fact bridge; composite FK is `RESTRICT`. | P3.3 must not update it. |
| `KnowledgeRelation.logical_fact_id` | Initial Fact materialization writes it through `_bridge`. | Ontology projection's original Fact bridge; composite FK is `RESTRICT`. | P3.3 must not reuse `_bridge` to overwrite it. |
| `RelationEvidence.fact_assertion_id` | Initial Fact materialization writes it through `_bridge`; evidence lifecycle reads it. | Original evidence-to-assertion bridge; composite FK is `SET NULL` only on physical delete. | P3.3 must not update it. |
| `FactResolutionDecision.{stable_predicate_identity_id,logical_fact_id,fact_assertion_id}` | Fact Resolution persists a snapshot plus bridges; lifecycle supersedes decisions. | Resolution audit at the time of the source occurrence; each bridge is `SET NULL` only on physical delete. | Keep snapshots and old rows; future evolution/reconciliation needs a separate decision/lineage audit. |

Primary schema evidence: `app/models/entity.py:55-87`,
`app/models/entity_resolution_decision.py:32-120`,
`app/models/fact_foundation.py:58-178,181-519`,
`app/models/knowledge_relation.py:25-123`, and
`app/models/relation_evidence.py:25-73`. Migration `0064` conservatively created one
CanonicalEntity per historical Entity; migration `0066` created the P2 bridges and
their composite FK contracts.

### A.2 Actual code consumers and notable boundary

- `canonical_entity_resolution.py:420-468` retrieves only `CanonicalEntity.status ==
  "active"` candidates and uses existing `Entity.canonical_entity_id` assignments as
  evidence. P3.1 must make this current-leaf aware without redefining `disabled`.
- `graph_extraction_materializer.py:804-833` writes an Entity projection's canonical
  assignment, while `graph_relation_fact_resolution.py:497-704` reads source/target
  canonical IDs and the active predicate mapping to build Facts.
- `fact_lifecycle.py:151-304` and `:390-439` consume Fact/Assertion/Evidence bridges
  for assertion state and conflict recalculation.
- `graph_publication_*`, `graph_retrieval.py`, `graph_catalog*.py`, and chat graph
  services consume `Entity`, `KnowledgeRelation`, and `RelationEvidence`; no direct
  production reader of `LogicalFact` or `FactAssertion` was found in those paths.

One additional writer is material: `graph_governance_actions.stage_entity_merge`
(`app/services/graph_governance_actions.py:1061-1288`) is an ontology-specific
**Entity** merge. It stages disabled Entity/alias effects and KnowledgeRelation
endpoint reassignment. It is not CanonicalEntity evolution, has no P3 lineage, and
must never be reused for P3 Canonical merge/split or Fact reconciliation.

## 8. Output B - Fingerprint Impact Matrix (Actual)

The source-occurrence fingerprint is independent of extraction-run identity. It is
derived from library, document, revision, evidence unit, chunk, optional block and
source span (`graph_relation_fact_resolution.py:348-394`). Existing tests prove that
the same occurrence replay is stable and a different occurrence is distinct
(`tests/test_p2_3_graph_relation_fact_resolution.py:163-226`).

| Fingerprint | Canonical merge/split | Predicate merge/split | `identity_policy_version` change | Historical handling |
| --- | --- | --- | --- | --- |
| `LogicalFact.identity_fingerprint` | **Changes for future/current derivation.** Subject and entity-object canonical UUIDs are hash inputs. | **Changes.** Predicate snapshot contains ID, namespace, key, contract, temporal class and identity policy. | **Changes directly.** It is an explicit hash input. | Existing value remains the historical row identity. |
| `FactAssertion.assertion_fingerprint` | **Changes indirectly** because it includes LogicalFact fingerprint. | **Changes indirectly** for the same reason. | **May also change directly** when policy changes assertion value, qualifier, polarity, modality or time classification. | Existing value remains immutable. |
| `FactResolutionDecision.subject_fingerprint` | **Changes for future resolution.** It includes source and target canonical UUIDs plus source occurrence and observed relation key. | No direct input. | No direct input. | Existing decision remains queryable by its recorded subject fingerprint. |
| `FactResolutionDecision.decision_fingerprint` | **Changes indirectly** through LogicalFact/Assertion fingerprints. | **Changes** through predicate snapshot and Fact/Assertion fingerprints. | **Changes** through predicate snapshot and Fact/Assertion fingerprints. | Existing active decision is not overwritten; a later source re-resolution must supersede it. |
| `EntityResolutionDecision.subject_fingerprint` | No direct input: it is observation/source/ontology/entity-type scoped. | No input. | No input. | Immutable historical observation identity. |
| `EntityResolutionDecision.decision_fingerprint` | Only a `link_existing` decision hashes the selected canonical UUID. A new link therefore has a different fingerprint; CREATE NEW intentionally excludes its random UUID. | No input. | No input. | Never recompute or rewrite. |

Evidence: Fact fingerprints are composed in
`graph_relation_fact_resolution.py:625-686`; Fact Resolution subject fingerprints
in `:398-424`; Entity Resolution fingerprints in
`canonical_entity_resolution.py:234-312`. The `StablePredicateIdentity` policy is
strictly parsed and includes identity-bearing versus assertion-bearing qualifier
classes (`stable_predicate_resolution_policy.py:1-217`).

**Frozen result:** fingerprint evolution produces a new current derived identity or
new decision. It never changes a historical fingerprint in place.

## 9. Output C - Current / Historical Bridge and Read Contract (Actual)

### C.1 Current implementation state

No `resolve_current_canonical_identity`, `resolve_current_stable_predicate_identity`
or `resolve_current_logical_fact` exists yet. Current P2 writers use the row IDs
directly: Canonical candidate lookup filters `status == "active"`; Fact Resolution
uses the active StablePredicateMapping and direct Entity canonical IDs; LogicalFact
lookup uses the stored identity fingerprint. There is no evolution or reconciliation
lineage table in the current schema.

### C.2 Frozen P3 contract

The conceptual resolver signature is:

```text
resolve_current_canonical_identity(id, context?)
resolve_current_stable_predicate_identity(id, context?)
resolve_current_logical_fact(id, context?)
```

It must return one of:

```text
resolved        unique successor is determined
forked          more than one successor exists and supplied context cannot choose one
pending         evolution/reconciliation is not yet deterministically resolved
historical_only no current successor exists, while the original remains queryable
```

For a split, absence of a specific Entity projection, Assertion source group or other
frozen deterministic context must return `forked` or `pending`; selecting an arbitrary
successor is forbidden.

New Entity Resolution, graph-candidate Fact Resolution and RawClaim-backed Fact
Resolution must write only an unambiguous `resolved` current identity. The existing
P2 pending behavior is the compatibility pattern: an unresolved canonical/predicate
or ambiguous mapping produces a Decision without Fact bridges. Historical reads use
stored FKs and snapshots unchanged. Current reads must be an evolution-aware derived
view; it is not an update of the historical tables.

Existing Publication, Retrieval, graph catalog and chat paths are historical
ontology-projection reads. They remain unchanged for all authorized P3 work; no
automatic Publication or Retrieval migration is permitted.

## 10. Output D - Current-State Consumer Matrix (Actual)

| Consumer | Current P2 input | Required P3 interpretation | Change timing |
| --- | --- | --- | --- |
| Entity Resolution candidate lookup | Active CanonicalEntity and existing Entity projection assignment. | Must resolve current canonical leaf or return `forked/pending`; legacy `status` alone is insufficient. | P3.1 implementation only. |
| Fact Resolution preflight and lookup | One active predicate mapping, direct Entity canonical IDs, direct Fact fingerprint. | Must resolve current canonical/predicate before planning; unresolved split must remain pending. | P3.1/P3.2 implementation only. |
| RawClaim-backed Fact Resolution | Reuses the graph Fact compiler after a binding-backed projection check. | Same current-identity gate as graph-candidate Fact Resolution. | P3.1/P3.2 implementation only; no RawClaim promotion redesign. |
| Initial `KnowledgeRelation` / `RelationEvidence` bridge | `_bridge` writes only the initial direct Fact and Assertion bridges. | Remains historical projection materialization; cannot implement reconciliation by re-bridging history. | Unchanged through P3.3. |
| Assertion lifecycle | FactAssertion status plus RelationEvidence rows by historical bridge. | Historical assertion lifecycle remains auditable; current interpreted lifecycle must use reconciliation assignments. | P3.3 only. |
| LogicalFact status, measurement, polarity | Direct Fact-to-Assertion grouping; measurement and polarity conflict use supported assertions. | Current graph status must be calculated over current reconciled Fact projection, not a mutated old Fact. | P3.3 only. |
| Functional sibling conflict lookup | Subject canonical, predicate, policy, identity qualifiers and ontology relation constraints. | Must group current reconciled identities; otherwise merge/split creates false negatives/positives. | P3.3 only. |
| Publication materialization and Retrieval | Active ontology-specific Entity/KnowledgeRelation/RelationEvidence snapshot. | Historical projection; must remain unchanged and not auto-follow successor lineage. | Out of P3 scope. |
| Graph catalog / UI / chat graph context | Historical Entity/KnowledgeRelation and active RelationEvidence. | Historical projection; no current-Fact UI is present today. | Out of P3 scope. |
| GraphGovernance Entity merge | Current Entity, relation and alias writes within one ontology. | Incompatible with P3 Canonical evolution unless a later implementation enforces mutual exclusion. | P3.1 prerequisite; do not reuse it. |

**Lifecycle contract:** P2.4 must operate on the current reconciled Fact projection
after P3.3. Old LogicalFact rows and their assertion bridges stay historical/auditable
and must not be overwritten to make a current conflict appear or disappear. Evidence
staleness continues to be derived from original RelationEvidence, then projects into
the current reconciliation view. This requires a P3.3 lineage-aware lifecycle query;
it is not present today.

Evidence: `fact_lifecycle.py:151-439` implements the current direct bridge lifecycle;
measurement and polarity cases are covered statically in
`tests/test_p2_4_fact_lifecycle.py:107-176`. Publication and Retrieval use
KnowledgeRelation IDs and ontology versions rather than P2 Fact IDs
(`graph_publication_read.py:285-310`, `graph_retrieval.py:332-351,751-793`).

## 11. Output E - Reconciliation Bridge Contract (Actual)

Current schema confirms that in-place P3 reassignment is invalid:

```text
FactAssertion.logical_fact_id       NOT NULL + RESTRICT
KnowledgeRelation.logical_fact_id   nullable + RESTRICT
RelationEvidence.fact_assertion_id  nullable + SET NULL only on physical delete
```

The normal P2 `_bridge` function (`graph_relation_fact_resolution.py:775-784`) writes
these bridges only during initial resolution and rejects a different existing target.
P3.3 must not repurpose it for historical reconciliation.

The existing source provenance is sufficient for deterministic partitioning without a
new provenance system: the source occurrence snapshot requires document, revision,
evidence unit, chunk and source span; RelationEvidence persists corresponding source
fields; FactResolutionDecision persists `source_snapshot` and `evidence_refs`.
For any legacy row that lacks a unique source group or a complete source occurrence,
`1 -> N` reconciliation must return `pending`.

The frozen P3.3 contract is:

```text
Old LogicalFact(s)
    -> append-only Fact Reconciliation Decision / Lineage
    -> current LogicalFact(s)
```

```text
1 -> 1                allowed
N -> 1                allowed
1 -> N deterministic  allowed only with a unique source-group partition
N -> M                pending in P3 v1
```

Historical Assertions retain their original `logical_fact_id`; historical
KnowledgeRelation and RelationEvidence bridges also remain unchanged. The reconciliation
lineage, not an updated FK, expresses current interpretation and supplies the
current-Fact lifecycle input.

## 12. Output F - Lock Ordering Contract (Actual)

Current locks are useful P2 primitives but are not a P3 multi-identity contract:

| Existing path | Current lock / transaction behavior | P3 conclusion |
| --- | --- | --- |
| Entity Resolution | PostgreSQL transaction advisory lock on Entity Resolution subject fingerprint. | Must join the P3 total order when it can race with projection reassignment. |
| Fact Resolution | Advisory lock on Fact Resolution subject, then LogicalFact fingerprint. | Must resolve current identities first and then join the P3 total order. |
| Fact Lifecycle | Advisory lock on LogicalFact fingerprint and, for state facts, functional scope. | P3.3 lifecycle must use current reconciliation scope rather than historical-only grouping. |
| Extraction materializer | One outer transaction; locks job/library/candidate rows with `FOR UPDATE`. | Preserves materialization atomicity, but does not protect a future evolution operation by itself. |
| GraphGovernance Entity merge | Locks the Library and ontology Entity/relations for its own action. | It does not participate in P3 locks; P3.1 must guard the overlapping Entity projection scope or explicitly reject concurrent governance merge. |

P3 must use the following total order after constructing the **complete** lock set:

```text
(library_id, lock_scope_type, lock_scope_key)

canonical_entity        = 10, key = CanonicalEntity UUID
stable_predicate        = 20, key = StablePredicateIdentity UUID
logical_fact            = 30, key = LogicalFact UUID or derived identity fingerprint
entity_projection       = 40, key = Entity UUID
entity_resolution_subject = 50, key = versioned Entity Resolution subject fingerprint
```

The exact numeric values are part of the P3.1 implementation contract; lexical or
source/target order is forbidden. A command that overlaps an already-applied operation
must acquire its full set, re-read current lineage and preconditions, then fail closed
as `PENDING`, `RETRYABLE_CONFLICT` or `STALE_OPERATION` instead of writing from an old
snapshot.

PostgreSQL advisory behavior, multi-transaction deadlock freedom and rollback are
**SKIPPED - PostgreSQL runtime integration unavailable**. Static evidence only:
`canonical_entity_resolution.py:320-352,637-652`,
`graph_relation_fact_resolution.py:808-856`,
`fact_lifecycle.py:417-439`, and
`graph_extraction_materializer.py:669-1016`.

## 13. Output G - Canonical Split Lifecycle Contract (Actual)

Current CanonicalEntity has only operational status:

```text
active
pending_review
disabled
```

`canonical_entity_resolution._candidate_snapshot` considers only `active` rows, and
migration `0064` already used `disabled` to represent existing disabled/rejected/stale
Entity projections. Therefore `disabled` cannot safely acquire a second meaning of
merged, split or superseded.

The P3.1 design-freeze requirement is:

```text
CanonicalEntity.status = existing operational status only
Evolution lineage      = historical/current/successor and resolution eligibility
```

For a merge or split, the old CanonicalEntity remains retained and historically
queryable. Its evolution lineage marks it as non-leaf/non-eligible for new resolution;
the current resolver follows a single successor only when `resolved`, otherwise returns
`forked` or `pending`. A split must record each Entity projection's deterministic
assignment or explicit pending state. The final table/enum names are deferred to P3.1
design freeze; no lifecycle schema change is authorized by P3.0.

## 14. P3.0 Result and Next Authorized Boundary

P3.0 is **COMPLETED_WITH_KNOWN_LIMITATIONS**. Outputs A-G are closed by static
repository evidence and establish that:

- no existing evolution/reconciliation model can be reused as-is;
- historical Fact/Assertion/Relation/Evidence bridges must not be rewritten;
- current Fact semantics need a derived append-only lineage projection;
- source occurrence lineage is sufficient for deterministic `1 -> N` partition when
  a source group is unique;
- Publication, Retrieval and ontology-specific graph views remain historical and out
  of scope;
- the existing GraphGovernance Entity merge must be excluded or made mutually
  exclusive before P3 evolution implementation.

Known limitations are limited to unavailable GitNexus and PostgreSQL runtime
integration. They do not block P3.1 **design freeze**, but they prevent claims that
future FK enforcement, advisory-lock concurrency or rollback behavior has been runtime
tested.

The next possible stage is P3.1 design freeze only. It must decide the append-only
Evolution Decision/lineage schema, current resolver persistence/query model,
Entity-projection reassignment decision model, GraphGovernance mutual exclusion, and
the concrete P3 lock API. P3.1 implementation, migration, P3.2/P3.3/P3.4, RawClaim
promotion, Publication and Retrieval changes remain **NOT AUTHORIZED**.
