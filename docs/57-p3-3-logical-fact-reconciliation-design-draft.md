# P3.3 LogicalFact Reconciliation Design Freeze

状态：**P3.3 design = FROZEN**

**P3.3 implementation = AUTHORIZED (P3.3-A only)**。

P3.3-A 的唯一范围是 `0074` 持久化基础、ORM 映射、PostgreSQL 约束，以及迁移和约束的
数据库验证。它不接入 Fact Lifecycle、Fact Resolution、current resolver、Publication、Retrieval、
API、UI、chat 图谱读取或部署；这些均须在 P3.3-A 验收后单独授权。P3.3-A 也不改变任何历史
`LogicalFact`、`FactAssertion`、`KnowledgeRelation` 或 `RelationEvidence` bridge。

## 1. 目标、边界与当前基线

P3.3 要解决的是已发生 CanonicalEntity / StablePredicateIdentity 演进后，如何给旧
`LogicalFact` 一个可审计的**当前解释**，而不重写任何历史桥接：

```text
old LogicalFact(s)
  -> append-only reconciliation Command / Decision / lineage
  -> current LogicalFact target(s)
  -> current reconciled Fact projection for P2.4 lifecycle/conflict
```

本阶段允许的图形只有：

```text
1 -> 1
N -> 1
1 -> N, with a complete deterministic assertion partition
```

`N -> M`（`N > 1` 且 `M > 1`）不在 P3.3 v1 范围内。它必须在 prepare/evaluation 返回
`PENDING(n_to_m_not_authorized)`，不得部分写入、拆成隐式多条命令，也不得由服务选择一组
pairing。这是 pre-admission 的非持久化 `PENDING`：不创建 Command、Decision 或 source slot，
也不占住一个后续可修正的 pending intent。

当前本地基线：

```text
P3.0 = SEALED
P3.1 = implemented and PostgreSQL-accepted
P3.2 = implemented and PostgreSQL-accepted locally
Alembic local head = 0073
P3.2 repair commit = 3140e2ee4d1766b2251e1f5f22ee9f6a4ad255b7 (pushed)
P3.3 = design frozen; P3.3-A authorized only
```

`3140e2e` 修的是 `0073` PostgreSQL trigger，未改变 P3.2 的语义合同。P3.3 可以据此起草，
但在该提交被明确保留为 P3.2 基线并推送前，不得冻结 P3.3 implementation checkpoint、创建
`0074` 或执行 P3.3 schema。

唯一前置真源为：

- `docs/54-p3-identity-evolution-read-only-baseline.md`
- `docs/55-p3-1-canonical-entity-evolution-design-freeze.md`
- `docs/56-p3-2-stable-predicate-evolution-design-freeze.md`

本设计不改变它们的历史保护结论：

```text
LogicalFact historical identity fields                never rewritten by P3.3
FactAssertion.logical_fact_id                         never rewritten
KnowledgeRelation.logical_fact_id                     never rewritten
RelationEvidence.fact_assertion_id                    never rewritten
FactResolutionDecision historical bridges/snapshots   never rewritten
```

`LogicalFact.status` 和 `FactAssertion.status` 仍有既有 P2 lifecycle 含义。P3.3 不利用它们
改写旧事实来伪造“当前”结论；current projection 的计算和写入边界见第 8 节。

明确不在本阶段范围内：RawClaim promotion redesign、FactAssertion physical reassignment、
KnowledgeRelation / RelationEvidence migration、Publication migration、Retrieval current view、
Temporal Query、Conflict resolution engine、AI/LLM 自动 reconciliation、GraphGovernance Entity merge。

## 2. 当前证据和所有者

| Owner | 已有语义 | P3.3 的约束 |
| --- | --- | --- |
| `app/models/fact_foundation.py` | `LogicalFact` 是 identity row；`FactAssertion.logical_fact_id` 是历史 bridge。 | 新 target Fact 可以插入；旧 identity 和 bridge 不得原地变更。 |
| `app/services/graph_relation_fact_resolution.py` | 编译 Fact identity / assertion fingerprint，并只在初次 materialization 通过 `_bridge` 写历史 relation/evidence bridge。 | P3.3 不得复用 `_bridge` 做 reconciliation；target identity 必须复用或提取同一确定性 identity compiler，不能另写一套 hash。 |
| `app/services/fact_lifecycle.py` | 直接按 `FactAssertion.logical_fact_id`、Evidence 和 functional scope 计算 P2.4 状态。 | 保留历史路径；另加 current reconciled projection 的输入路径，不能把旧 row 的状态当作 current result。 |
| `app/services/graph_identity_locks.py` | 已冻结 scope `10/20/30/40/50` 及 total order。 | P3.3 只能使用 scope `10 canonical_entity`、`20 stable_predicate`、`30 logical_fact`，不取得全库锁。 |
| P3.1 / P3.2 evolution services | canonical/predicate 当前 identity 可被 persisted lineage 唯一解析或返回 fail-closed 状态。 | P3.3 的 target spec 只能引用 `resolved` 的 current identity，不能从名称、时间、相似度或最新 row 猜测。 |

未来 implementation 的 blast radius 是 **HIGH**：它会涉及 shared model/migration、P2.4 lifecycle、
Fact Resolution admission 和 PostgreSQL transaction concurrency。该判断不是本轮 production edit 的
授权；实施前必须重新做 symbol-level impact analysis 或项目约定的手工 fallback。

## 3. 词汇与不可变对象

### 3.1 Command、Decision 和 current target

- **Command root**：一个不可变的 reconciliation 范围和 target topology；没有 predecessor，不能被
  cross-command supersede。
- **Decision**：同一 root 下某次 evaluation、correction 或 cancellation 的 append-only version。
- **source transition**：某个 source Fact 在某个 Decision 下的 proposed/applied current-exit。
- **target slot**：Command scope 内命名的 target Fact identity specification。它不是按 row insertion
  order 推出的 target。它显式声明 `existing`（复用一个指定的 current Fact）或 `new`（创建一个
  由 Command Identity 导出的 Fact），从不按 fingerprint 查询结果的 first/latest row 自动选择。
- **source-target edge**：source transition 与允许的 target slot 的显式连接。它使 `N -> 1` 的多个
  sources 可以指向同一个 slot，也使 `1 -> N` 的完整 target set 可持久化。
- **assertion assignment**：一个历史 `FactAssertion` 对一个 source transition 和一个 target slot 的
  append-only current-projection membership；它绝不修改 assertion 的 `logical_fact_id`。

`pending` proposal 不产生 physical target `LogicalFact`，也不改变 current resolver 或 P2.4 输入。
只有一条 `evaluated_outcome=applied AND lifecycle_status=applied` Decision 的完整 child set 才创建
target Fact 并成为 state-changing lineage。

### 3.2 Result vocabulary

普通 evaluation 对调用方只返回下列 one-of outcome：

```text
APPLIED
PENDING
REJECTED(reason_code)
STALE_OPERATION(reason_code)
RETRYABLE_CONFLICT(reason_code)
```

`RETRYABLE_CONFLICT` 不持久化 Command 或 Decision。它只表示 advisory lock、row lock、
serialization/deadlock 或 commit 竞争后未能取得可信 fresh snapshot；其 transaction 的 P3.3
writes 必须为零。

resolver 返回：

```text
resolved        one persisted current Fact target is determined
forked          more than one applied target exists and deterministic context is absent
pending         a persisted pending intent/assignment prevents a current answer
historical_only an explicit terminal source state has no current successor
```

没有 lineage 的 original Fact 是其自身的 `resolved` leaf。`historical_only` 只能读取显式
persisted state；P3.3 v1 writer 不创建它，不能从 `LogicalFact.status`、没有 row、first/latest row
或 target count 推断。`disabled` CanonicalEntity 也从不等价于此状态。

### 3.3 Allowed topology admission

Command identity 的 source count 和 target count 同时决定形状：

| source count | target count | admission |
| --- | --- | --- |
| 1 | 1 | `1_to_1`，全部 Assertions 必须唯一分到唯一 target。 |
| N > 1 | 1 | `n_to_1`，每个 source 的全部 Assertions 都分到同一 target。 |
| 1 | N > 1 | `1_to_n`，每个 Assertion 都必须由 persisted deterministic source group 唯一分到一个 target。 |
| N > 1 | M > 1 | `PENDING(n_to_m_not_authorized)`；零 child/effect 写入。 |

P3.3 不允许 `0` sources、`0` targets、重复 source UUID、重复 `target_key`、target identity
重复、source 等于 state-changing target、source 已不是 current leaf、或 source/target 跨 library。
`new` target 的 server-compiled identity fingerprint 在 scope-30 锁后必须查询同 library 的**全部**
physical `LogicalFact` row（包括 historical source、non-current target 和 inactive Fact），并以
`FOR UPDATE` 固定观察集。只要该集合非空，prepare 必须返回非持久化
`PENDING(target_identity_already_materialized)`：不创建 Command、Decision、source slot 或 target
Fact。它不得只查 current Fact，也不得把已有 physical Fact 自动当作 `existing` target；调用方只能
显式提交 `existing` ref，且该 ID 必须由 resolver 唯一解析为 current leaf，并逐字段匹配
`target_spec`。`forked`、`pending`、`historical_only` 或与 spec 不匹配的 existing ref 分别返回
`PENDING(existing_target_not_resolved)` 或 `PENDING(existing_target_spec_mismatch)`。这样 P3.3 永不
制造会使既有 Fact Resolution 变成 `logical_fact_identity_ambiguous` 的第二个 physical Fact。

一个 state-changing source-to-target edge 永远不允许 `source_fact_id = target_fact_id`。若 `N_to_1`
要把 `B,C` 并入已存在的 current Fact `A`，`A` 是显式 `existing` target，Command sources 只列出
离开 current leaf 的 `B,C`；`A` 的直接历史 Assertions 继续以 `A` 为 leaf，并在 current projection
中与来自 `B,C` 的 assignments 合并。`1_to_n` 不支持“一个 target 恰为 source 本身”的 shortcut；
它必须使用不同的、完整可验证 target identity，或保持 pending 等待后续单独设计。

## 4. Command and Decision Identity

### 4.1 Canonical JSON rules

所有 identity 使用 RFC 8785 JCS UTF-8 bytes 和 lowercase SHA-256；UUID 用 canonical lowercase
string，object keys 按 JCS，set arrays 按其元素 JCS bytes 排序去重。数组若表达 business sequence，
必须在本节明说且保留该 sequence；本设计未声明任何 reconciliation array 为 insertion-order
significant。NFC、长度、JSON depth、byte cap、ASCII code 及 no-extra-key 规则沿用 P3.1/P3.2。

`idempotency_key`、actor/request audit、reason/evidence、precondition token、created time、generated
physical UUID、lifecycle status 和 row insertion order 都不进入 Command Identity。caller 不得提交或
重算 target Fact fingerprint；server 必须用与 P2 Fact compiler 相同的 normalized identity inputs
重算并逐字段验证它。

### 4.2 Normalized Command Identity

P3.3 v1 只有一个 operation：`reconcile`。其 exact normalized object 是：

```json
{
  "library_id": "UUID",
  "operation": "reconcile",
  "schema": "p3_3_fact_reconciliation_command_v1",
  "source_logical_fact_ids": ["UUID"],
  "source_target_topology": [
    {"source_logical_fact_id": "UUID", "target_keys": ["NFC-key"]}
  ],
  "target_slots": [
    {
      "target_key": "NFC-key",
      "target_ref": {
        "kind": "existing",
        "logical_fact_id": "UUID"
      },
      "target_spec": {
        "identity_policy_version": "ASCII-code",
        "identity_qualifiers": {},
        "object_canonical_entity_id": "UUID-or-null",
        "object_kind": "entity|literal|reference|null",
        "object_value": {},
        "stable_predicate_identity_id": "UUID",
        "subject_canonical_entity_id": "UUID",
        "temporal_identity_key": "string-or-null"
      }
    }
  ]
}
```

`target_ref` 是 closed exact shape，而不是带 optional 字段的通用对象：

```json
{"kind":"existing","logical_fact_id":"UUID"}
{"kind":"new"}
```

`new` object 出现 `logical_fact_id`（即使值为 `null`）是非法；`existing` 缺少、为 `null` 或额外
出现任何 key 也是非法。`planned_target_logical_fact_id` 不是 caller payload，而是 server 在取得
Command Identity 后导出的 audit value。实施必须为两种 exact shape 各保留一份 JCS UTF-8 bytes /
SHA-256 golden vector；vector 的 expected bytes 和 hash 固定在测试中，不得由测试时重算后自证。

`source_logical_fact_ids` 按 UUID string 排序；`target_slots` 按 NFC `target_key` UTF-8 bytes 排序；
`source_target_topology` 按 source UUID，内部 `target_keys` 按同一规则排序。topology 的 source set
必须恰等于 source IDs，target-key union 必须恰等于 slots，且每个 source 至少有一个 target key。

`target_spec` 是完整 `LogicalFact` identity input，不含 `identity_fingerprint`。服务在锁后调用
shared P2 compiler，根据 resolved current canonical/predicate state 产生并持久化预期 fingerprint。
`object_value`、qualifiers 的 exact JCS content 属于 identity；没有 name、fuzzy、embedding、LLM、
latest/first row 的选择空间。`existing` ref 的 Fact ID 是调用方显式指定的 target；服务必须证明它是
同库、unique resolved current leaf，且所有 immutable identity fields/fingerprint 与 `target_spec`
完全相同。`new` ref 的 physical ID 是服务器以冻结 namespace UUIDv5 对
`(command_identity_fingerprint, target_key)` 导出的 `planned_target_logical_fact_id`，不使用
idempotency key 或随机 UUID。`new` 仅在不存在同 identity 的 physical Fact 时可创建；它不能替代
显式 existing-target admission。

同 library：

```text
same idempotency key + different Command Identity = REJECTED(idempotency_key_conflict)
different key + same Command Identity             = REJECTED(command_identity_alias_key)
same key + same Command Identity                  = continue exact Decision replay lookup
```

所以 correction 必须继续使用 root 的 idempotency key。若要改变 source set、target spec 或 topology，
必须先合法取消 current pending root，随后以不同 Command Identity 和新 key 另开 root；不得把它
伪装成 cross-command supersede。

### 4.3 Decision Payload Identity and replay response

normal evaluation 的 Decision payload 是：

```json
{
  "confidence": null,
  "evidence_refs": [],
  "expected_precondition_fingerprint": "64-lowercase-hex",
  "method": "manual_or_named_rule",
  "operation_payload": {
    "assertion_assignments": [
      {
        "fact_assertion_id": "UUID",
        "partition_basis_snapshot": {},
        "reason_code": "ASCII-code",
        "source_group_fingerprint": "64-lowercase-hex",
        "source_logical_fact_id": "UUID",
        "state": "resolved|pending",
        "target_key": "NFC-key-or-null"
      }
    ]
  },
  "reason_code": "ASCII-code",
  "reason_text": "bounded NFC text",
  "requested_effect": "stage|apply"
}
```

Assignments sort by `(source_logical_fact_id, fact_assertion_id)`. `state=resolved` requires a non-null
target key allowed by source-target topology; `state=pending` requires null target key. `1_to_1` and
`n_to_1` accept only `resolved`; `1_to_n` may contain pending assignments. A pending group never defaults
to the first, last, closest or highest-confidence target.

The precondition token is server-issued and opaque. Its normalized snapshot includes the root/correction
context, every source leaf/current transition, all source Assertions and their deterministic group snapshot,
current target identity resolver results, planned target UUIDs, current target rows, all existing relevant
P3.3 heads, and the P2.4 current-projection membership that will be recalculated. Any change in a listed
member produces a distinct observed hash after locks. A caller-supplied JSON snapshot is not accepted.

Cancellation uses the same common envelope with `requested_effect="cancel"`,
`method="authorized_cancellation"`, `confidence=null`, and exact
`operation_payload={"control_kind":"cancel_pending","expected_pending_decision_id":"UUID"}`.
It has no Assertion assignment payload. A normal `stage` evaluation may persist only a complete `1_to_n`
proposal that has at least one pending assignment; `1_to_1` / `n_to_1` cannot use `stage`.

Exact normal replay, including a historical superseded Decision, returns:

```text
REUSED(
  reused_decision_id,
  effective_outcome = APPLIED | PENDING | REJECTED | STALE,
  current_decision_id,
  current_decision_status
)
```

`effective_outcome` is the reused Decision's immutable `evaluated_outcome`; `current_decision_status` is
the actual root head at read time. `REUSED` alone never means effects were applied, and a historical
`REUSED(APPLIED)` never revives a superseded branch. Exact cancellation replay is instead
`CANCELLED(cancel_already_committed, decision_id)`; it is not overloaded into `REUSED`.

## 5. Lifecycle, correction and pending release

Decision rows use immutable `evaluated_outcome`:

```text
pending | applied | rejected | stale | cancelled
```

and `lifecycle_status` initialized to that value, plus a later `superseded`. New `superseded` is never an
evaluated result. The complete predecessor-status x new-result matrix is frozen below; every omitted cell
is illegal.

| Current predecessor | pending | applied | rejected | stale | cancelled |
| --- | --- | --- | --- | --- | --- |
| none (D1) | append D1 and complete pending proposal rows | append D1 and atomic target/lineage effects | append audit-only D1 | append audit-only D1 | illegal |
| pending | append correction; supersede old pending Decision and every proposal child | append correction; release old pending then atomically create target/effects | return only, zero writes | return only, zero writes | append cancellation; supersede entire old proposal |
| rejected | append audit/proposal Decision; supersede predecessor Decision | append applied Decision/effects; supersede predecessor Decision | append audit-only Decision; supersede predecessor Decision | append audit-only Decision; supersede predecessor Decision | illegal |
| stale | append audit/proposal Decision; supersede predecessor Decision | append applied Decision/effects; supersede predecessor Decision | append audit-only Decision; supersede predecessor Decision | append audit-only Decision; supersede predecessor Decision | illegal |
| applied | illegal: command closed | illegal: command closed | illegal: command closed | illegal: command closed | illegal: command closed |
| cancelled | illegal: command closed | illegal: command closed | illegal: command closed | illegal: command closed | illegal: command closed |
| superseded | illegal: reload actual head | illegal | illegal | illegal | illegal |

`pending -> rejected|stale` is deliberately return-only. The pending intent keeps its source slot until a
same-root correction is accepted or an explicit cancellation commits. No rejected/stale request can silently
release it.

For a split correction, D1 and D2 always retain the same command target slots. Example:

```text
Command: A -> {B, C}
D1: e1 -> B, e2 -> pending                 (pending)
D2: e1 -> B, e2 -> C                       (applied, or still pending if another assertion remains)
```

`A -> {B}` is not a correction of this split and is illegal. In a correction, D1 payload and child identity
content remain immutable; D2 appends replacement source/target/edge/assignment rows, each direct-predecessor
linked to the corresponding D1 row. D1 Decision and all live proposal rows transition to `superseded` in
the same outer transaction.

Cancellation is an existing-root-only, authenticated control path. It requires root command ID, original
idempotency key, `expected_pending_decision_id`, actor type/id, request id, reason code/text and optional
evidence refs. After authorization but before exposing root details, the service acquires the full lock set,
reads the head `FOR UPDATE`, verifies exact pending CAS and proposal integrity, appends a `cancelled`
Decision that directly supersedes the pending head, and changes all current proposal children to
`superseded`. It creates no target Fact, source, target slot, edge or assignment child, does not update a
historical bridge, and commits atomically. Only after this commit may a different normalized Command Identity
take the released source slot in a later transaction.

## 6. Persistent lineage proposal

The target migration number is **0074 only after 0073 is the frozen deployed baseline**. No migration is
created by this draft.

All new IDs are PostgreSQL UUID, audit timestamps are `TIMESTAMPTZ NOT NULL DEFAULT now()`, ordinary
foreign keys are `RESTRICT NOT DEFERRABLE` unless explicitly identified as the target/LogicalFact circular
pair, and JSON fields are `JSONB`. Service validates JCS byte caps/depth and exact shape; stored `jsonb::text`
byte caps are a separate database bound, not proof that PostgreSQL has recomputed JCS.

### 6.1 Tables

`fact_reconciliation_commands`

```text
id, library_id, idempotency_key, command_identity_fingerprint,
operation_kind='reconcile', command_payload_snapshot,
contract_version='p3_3_fact_reconciliation/v1', created_at
```

It has `UNIQUE(id, library_id)`, `UNIQUE(library_id, idempotency_key)`, and
`UNIQUE(library_id, command_identity_fingerprint)`.

`fact_reconciliation_decisions`

```text
id, library_id, command_id, decision_payload_fingerprint, operation_kind,
requested_effect=stage|apply|cancel, evaluated_outcome, lifecycle_status, operation_payload_snapshot,
expected_precondition_fingerprint, observed_precondition_fingerprint,
reason_code, reason_text, method, confidence, evidence_refs,
supersedes_decision_id nullable, actor_type, actor_id, request_id, created_at
```

It has `(command_id, library_id) -> commands(id, library_id)`, exact same-command predecessor FK
`(supersedes_decision_id, library_id, command_id) -> decisions(id, library_id, command_id)`, one decision
payload unique per command, one current Decision head, and one direct successor per predecessor. Decision
shape checks enforce initial `lifecycle_status=evaluated_outcome`, valid requested-effect/outcome pairing,
and cancellation-only control payload.

`fact_reconciliation_sources`

```text
id, library_id, command_id, evolution_decision_id,
source_logical_fact_id, supersedes_source_transition_id nullable,
resolution_state=pending|applied|superseded|historical_only, created_at
```

It duplicates command ownership intentionally. Its same-command/source predecessor FK is
`(supersedes_source_transition_id, library_id, command_id, source_logical_fact_id)`. It has a live source
partial unique on `(library_id, source_logical_fact_id)` where state is `pending|applied`, so a Fact cannot
silently belong to two current roots. P3.3 v1 insert trigger rejects `historical_only` writers.

`fact_reconciliation_target_slots`

```text
id, library_id, command_id, evolution_decision_id, target_key,
target_ref_kind=existing|new, existing_logical_fact_id nullable,
target_spec_snapshot, target_identity_fingerprint,
planned_target_logical_fact_id nullable, target_logical_fact_id nullable,
slot_state=pending|applied|superseded, supersedes_target_slot_id nullable, created_at
```

The ref kind, explicit existing Fact ID or planned UUID, and immutable spec are command-scope identity. Its
predecessor FK is same-command/same-`target_key`; a partial root/one-successor pair prevents hidden
target-slot branching. An `existing` slot must have equal non-null existing/target Fact IDs and no planned
UUID. A `new` slot must have its planned UUID; it has null target Fact while pending and a same-library
materialized target Fact when applied.

`fact_reconciliation_source_target_edges`

```text
id, library_id, command_id, evolution_decision_id,
source_logical_fact_id, source_transition_id, target_key, target_slot_id,
edge_state=pending|applied|superseded,
supersedes_source_target_edge_id nullable, created_at
```

The redundant source ID and target key make the predecessor FK executable for exactly the same command,
source and target slot. Composite FKs separately prove that `source_transition_id` and `target_slot_id` are
owned by this Decision and match the stored source/key. This table is the persisted topology, not a service
inference.

`fact_reconciliation_assertion_assignments`

```text
id, library_id, command_id, evolution_decision_id,
source_logical_fact_id, source_transition_id, fact_assertion_id,
source_group_fingerprint, partition_basis_snapshot,
assignment_state=resolved|pending|superseded,
target_key nullable, source_target_edge_id nullable,
reason_code, supersedes_assignment_id nullable, created_at
```

The assignment predecessor FK is exact same-command/same `fact_assertion_id`; it cannot cross source or
projection through the duplicated source ID plus source-transition FK. `resolved` requires an edge and
target key; `pending` requires both null. `UNIQUE(library_id, evolution_decision_id, fact_assertion_id)`
and the direct-predecessor uniques prohibit duplicate or branching assignments.

Finally, `logical_facts` gains nullable `reconciliation_target_slot_id` for `new` targets only. A deferrable
circular pair links it to `target_slots.target_logical_fact_id`; a deferred trigger requires that both
columns point to each other, that the Fact ID equals the planned UUID, and that every immutable identity
field/fingerprint exactly matches the slot's server-compiled spec. The physical pairing is frozen as follows:

- only an initial `INSERT` of a new physical Fact may set a non-null
  `reconciliation_target_slot_id`;
- an `UPDATE` from a null pointer to a non-null pointer is always rejected, so no pre-existing Fact can be
  claimed later;
- a Fact with a non-null pointer may update only its ordinary lifecycle `status` and the ORM/server-maintained
  `updated_at` timestamp that accompanies that status update. Its pointer and every immutable identity column
  are forever unchanged; and
- a `new` target slot is inserted either pending with both physical IDs null, or applied with
  `target_logical_fact_id = planned_target_logical_fact_id` and its paired newly inserted Fact in the same
  transaction. A later UPDATE may only mark the slot `superseded`; it may never fill, replace or clear a
  physical target ID.

The immediate `logical_facts` INSERT/UPDATE guard and the deferred circular-pair trigger are separate:
the first permits only `status`/`updated_at` after insert and prevents old-row hijack before a flush, while
the second proves the two new rows pair at COMMIT.
`existing` targets leave the named Fact's pointer unchanged, including when that Fact was created by an
earlier P3.3 command. Because an existing historical Fact cannot acquire a pointer by P3.3 writer, a `new`
slot cannot hijack an old row; because an `existing` slot names its target in Command Identity, it cannot be
chosen by query order.

### 6.2 What database constraints prove, and what they do not

Composite FK, CHECK, unique and partial unique constraints prove same-library ownership, same-command
predecessors, source/target/assignment ownership, one direct successor, idempotency uniqueness and basic
row shapes. They do **not** prove whole-graph membership, target spec compilation, complete Assertion
coverage, source != target, current-head reachability or acyclicity.

`DEFERRABLE INITIALLY DEFERRED` PostgreSQL constraint triggers must prove at COMMIT:

1. exactly one command root, exactly one reachable head and no Decision/source/target/edge/assignment cycle;
2. every `pending` or `applied` Decision has a complete versioned child set whose source/target topology
   exactly matches Command Identity; `rejected`, `stale` and `cancelled` Decisions have no such child set;
   no cross-command, cross-source, cross-target-key or non-current predecessor is accepted;
3. cardinality is only `1:1`, `N:1` or `1:N`; target slots and source-target edges are complete, distinct and
   non-branching;
4. an applied Decision has every `new` target Fact materialized and paired to its slot, every `existing`
   target revalidated, and every source Assertion assigned exactly once; a pending Decision has no
   materialized **new** target Fact and at least one pending assignment;
5. persisted `1:N` source-group rows are complete and map to exactly one target, while `N:1` assigns every
   Assertion to its only target. The trigger verifies this stored structure; the Section 6.3 service
   invariant, not SQL JCS reimplementation, rejects unknown/invalid/non-unique group evidence before it can
   be persisted as applied;
6. an applied source-to-target edge has different same-library logical Fact UUIDs, and the directed applied
   lineage is acyclic even across later commands;
7. only the documented lifecycle transitions and cancellation child absence are legal.

### 6.3 Source-group derivation and its enforcement boundary

`1:N` derives every `source_group_fingerprint` from persisted rows only. There is no caller-supplied group
key, no ``latest``/``first`` query and no fallback from a source name, raw text, similarity, embedding or LLM.
After the full P3 lock set and before child rows are written, the service applies this closed lookup order for
each original Assertion in current projected membership:

1. Read **all** `FactResolutionDecision` rows in the same library whose `fact_assertion_id` equals that
   Assertion ID, with no ordering. If there is exactly one row, its `status` must be `resolved` or
   `superseded`, and its persisted `source_snapshot` must validate as the P2
   `graph_relation_source_occurrence_v1` occurrence after removing P2's non-occurrence additions
   (`predicate`, `source_fingerprint`). The normalized occurrence has exactly
   `schema_version`, `library_id`, `document_id`, `document_revision_id`, `evidence_id`, `chunk_id`,
   `block_id` and `source_span`; UUID fields, nullable `block_id` and the JSON span use the same normalizer
   as `_source_occurrence_snapshot`. Its JCS bytes are the partition basis and its SHA-256 is the group
   fingerprint.
2. If that Decision set is empty, read **all** same-library `RelationEvidence` rows whose
   `fact_assertion_id` equals the Assertion ID. There must be at least one, every row must have a non-null
   `evidence_id`, `document_id`, `document_revision_id`, `chunk_id` and object `source_span`, and their
   normalized occurrence tuples must be distinct. Sort the tuples by JCS bytes and hash the exact object
   `{"library_id":"UUID","occurrences":[...],"schema_version":"p3_3_relation_evidence_group_v1"}`.
   This object is the partition basis and its SHA-256 is the group fingerprint.
3. Any other case is non-persistent `PENDING`: multiple Decision rows is
   `source_group_fact_resolution_decision_ambiguous`; one unusable Decision snapshot is
   `source_group_snapshot_invalid`; no Decision plus no RelationEvidence is
   `source_group_missing_occurrence`; incomplete or duplicate RelationEvidence is
   `source_group_relation_evidence_invalid`. This includes manual and legacy Assertions without a complete
   persisted occurrence. It does not consume a source slot.

The first applicable step is exclusive: a non-empty Decision set never falls through to RelationEvidence,
and multiple candidate rows are never selected by time or ID. The P3.3 implementation must extract or expose
the existing P2 occurrence normalizer rather than recreate it. A durable `stage` proposal is permitted only
when every Assertion has passed the lookup and persisted the exact `partition_basis_snapshot` plus its hash;
it may then leave one or more valid groups explicitly `state=pending` for later human correction. All
assignments bearing the same group fingerprint must use the same target key, and each group has exactly one
target key. Distinct groups may share a target only when that target key is explicitly allowed by the source
topology.

Service code recomputes this lookup after locks and compares the canonical snapshot/hash to the proposed
payload. The PostgreSQL trigger proves coverage, row shape, one assignment per Assertion and one target per
group, but does **not** claim to recompute RFC 8785 or reinterpret legacy JSON inside SQL. Direct SQL that
bypasses the service is unsupported for semantic group derivation; PostgreSQL tests must prove the stated
structural trigger checks, while service tests prove this lookup and its fixed `PENDING` outcomes.

“Every source Assertion” means the source Fact's **current projected membership**, not merely the rows whose
historical `FactAssertion.logical_fact_id` equals that Fact. For an original leaf this is its direct historical
Assertions. For a P3.3 target later used as a source, it is the set of original Assertions whose recursive
resolver path currently terminates at that target. The constraint trigger evaluates this same persisted
recursive membership at commit. Thus a later `X -> Y` reconciliation records assignments with source `X` for
the inherited Assertions, while the earlier `A -> X` assignment remains immutable history rather than a
second final membership.

The migration must not claim that ordinary SQL constraints freeze arbitrary legacy writes to
`FactAssertion.logical_fact_id`, `KnowledgeRelation.logical_fact_id` or `RelationEvidence.fact_assertion_id`:
existing P2 initial materialization has an authorized `NULL -> bridge` path. The P3.3 guarantee is a strict
service-writer invariant plus focused tests: no P3.3 function issues those updates or calls `_bridge`.
It is incorrect to report this as a new static database guarantee without a separately frozen compatibility
contract for P2 initial materialization.

## 7. Current LogicalFact resolver

The public internal contract is:

```text
resolve_current_logical_fact(
  library_id,
  logical_fact_id,
  context = {fact_assertion_id? | source_group_fingerprint?}
) -> {
  status: resolved | forked | pending | historical_only,
  current_logical_fact_id?: UUID,
  current_target_slot_id?: UUID,
  reason_code?: string
}
```

It starts with the requested Fact and follows only persisted applied source-target edges and assignments. For
`1:N`, context must be an Assertion ID or a source-group fingerprint that matches one persisted assignment;
the resolver then repeats from that assignment's target until a leaf. No context returns `forked`. A pending
source/assignment returns `pending` even if another assignment is resolved. A corrupted/multiple current row
fails closed as `pending(lineage_integrity)`; it never chooses first/latest. The resolver does not use
`LogicalFact.status` to choose a target.

Historical readers continue using original `LogicalFact` IDs and bridges. P3.3 does not automatically make
Publication, Retrieval, UI or chat call this resolver.

## 8. P2.4 current reconciled projection contract

P2.4's existing direct bridge calculation remains the historical lifecycle path. P3.3 adds a separate
current-projection query that yields:

```text
current target LogicalFact
  <- terminal applied assignment reached through resolver lineage
  <- original FactAssertion
  <- original RelationEvidence
```

The projected Assertion retains its original status, evidence, time, polarity, modality, value and
functional-source contract. Evidence staleness is still derived from the original `RelationEvidence`; only
its membership is projected. The projection starts at each Assertion's historical Fact bridge and uses the
recursive resolver, so an immutable earlier `A -> X` assignment cannot make the same Assertion appear both
at `X` and at later leaf `Y`. The new target's status is recalculated from this terminal projected Assertion
set, not from `FactAssertion.logical_fact_id` and not from an old Fact's stored status.

For `state_fact`, functional sibling lookup groups current target specs by current subject, predicate,
policy and identity qualifiers. Measurement and polarity checks use the same P2.4 rules over projected
Assertions. Current target Fact rows may have their own `status` updated while they are resolver-current;
historical source Facts are never overwritten merely to make a current conflict appear/disappear. Existing
historical P2.4 recalculation remains available for audit.

P3.3 must not introduce a second conflict algorithm. It reuses P2.4 `is_effectively_supported_assertion`,
time overlap and functional-contract semantics with a different membership query. A new target with an
unknown/invalid projection is `pending`/inactive fail-closed, not silently active.

### 8.1 Mandatory lifecycle event wiring

The current-projection query is not a passive helper. P3.3 implementation must add one shared
`recalculate_current_reconciled_projection_for_assertions` owner and a preceding lock coordinator. For a
same-library Assertion-ID set, the coordinator first makes an **untrusted discovery read** of historical
bridge/lineage solely to construct the complete scope-30 set: every historical source Fact fingerprint plus
every discovered terminal `resolved` target fingerprint. It then takes that complete deduplicated set through
`normalized_graph_identity_scopes` before any legacy Fact or functional lock. After locking it rereads the
same bridge/lineage snapshot `FOR UPDATE`; a changed source/target set is
`RETRYABLE_CONFLICT(current_projection_lock_snapshot_changed)` and rolls back the outer transaction rather
than acquiring an extra late lock.

Only with that revalidated lock snapshot does the recalculation owner reuse the existing P2.4 eligibility,
time-overlap and functional-scope functions against projected membership. `pending`, `forked`,
`historical_only` and lineage-integrity failures contribute no target and leave the target set fail-closed;
they are never redirected to an historical Fact. The recalculation owner never acquires a shared P3 lock
itself: its coordinator, or the already-locked P3.3 `apply` service, has done so before the historical and
current recalculations begin.

The coordinator and recalculation owner are called in the same outer transaction, before commit, from exactly
these mutation entrances:

1. `reconcile_relation_evidence_lifecycle`: collect every affected Assertion ID without mutation, invoke the
   coordinator, then run the existing Assertion-status transition and historical
   `recalculate_logical_fact_status` path unchanged, followed by current-projection recalculation on that
   same set.
2. `reconcile_resolved_fact_decision`: collect both the newly resolved Assertion ID and a prior superseded
   Assertion ID when present without mutation, invoke the coordinator, then complete its existing Assertion
   status/historical lifecycle work and current-projection recalculation on the union. This is the single
   existing entry used after new Fact Resolution materialization.
3. P3.3 `apply`: after source/target/edge/assignment lineage and any new target Fact have been inserted,
   invoke recalculation for the command's complete original Assertion membership before the Decision can
   commit. Its outer service has already acquired the complete P3.3 source/target scope set before any write;
   it does not run the lifecycle coordinator again. `stage`, rejected/stale evaluation and cancellation have
   no projection effect and do not invoke it.

The helper may change only `status` on resolver-current target Facts. It never changes an historical source
Fact merely because its projected successor became active/inactive/conflicted, and it never changes a
historical bridge. If any resolver, projected-membership or P2.4 calculation fails, the caller rolls back
the entire owning transaction; there is no best-effort status update or background repair fallback.

## 9. Lock and transaction contract

Before any state read that can support a write, service constructs the entire deduplicated set and acquires
transaction advisory locks through `normalized_graph_identity_scopes` in the shared order:

```text
(library_id, lock_scope_type, normalized UUID/string key)
10 canonical_entity: all source and target current canonical identities
20 stable_predicate: all source and target current predicate identities
30 logical_fact: source and target LogicalFact identity_fingerprints (64-lowercase-hex strings)
```

Scope 30 deliberately uses fingerprint strings, not physical Fact UUIDs: this is the existing
`_lock_fact_resolution_identity_scopes` key and is the only way reconciliation contends with concurrent
Fact Resolution for the same derived Fact. Target fingerprints are server-compiled before locks; the planned
UUID is a physical pairing key, not a lock key. P3.3 does not take scope 40/50 and never takes a library-wide
lock. It uses `lock_graph_identity_scopes(..., wait=False)`; lock busy is
`RETRYABLE_CONFLICT(fact_reconciliation_lock_busy)` with zero P3.3 writes.

After locks, the transaction reads command/Decision lineage, sources, Assertions, relevant current
canonical/predicate resolver rows, target Facts and current lifecycle candidates `FOR UPDATE`, recomputes
the precondition snapshot, then applies a strict CAS. P3.2 Fact Resolution already takes shared scopes
10/20/30 before its legacy resolution-subject/logical-fact locks. P3.3 must adapt Fact Lifecycle to take its
scope-30 fingerprint lock before its legacy Fact and functional locks; no code path may acquire a shared P3
lock after a legacy lock. Serialization/deadlock/unique races after this point roll back all P3.3 rows and
map only to `RETRYABLE_CONFLICT` after fresh error classification.

The outer transaction owns Command/Decision/lineage writes, target Fact insert, projected lifecycle status
update and old-pending supersession. Partial flush, any P2.4 error, or cancellation error rolls back all of
them. An `APPLIED` service result is not durable until caller commit succeeds.

## 10. Migration direction and runtime acceptance

`0074` is maintenance-only and may be authored only after `0073` is the retained frozen baseline. It is one
PostgreSQL transactional Alembic migration: no `COMMIT`, autocommit block, `CREATE INDEX CONCURRENTLY`,
Python-side online/offline branch or second concurrent DDL path. Existing logical facts remain direct current
leaves; the migration does no backfill, Fact/bridge rewrite or status recalculation.

### 10.1 Fixed upgrade admission and quiescence

Before Alembic starts, the operator stops admission and drains or terminates every P3.1/P3.2 identity writer,
Fact Resolution worker, Fact Lifecycle/evidence writer, graph worker and ad hoc administrative writer. The
database has no universal runtime-admission flag, so this is an explicit maintenance-runbook precondition,
not a claim that a table is empty or that a migration can infer idle writers. Transactions must commit or
roll back before migration; terminating a session is an operator action, never migration logic.

The upgrade executes exactly `SET LOCAL lock_timeout = '5s'`, `SET LOCAL statement_timeout = '300s'`, then
this one server-side block unchanged in online and offline SQL. The advisory key
`3410968108221770891` is the signed big-endian first eight bytes of
`SHA256("vector-kb:p3.3:0074:migration:v1")`.

```sql
DO $p3_3_migration_lock$
BEGIN
    IF NOT pg_try_advisory_xact_lock(3410968108221770891) THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'migration_busy';
    END IF;

    BEGIN
        LOCK TABLE sys_libraries IN SHARE MODE NOWAIT;
        LOCK TABLE canonical_entities IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE entities IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE entity_resolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_identities IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mappings IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE relation_types IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE logical_facts IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_assertions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE fact_resolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE knowledge_relations IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE relation_evidence IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_commands IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_evolution_successors IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE canonical_entity_projection_assignments IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_commands IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_successors IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mapping_evolution_assignments IN ACCESS EXCLUSIVE MODE NOWAIT;
    EXCEPTION WHEN lock_not_available THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'migration_busy';
    END;
END
$p3_3_migration_lock$;
```

This is the fixed lock order. It is deliberately a maintenance-time table boundary, not a runtime
whole-library lock. A pre-existing Fact Resolution, Fact Lifecycle or P3 identity transaction that already
holds any listed incompatible relation makes the migration fail immediately with SQLSTATE `55P03` /
`migration_busy`, before DDL. A request that wrongly enters after the locks are acquired is blocked by
PostgreSQL's normal table-lock semantics and must be prevented by the stopped-admission runbook; with its own
bounded lock timeout it receives a database lock failure and rolls back, while a client without such a bound
may wait for the maintenance transaction. The draft does not invent an unimplemented application error code
for that case. It cannot commit a partial P3.3 write because every migration DDL step is one transaction.

Only after all locks are held may upgrade assert the exact `0073` baseline catalog (the listed P3.1/P3.2
tables and base Fact tables exist; no P3.3 table, trigger/function name or
`logical_facts.reconciliation_target_slot_id` exists), create the six P3.3 lineage tables, add the nullable
pointer, install immediate guards/deferred constraints, validate the expected final catalog, and commit once.
Any precondition, lock or DDL failure rolls back the entire migration. New application binaries start with
P3.3 mutation admission disabled and only open it after every API/worker is P3.3-aware and healthy; old
Fact writers are not semantically compatible after a P3.3 command exists.

### 10.2 Downgrade barrier

Downgrade runs in a fresh maintenance window with the same writer stop, timeouts, advisory key and all
upgrade locks above. It then obtains these additional locks, in this exact order, before checking data or
drawing down constraints:

```text
fact_reconciliation_commands                 ACCESS EXCLUSIVE
fact_reconciliation_decisions                 ACCESS EXCLUSIVE
fact_reconciliation_sources                   ACCESS EXCLUSIVE
fact_reconciliation_target_slots              ACCESS EXCLUSIVE
fact_reconciliation_source_target_edges       ACCESS EXCLUSIVE
fact_reconciliation_assertion_assignments     ACCESS EXCLUSIVE
```

The downgrade's single server-side block repeats the upgrade list followed by this list, every lock using
`NOWAIT`, and maps an advisory or table-lock failure to the same SQLSTATE `55P03` / `migration_busy`.
Only then may it prove that every six P3.3 table is empty **and** every
`logical_facts.reconciliation_target_slot_id` is null. Rejected, stale and cancelled audit rows count as
non-empty. On any non-empty result it raises `P3_3_0074_NONEMPTY_AUDIT` and leaves the entire P3.3 schema
intact. Once a P3.3 row or paired Fact exists, rollback is roll-forward on a P3.3-aware binary while writers
remain stopped; an old binary/downgrade is prohibited.

Required evidence is offline `0073 -> 0074` SQL, offline `0074 -> 0073` SQL, a real disposable PostgreSQL
upgrade/downgrade round trip, lock-contender failures for both directions and trigger-catalog inspection.
SQLite/unit tests cannot replace PostgreSQL constraint-trigger, advisory-lock or concurrent-transaction
acceptance.

## 11. Required implementation test matrix

| Area | Required proof |
| --- | --- |
| topology | `1:1`, `N:1`, fully resolved `1:N`, unresolved group `PENDING`, and every `N:M` rejection/pending branch; explicit existing target, safe new target, any historical/materialized same-fingerprint Fact blocking `new`, and target selection never uses ordering. |
| deterministic partition | full source-group coverage; same-group-to-one-target uniqueness; one Decision, multiple Decision, empty Decision plus complete/incomplete/duplicate RelationEvidence, manual and legacy provenance cases; payload-order normalization; and golden JCS/fingerprint vectors including both closed `target_ref` shapes. |
| lineage | Decision/source/target/edge/assignment full superseded chains; idempotency replay, alias-key conflict, idempotency-key conflict, direct predecessor, cycle prevention and cross-command/source/target predecessor rejection. |
| bridge protection | P3.3 leaves `FactAssertion.logical_fact_id`, `KnowledgeRelation.logical_fact_id`, `RelationEvidence.fact_assertion_id`, existing Fact fingerprints and old Fact identity fields byte-for-byte unchanged. |
| resolver | direct leaf, `N:1`, forked split with no context, resolved split with persisted assertion/group context, pending, explicit historical-only fixture and corrupt lineage fail-closed. |
| lifecycle | historical P2.4 unchanged; both existing lifecycle entrances plus P3.3 `apply` invoke current-projection recalculation in their outer transaction; projected active/inactive, measurement, polarity and functional conflict behavior; original Evidence staleness changes current projection without bridge mutation. |
| atomicity | failure injection after old-pending supersede, target Fact insert, target-slot pairing, assignment insert and target lifecycle update; lifecycle-helper failure and cancellation rollback; all tables and statuses unchanged after rollback. |
| concurrency | PostgreSQL two-connection overlapping reconciliation, same-fingerprint scope-30 contention with Fact Resolution, reconciliation versus lifecycle/current functional scope, lock ordering, fail-fast busy result, post-lock stale CAS and no partial writer. |
| database enforcement | direct PostgreSQL INSERT/UPDATE/DELETE/COMMIT negatives for composite FKs, lifecycle, shape, target/Fact pairing, attempted old-Fact pointer claim/identity rewrite, permitted paired-new-Fact `status` plus `updated_at` update, complete coverage, source != target, applied DAG, cancellation child ban and source-slot ownership. |
| migration | Alembic head, `0073 <-> 0074` offline SQL containing the fixed P3.3 advisory key and ordered `NOWAIT` locks, disposable PostgreSQL upgrade/downgrade, trigger catalog, pre-existing-writer `migration_busy` rejection, a post-lock writer with bounded client lock timeout rolling back without a partial P3.3 write, and non-empty downgrade refusal. |
| regressions | P1/P2/P3.1/P3.2 focused suites, touched-file Ruff, compileall. Full-repository lint remains a separate historical-debt task. |

When `VECTOR_KB_PG_TEST_DSN` is absent, PostgreSQL rows must be reported exactly as
`SKIPPED — PostgreSQL runtime integration unavailable`; no simulated unit result may be called database
acceptance.

## 12. Freeze record and stop condition

以下冻结门已闭合：P3.2 repair `3140e2e` 已保留并推送；Command/Decision JSON、source-group
derivation、target identity compiler、lifecycle matrix、数据库约束与 service invariant、resolver
vocabulary、lock order、migration 和 rollback 语义已完成独立设计复审；本文件在专用 commit
中冻结；用户已授权受限的 P3.3-A。

P3.3-A 只能实现并验收第 10 节所定义的 `0074` 持久化合同和本阶段所需的 ORM 映射。其完成
不自动授权 lifecycle 接入、Fact Resolution 接入、resolver、Publication、Retrieval、API、UI、
chat 图谱读取、部署或任何后续 P3 子阶段。没有真实 PostgreSQL 迁移验证时，不得宣称 P3.3-A
数据库验收通过；没有后续明确授权，不得写入上述非目标的 production 行为。
