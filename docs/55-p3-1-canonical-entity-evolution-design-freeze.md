# P3.1 CanonicalEntity Evolution Design Freeze

状态：**P3.1 design = FROZEN**

本文件是 P3.1 CanonicalEntity Evolution 的唯一设计真源。它以
[`54-p3-identity-evolution-read-only-baseline.md`](./54-p3-identity-evolution-read-only-baseline.md)
为上游约束，冻结后续实现必须遵守的语义、持久化模型、并发和验证契约。

本文件不是 implementation 授权。

```text
P3.0 = SEALED
P3.1 design freeze = FROZEN
P3.1 implementation = NOT AUTHORIZED
0071 = NOT AUTHORIZED
P3.2 / P3.3 / P3.4 = NOT AUTHORIZED
```

## 1. Scope and Non-goals

P3.1 只定义 CanonicalEntity 的 current-versus-historical evolution。它不改变
历史 Fact 的含义，也不把 ontology-specific Entity governance merge 解释为
CanonicalEntity evolution。

本设计冻结的结果是：同库内的 CanonicalEntity 可以有明确的 merge、split 或
projection reassignment 审计；新的写入只能消费可唯一解析的 current identity；
无法唯一解析时必须停在 pending，而不是猜测。

本阶段和其后续实现都不得做以下事情：

```text
rewrite LogicalFact.subject_canonical_entity_id
rewrite LogicalFact.object_canonical_entity_id
rewrite FactAssertion.logical_fact_id
rewrite KnowledgeRelation.logical_fact_id
rewrite RelationEvidence.fact_assertion_id

StablePredicate merge/split
LogicalFact / FactAssertion reconciliation
Publication migration
Retrieval current-view migration
RawClaim promotion redesign
Graph UI redesign
AI proposal or LLM-based identity selection
```

`CanonicalEntity.status` 继续只表示既有 operational status：`active`、
`pending_review`、`disabled`。它永远不表示 `merged`、`split`、`superseded` 或
current leaf。

## 2. Current Evidence and Ownership

P3.0 已确认且本轮复核的代码约束如下：

| Owner | Current behavior | P3.1 consequence |
| --- | --- | --- |
| `CanonicalEntity` | 只有 operational `status`，且以 `(id, library_id)` 作复合 identity。 | Evolution 必须是独立 append-only lineage，不能重载 `status`。 |
| `Entity.canonical_entity_id` | 是可变的 ontology Entity projection assignment；复合 FK 为 `RESTRICT`。 | 可以更新为新的 current assignment，但每次更新都需要独立审计。 |
| `EntityResolutionDecision` | 同一 resolution subject 只有一个 active 决定；新决定可 supersede 旧决定。 | Reassignment 复制审计 snapshot 并创建新的 `link_existing` 决定，不能改写旧决定。 |
| `canonical_entity_resolution` | 只按 `CanonicalEntity.status == active` 选候选，并仅锁 resolution subject。 | 必须先走 P3 current resolver，并迁移到统一 lock API。 |
| `graph_extraction_materializer` | 持有 outer transaction，随后更新 `Entity.canonical_entity_id`。 | P3 mutation 和 materializer 内的 resolution 必须保留同一 outer transaction 归属。 |
| `graph_relation_fact_resolution` | 直接从 Entity 读取 canonical ID 并在 resolved 时桥接 Fact/Assertion。 | 只增加 current-canonical preflight gate；非 `resolved` 不得桥接任何新 Fact。 |
| `graph_governance_actions.stage_entity_merge` | 锁 ontology Entity、relation、alias；它会 disable loser Entity 并重定向 relation endpoint。 | 不是 Canonical evolution，禁止复用；只可接入相同 projection lock scope 以实现互斥。 |

证据位置：`app/models/canonical_entity.py`、`app/models/entity.py`、
`app/models/entity_resolution_decision.py`、
`app/services/canonical_entity_resolution.py`、
`app/services/graph_extraction_materializer.py`、
`app/services/graph_relation_fact_resolution.py`、
`app/services/graph_governance_actions.py`，以及 migration `0064`、`0066`。

## 3. A. Merge Survivor Semantics

Canonical merge 的唯一默认形态是：

```text
A + B + ... -> existing survivor S
```

- `S` 必须是 command 中显式给出的、同一 library 内已经存在的 current active
  CanonicalEntity；不得按 newest、first、名称、confidence、embedding 或 LLM 自动选择。
- merge 不创建新的 survivor `C`。这不是 convenience default，也不是 replay fallback。
- 每个非 survivor source 必须在执行时解析为自身的 current active leaf；否则返回
  `STALE_OPERATION`，并要求调用方显式按当前 identity 重建 command。
- source CanonicalEntity 不删除、不复用其 ID、不改写历史 Fact 或 Decision；它通过
  applied lineage 指向 `S`，从此不再是 new resolution 的 current leaf。
- survivor 自身不写 self-edge。它是 command participant 和 target，但不构成
  `S -> S` lineage edge。

若先前已经 applied `B -> A`：

```text
merge B -> A  => REUSED
merge B -> C  => STALE_OPERATION
```

第二种请求不得暗中变成 `merge A + C`。调用方必须显式指定新的 current leaves 和
survivor。

## 4. B. Split Semantics

Canonical split 的形态为：

```text
A -> B + C + ...
```

- source `A` 必须是同库 current active leaf；target 至少两个、两两不同，且最终均为
  current active leaf。
- target 可以是已有 CanonicalEntity，或是仅由 command 显式提供的完整 new-target
  specification。merge 永远不允许后一种形式。
- new target 不做名称相似、identifier、embedding 或 LLM 的自动复用。它仅在 split
  所有 projection partition 都 `resolved` 时，在同一 transaction 内创建；PENDING
  split 不创建 orphan CanonicalEntity。
- 每个直接指向 `A` 的 Entity projection 都必须在 partition 中出现一次且只能一次：
  `resolved -> target` 或 `pending -> no target`。遗漏、重复、跨库和无法验证的输入均不
  能 applied。
- 无法唯一分配的 projection 保持原 `Entity.canonical_entity_id == A`；它的
  `CanonicalEntityProjectionAssignment` 记录 `pending`。这不是把它分配给最相近或最新
  successor。

已 applied split 在没有确定性 context 时必须返回 `forked`；尚未完成的 split 或
pending assignment 必须返回 `pending`。两者都不允许产生新的 Entity Resolution 或
Fact Resolution bridge。

## 5. C. Evolution Audit and Lineage Schema

未来 P3.1 implementation 的单一 schema migration（预留为 `0071`，实际创建前仍须
重新核对 Alembic head）必须新增以下模型。此处是设计冻结，不创建 migration。

### 5.1 `canonical_entity_evolution_commands`

每一行是一个 immutable logical evolution command root，不是 Decision。它消除
"一个 idempotency key 只能有一条 Decision" 与 append-only Decision lineage 之间的
矛盾：一个 command 可以有多条 Decision，但一个 library 中一个 idempotency key 只能有
一个 command root。

| Field | Contract |
| --- | --- |
| `id`, `library_id` | UUID primary ID，另有 `(id, library_id)` unique；library FK `RESTRICT`。 |
| `idempotency_key` | 调用方提供的稳定 logical-command key；必须非空且有界。 |
| `request_fingerprint` | 64-char hash，包含 contract version、operation kind、显式 inputs/targets、partition、expected precondition；不含时间或随机 UUID。 |
| `operation_kind` | `merge`、`split`、`reassign`；command root 的 immutable semantic kind。 |
| `contract_version` | 本 command 使用的 evolution/resolver contract version。 |
| `supersedes_command_id` | 可空的同 library 直接前驱 command root；只用于 stale/pending command 被 fresh command 取代时的 command-chain 审计，不替代 Decision 的直接前驱。 |
| `created_at` | 不可变审计时间。 |

数据库必须建立：

```text
UNIQUE (library_id, idempotency_key)
UNIQUE (id, library_id)
FK (supersedes_command_id, library_id) -> commands(id, library_id)
UNIQUE (library_id, supersedes_command_id)           # NULL permitted; no branching command successor
```

因此 `(library_id, idempotency_key)` 唯一标识一个 persisted command chain，而不是一条
Decision 行。`request_fingerprint` 在 command 创建后不可变；同 key 的后续请求必须读取该
command 后比较 fingerprint，不能创建第二个 root。

### 5.2 `canonical_entity_evolution_decisions`

| Field | Contract |
| --- | --- |
| `id`, `library_id` | UUID primary ID，另有 `(id, library_id)` unique；library FK `RESTRICT`。 |
| `command_id` | 非空 root command ID；以 `(command_id, library_id)` composite FK 指向 command。D1、D2、D3 经此字段共享同一 idempotency key、request fingerprint 和 contract version。 |
| `operation_kind` | `merge`、`split`、`reassign`。`reassign` 只改变一个 Entity projection，不创建 global canonical successor。 |
| `lifecycle_status` | `pending`、`applied`、`rejected`、`stale`、`superseded`。`pending`/`rejected` 是审计状态；`stale` 独立于 `rejected`。 |
| `reason_code`, `reason_text` | 必须说明人工/规则依据；`reason_text` 有界且不可承载秘密或原始大 payload。 |
| `method`, `confidence` | `method` 明确产生来源；`confidence` 为可选 `0..1` 审计值，永远不是 split 自动分流依据。 |
| `evidence_refs` | JSONB array，沿用稳定 reference shape；只保留最小必要证据 locator。 |
| `precondition_fingerprint` | 锁前 observation 的可验证摘要；锁后重新读取时不相同即 `STALE_OPERATION`。 |
| `supersedes_decision_id` | 可空的同 command 直接前驱；旧行保留。自引用 composite FK 必须同时携带 `library_id` 和本行 `command_id`，禁止跨 command predecessor。 |
| `created_at` | 不可变审计时间。 |

Decision payload 永远不更新。唯一允许的原地 lifecycle mutation 是：在同一 outer
transaction 中插入直接 successor 后，将其直接前驱从 `pending` 标记为 `superseded`。
`applied`、`rejected`、`stale` 与新 `pending` 均只能在新 Decision 行创建时写入；不得把
同一行从 `pending` 原地改为 `applied`。

```text
command C
  D1: pending
  D2: applied, command_id = C, supersedes_decision_id = D1
  D1.lifecycle_status: pending -> superseded   # the only mutable lifecycle transition
```

数据库还必须建立以下可静态执行的 chain 约束：

```text
UNIQUE (id, library_id, command_id)
FK (command_id, library_id) -> commands(id, library_id)
FK (supersedes_decision_id, library_id, command_id)
  -> decisions(id, library_id, command_id)
UNIQUE (library_id, supersedes_decision_id)          # NULL permitted; no branching direct successor
UNIQUE (library_id, command_id) WHERE lifecycle_status = 'pending'
UNIQUE (library_id, command_id) WHERE lifecycle_status = 'applied'
```

这些约束允许同一 command chain 有多个 append-only Decision rows，同时禁止平行 pending
chain、多条 applied terminal Decision 与跨 command supersede。

### 5.3 `canonical_entity_evolution_sources`

每一行代表一个不再是 current leaf 的 predecessor：

```text
id
library_id
decision_id
source_canonical_entity_id
resolution_state = pending | applied | superseded | historical_only
created_at
```

它以复合 FK 约束同库的 decision 和 CanonicalEntity。`resolution_state` 是 current
resolver 对该 source 的唯一权威状态，而 decision header 的 status 是整条命令的审计
结果，不是第二个 current-pointer。

必须建立 PostgreSQL partial unique index：

```text
(library_id, source_canonical_entity_id)
WHERE resolution_state IN ('pending', 'applied')
```

因此一个 historical canonical 不可能同时拥有两条 current/pending evolution 分支。
`historical_only` 只能是显式 persisted terminal evolution state：该 source 已被声明为不再是
current leaf，且没有 successor。P3.1 的 merge、split 与 reassign command 不创建该 terminal
state；缺少这种 persisted state 时，resolver 不得从 CanonicalEntity operational status 或
"没有找到 successor" 猜出 `historical_only`。

### 5.4 `canonical_entity_evolution_successors`

```text
source_transition_id
library_id
target_canonical_entity_id
target_spec_snapshot
created_at
```

它以复合 FK 约束 source transition 和 target CanonicalEntity。merge 的每个 losing
source 只有一个 successor；split 的一个 source 有两个或更多 successor。顺序只按
stable UUID/target key 正规化用于 hashing 和 lock construction，永远不表达优先级。

对于 fully resolved split，显式 new-target specification 在 transaction 内创建
CanonicalEntity 后写入 `target_canonical_entity_id`。对于 pending split，只记录已存在
target 或 immutable target specification，不产生 target CanonicalEntity，也不形成
applied successor relation。

### 5.5 `canonical_entity_projection_assignments`

```text
id
library_id
evolution_decision_id
entity_id
from_canonical_entity_id
target_canonical_entity_id nullable
assignment_state = resolved | pending | rejected
partition_basis_snapshot
reason_code
previous_entity_resolution_decision_id nullable
new_entity_resolution_decision_id nullable
created_at
```

该表是 Entity projection current assignment 的 append-only audit，不是 historical
Fact reconciliation。它必须以 `(entity_id, library_id)`、`(canonical_entity_id, library_id)`
和 `(id, library_id)` 复合 FK 保证同库。P3.1 migration 还必须给
`entity_resolution_decisions` 添加 `(id, library_id)` unique，才能让两条 Decision FK
同样为 composite、可静态强制。

每个 applied reassignment 都有完整的 old/new Decision link；`pending` projection 的 new
Decision 和 target 均为 NULL。一个 projection 在同一 decision 中只能有一条 assignment。

### 5.6 Schema invariants

未来 migration 必须静态保证：

```text
all lineage and assignment FKs are library-scoped
all hashes are lowercase 64-char SHA-256 values
confidence is NULL or 0..1
JSON evidence/basis shapes are constrained
current/pending source transition is partial-unique
source != target for state-changing successor rows
one library + idempotency key has exactly one command root
all Decision rows reference one persisted command root
one command has no parallel pending chain and no parallel applied terminal Decision
```

跨 library、self successor、没有 successor 的 applied merge/split、split 少于两个
successor、以及将 `reassign` 伪装成 global lineage transition，均由 service validation
拒绝；跨行 cardinality 和 cycle 不能由普通 SQL CHECK 完整表达。

## 6. D. Current Resolver Contract

概念接口冻结为：

```text
resolve_current_canonical_identity(canonical_entity_id, context?)
```

返回 payload 至少包含：

```text
status = resolved | forked | pending | historical_only
historical_canonical_entity_id
current_canonical_entity_id nullable
lineage_decision_ids
resolution_eligible
reason_code
```

`status` 只描述 evolution 解析结果；`resolution_eligible` 由当前 target 的既有
operational status 单独给出。两者严格解耦：Evolution status 绝不从
`CanonicalEntity.status` 推导，operational status 也不改变 lineage 的 current leaf。下游
new resolution 必须同时要求：

```text
status == resolved
current_canonical_entity_id is not null
resolution_eligible == true
```

结果规则如下：

| Result | Meaning |
| --- | --- |
| `resolved` | 无 evolution successor 的唯一 current leaf 解析为自身，或 lineage 唯一到达一个 current leaf；该 leaf 可以是 `active`、`pending_review` 或 `disabled`。 |
| `forked` | 已 applied split 有多个 successor，但 context 没有唯一的 persisted partition。 |
| `pending` | source 有 pending evolution、指定 projection assignment pending、context 无效/不在 partition，或检测到 lineage integrity failure。 |
| `historical_only` | 仅由显式 persisted terminal evolution state 表示：该 historical identity 已被声明为不再是 current leaf，且没有任何 current successor。它绝不能由 `disabled`、`pending_review`、普通缺失 successor 或 read-time integrity failure 推断。 |

例如，一个没有 evolution successor 的 disabled CanonicalEntity 仍然是唯一 current leaf：

```text
status = resolved
current_canonical_entity_id = self
resolution_eligible = false
```

相反，只有 source transition 明确为 `historical_only` 且没有 successor 时，resolver 才返回
`historical_only`。没有这种明确 persisted terminal state 的异常 lineage 一律 fail closed 为
`pending`。

第一版 `context` 只允许 persisted deterministic key：

```text
Entity projection ID
Entity Resolution subject fingerprint
```

subject fingerprint 只有在当前 active EntityResolutionDecision 能唯一映射到同库 Entity
projection 时才可转换为 projection context。名称、alias、时间、latest row、相似度、
embedding、confidence 和 LLM 输出均不是 context。

resolver 沿 successor chain 解析；每一跳都重复同样规则。任何 cycle 或不满足 schema
invariant 的读时数据都 fail closed 为 `pending`，并保留 integrity reason，不尝试选择
一条路径。

## 7. E. Entity Projection Reassignment Contract

`Entity.canonical_entity_id` 保持 current projection assignment，但不再能单独更新。
所有 P3 reassignment command 的 atomic unit 必须是：

```text
lock complete scope
-> re-read lineage, Entity, active EntityResolutionDecision
-> validate preconditions and partition
-> append Evolution Decision / source / successor / assignment audit
-> append new EntityResolutionDecision(link_existing)
-> mark prior active EntityResolutionDecision superseded
-> update Entity.canonical_entity_id
-> flush; outer transaction commits or rolls back all rows together
```

新的 EntityResolutionDecision 必须复制旧 decision 的 immutable observation、candidate
snapshot 和 evidence snapshot，使用新的 selected canonical、`method = canonical_evolution_v1`
和新 decision fingerprint，并以 `supersedes_decision_id` 指向旧 active decision。旧
Decision 绝不重写或删除；candidate 已 purge 时继续保留其 snapshot，FK 可为 NULL。

缺少唯一 active prior EntityResolutionDecision、Entity 已不再指向 command 的 source、
或 partition 未唯一确定时，结果是 `PENDING`：不得更新 Entity，亦不得 supersede 旧
Decision。这样 legacy projection 不会被凭空归属到 split successor。

merge 的 losing canonical 全部直接 Entity projections 都必须得到唯一 survivor 的
`resolved` reassignment。split 的直接 projections 必须完整 partition；每一行要么
resolved 到显式 target，要么 pending 且保持原 assignment。

standalone `reassign` 只允许一个显式 Entity projection 在两个同库 current active leaves
之间变更，并遵循完全相同的 Decision/supersede/transaction 规则。它不能建立或修改
global canonical successor lineage。

## 8. F. GraphGovernance Mutual Exclusion

`graph_governance_actions.stage_entity_merge` 是 ontology-specific Entity merge。P3.1
不得调用、复制或扩展它来实现 Canonical merge/split；它的 relation endpoint rewrite 和
Entity disable 都不是 Canonical evolution 的语义。

未来 implementation 只在并发层使两个入口互斥：

1. Canonical evolution command 对所有被 partition/reassigned 的 Entity projection 使用
   `entity_projection` lock。
2. GraphGovernance Entity merge 在锁定 survivor/loser Entity 前，对同一 Entity IDs 使用
   同一个 `entity_projection` lock API。
3. 两者在获得所有 advisory locks 后都必须重新读取 Entity、active EntityResolutionDecision
   和 current lineage；旧 expected state 不可继续写入。

命令状态已失配时返回 `STALE_OPERATION`；数据库 serialization/unique race 返回
`RETRYABLE_CONFLICT`；都不能继续在旧 snapshot 上应用 effect。

## 9. G. Unified Lock Contract

未来新增的 lock owner 必须是一个共享 service，而不是在各 resolver 中复制 hash prefix。
概念 API：

```text
lock_graph_identity_scopes(db, library_id, scopes)
```

调用方先构造完整的去重 scope set，再按以下 total order 排序，然后逐个取得 PostgreSQL
transaction advisory lock：

```text
(library_id, lock_scope_type, lock_scope_key)

10 canonical_entity          CanonicalEntity UUID
20 stable_predicate          StablePredicateIdentity UUID (reserved for P3.2)
30 logical_fact              LogicalFact UUID/fingerprint (reserved for P3.3)
40 entity_projection         Entity UUID
50 entity_resolution_subject versioned subject fingerprint
```

P3.1 实际使用 `10`、`40`、`50`。`20`、`30` 已保留在同一 API 中，防止后续阶段创造第二套
排序。原 `canonical_entity_resolution._lock_resolution_subject` 必须迁移至该 API；否则
Canonical evolution 无法与新 extraction resolution 正确互斥。

构造完整 scope set 时允许先做无锁 discovery，但拿锁后必须用 `FOR UPDATE` 重新读取并
验证：current source/target leaves、source transition、所有 projection、active resolution
decision、complete partition、expected precondition fingerprint 和 cycle condition。discovery
与 re-read 不同即不得继续。

正式 result vocabulary 冻结为：

```text
APPLIED             all atomic effects staged in the owning transaction; durability follows outer commit
REUSED              same idempotent semantic command already applied
PENDING             durable incomplete split/projection decision; no unsafe reassignment
STALE_OPERATION     stale audit only, with no lineage or projection mutation; caller rebuilds from current state
RETRYABLE_CONFLICT  no success claim; retry transaction from fresh read
REJECTED            invalid/scope/policy/cycle command, with explicit reason
```

`STALE_OPERATION`、`RETRYABLE_CONFLICT` 和 `PENDING` 不得被统称为 `rejected`。

## 10. H-I. Replay, Idempotency, and Cycle Prevention

### Replay

`request_fingerprint` 使用已规范化的 stable inputs，包含 command 的 expected current
preconditions，不能包含时间、random ID、candidate job ID 或 row insertion order。它与
`idempotency_key` 均由 `canonical_entity_evolution_commands` root 行持久化，而不是作为
Decision 行唯一键。

- 同一 library、同一 idempotency key、同一 request fingerprint：读取唯一 command chain。
  chain 已有 `applied` Decision 时返回 `REUSED`，不新增 Decision、source、successor、
  assignment 或 EntityResolutionDecision；chain 当前有 `pending` Decision 时返回该
  `PENDING` 状态，不得创建平行 pending chain。
- 同一 idempotency key 配不同 request fingerprint：读取同一 command root 后返回
  `REJECTED(idempotency_key_conflict)`；不得创建第二个 command root 或 Decision chain。
- pending 完成必须 append successor Decision：`D1 pending -> D2 applied`
  (`D2.supersedes_decision_id = D1`)，并仅将 D1 标记为 `superseded`。D1 不得原地变成
  `applied`。
- 历史上已存在等价 applied direct transition 时，即使调用方换了 idempotency key，也返回
  `REUSED`，不制造重复 lineage。
- 被 supersede 的 pending/stale command 不可重新激活；调用方用 fresh precondition 和新的
  idempotency key 发出新 command root，并以 `supersedes_command_id` 留下 command-chain 直接
  历史。`supersedes_decision_id` 只连接同一 command 内的 D1 -> D2 直接前驱。

### Cycle prevention

在完整 lock set 内、写入 successor 前，对 proposed state-changing edge 执行同库
reachability check：若任一 target 已可到达 source，拒绝。

```text
A -> B, then B -> A        REJECTED(cycle)
A -> B -> C, then C -> A   REJECTED(cycle)
```

merge survivor self-membership 不生成 edge，因此不是 cycle exception。cycle protection
必须覆盖 multi-source merge 和 multi-target split；读时若遇到损坏 cycle，resolver 返回
`pending(lineage_integrity_error)`。

## 11. J. Transaction Ownership

Evolution service、current resolver 和 assignment writer 都不调用 `commit()` 或
`rollback()`；调用方拥有 outer transaction。它们可以使用 nested savepoint 来识别
unique/serialization race，但不得在外层事务内留下部分 applied state。

一次 applied evolution 的事务边界至少包含：

```text
all advisory locks
all required FOR UPDATE reads
CanonicalEntity creation for fully resolved split, if any
Evolution Decision / sources / successors / assignments
new and superseded EntityResolutionDecision rows
Entity.canonical_entity_id updates
```

`graph_extraction_materializer` 已在 `materialize_graph_extraction_job` 中拥有 outer
transaction；P3 integration 必须适配这一事实。GraphGovernance 和管理 command 同样由其
入口事务持有。任何异常、stale precondition 或 failed postcondition 都必须使上述写入
一起 rollback；不得只留下 Entity 更新或半条 lineage。

## 12. K. Exact Future P3.1 Implementation Scope

在明确授权 P3.1 implementation 后，允许的最小范围是：

1. 一个新的 `0071` schema migration，创建第 5 节的 command-root、lineage/assignment
   tables、composite FKs、partial unique indexes 和 EntityResolutionDecision `(id, library_id)`
   unique；不修改任何 historical Fact bridge。
2. ORM models、typed command/result contracts 和 Canonical evolution service；command
   必须显式接收 survivor、targets、partition、idempotency key、reason、method、evidence
   和 expected preconditions。
3. shared multi-identity lock service，并把 Entity Resolution subject lock 迁移到它。
4. `resolve_current_canonical_identity`，并把它接入 Entity Resolution candidate/existing
   mapping path。
5. Entity projection reassignment writer，与旧/new EntityResolutionDecision 的 supersede
   链一起运行。
6. `graph_extraction_materializer` 的 current-canonical gate，以及 shared Fact Resolution
   preflight 的 canonical-only gate：非 `resolved` identity，或 `resolved` 但
   `resolution_eligible == false` 的 identity，均写 P2-compatible pending decision，且不创建
   LogicalFact、FactAssertion、KnowledgeRelation 或 RelationEvidence 的新 bridge。该 gate 不改变
   RawClaim promotion 模型或 API。
7. `stage_entity_merge` 对相同 Entity projection scopes 使用共享 lock API，保持其现有
   ontology-specific effect 语义不变。

明确不属于 P3.1 implementation：StablePredicate evolution、Fact reconciliation、current
Fact lifecycle、RawClaim promotion semantics、Publication、Retrieval、graph UI，及任何对
历史 Fact/Assertion/Relation/Evidence FK 的 update。

## 13. L. Required Implementation Tests

未来 implementation 必须使用已有 pytest/SQLAlchemy 约定，至少覆盖：

| Area | Required evidence |
| --- | --- |
| Merge | 显式 existing survivor、无 self-edge、losing source 唯一解析、禁止自动新 C。 |
| Resolver status separation | 无 successor 的 `active`、`pending_review`、`disabled` leaf 都为 `resolved(self)`；仅 `active` 为 resolution-eligible。`historical_only` 仅由显式 terminal evolution state 产生，不能由 disabled 或缺失 successor 推断。 |
| Split resolver | 无 context 为 `forked`；唯一 Entity projection/subject context 为 `resolved`；pending/invalid context 为 `pending`；没有 first/latest fallback。 |
| Projection partition | 覆盖全部 direct projections、重复/遗漏拒绝、pending 不改 Entity、resolved 原子更新 Entity 与新/旧 ER Decision。 |
| History | 旧 EntityResolutionDecision 和所有 Fact/Assertion/Relation/Evidence bridges 不变。 |
| Command replay | `(library_id, idempotency_key)` 只产生一个 command root；相同 fingerprint 的 applied chain 复用、pending chain 返回既有 `PENDING`、key 冲突拒绝；D1 pending -> D2 applied 必须 append 并仅将 D1 标记 superseded。 |
| Replay | 相同 command 复用；key 冲突拒绝；`B -> A` 重放复用；`B -> C` 在 B 已解析为 A 时 stale。 |
| Cycle | 两节点、长环、多 source/target 环全部拒绝；损坏读时 fail closed。 |
| Cross-library | schema/model/service 都拒绝跨 library source、target、Entity、Decision。 |
| Lock API | complete set 去重后固定排序；Entity Resolution 与 GraphGovernance 共享 projection scope；锁后 stale re-read 返回正确 vocabulary。 |
| Transaction | Evolution audit、ER Decision supersede 与 Entity update 一起 rollback；无部分 lineage。 |
| New Fact guard | canonical `forked`/`pending`/`historical_only`，以及 `resolved` 但 `resolution_eligible == false`，仅产生 pending FactResolutionDecision，且无 Fact/Assertion bridge。 |
| Regression | Publication、Retrieval、RawClaim promotion、P2 historical Fact reads 保持既有行为。 |

可稳定自动化的 service/schema 行为使用 T2 strict RED -> GREEN。当前文档任务本身是 T0：
只需要文档 read-back、contradiction search 和 diff whitespace verification，不新增测试代码。

## 14. M. Known Limitations and PostgreSQL Runtime Requirements

`VECTOR_KB_PG_TEST_DSN` 当前未配置。以下只能在 implementation 后以 migration/ORM/static
contract 验证，不能声称 runtime PASS：

```text
composite FK enforcement
partial unique index enforcement
transaction advisory lock ordering and contention
true concurrent overlapping merge/split/reassignment
outer transaction rollback
```

对应 PostgreSQL integration tests 必须在 disposable DSN 下显式运行；在该环境不可用时，
报告必须写为：

```text
schema/service contract PASS
PostgreSQL runtime integration SKIPPED
```

GitNexus MCP 当前不可用；P3.0 已记录 `npx gitnexus analyze` 因 `tree-sitter-kotlin`
无法加载 `node-gyp-build` 失败。本设计依赖已记录的 manual source/migration/test fallback；
它不代替未来 implementation 前的 symbol-level impact analysis。

## 15. Freeze Acceptance and Stop Condition

本设计已冻结以下内容：

```text
A. explicit existing merge survivor semantics
B. fail-closed split and complete Entity projection partition semantics
C. immutable command-root plus append-only evolution decision, source, successor, and assignment schema
D. four-state current resolver contract, strictly separate from CanonicalEntity operational status
E. Entity reassignment plus EntityResolutionDecision supersede transaction
F. GraphGovernance projection-level mutual exclusion
G. shared lock API and fixed total order
H. command-root replay and idempotency behavior with append-only Decision lineage
I. cycle prevention
J. outer transaction ownership
K. exact implementation boundary
L. required test matrix
M. PostgreSQL runtime verification gap
```

下一步仅能是一次单独授权的 P3.1 implementation。它开始前必须重新检查本文件、P3.0
真源、当前 Alembic head、Git worktree 和每个将被修改 symbol 的 impact。P3.2、P3.3、
P3.4、Publication、Retrieval、RawClaim promotion 和历史 Fact reconciliation 仍然不获授权。
