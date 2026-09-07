# P3.1 CanonicalEntity Evolution Design Amendment

状态：**P3.1 design = AMENDMENT REQUIRED**

本轮以 checkpoint `330d2c4e085764af8f1df17a0c47cae93ccd25e5` 为输入，继续修订
Command Identity、Decision lifecycle、pending intent release、schema enforcement、result
contract 和验证矩阵。在人工复审确认所有未闭合项前，本文件不得恢复为 `FROZEN`。

本文件是 P3.1 CanonicalEntity Evolution 的唯一设计真源。它以
[`54-p3-identity-evolution-read-only-baseline.md`](./54-p3-identity-evolution-read-only-baseline.md)
为上游约束，冻结后续实现必须遵守的语义、持久化模型、并发和验证契约。

本文件不是 implementation 授权。

```text
P3.0 = SEALED
P3.1 design = AMENDMENT REQUIRED
P3.1 implementation = NOT AUTHORIZED
0071 migration execution = CONFIRMED
0071 = IMMUTABLE; rewrite/reuse/delete = PROHIBITED
future corrective revision = 0072; NOT AUTHORIZED
0071 production/private deployment = UNKNOWN
P3.2 / P3.3 / P3.4 = NOT AUTHORIZED
```

## 1. Scope and Non-goals

P3.1 只定义 CanonicalEntity 的 current-versus-historical evolution。它不改变
历史 Fact 的含义，也不把 ontology-specific Entity governance merge 解释为
CanonicalEntity evolution。

本设计目标是：同库内的 CanonicalEntity 可以有明确的 merge、split 或
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
| `EntityResolutionDecision` | 同一 resolution subject 只有一个 active 决定；同一 Entity 可因不同 subject 同时有多条 active 决定，新决定可 supersede 同 subject 旧决定。 | Reassignment 必须对 projection 的全部 active supporting subjects 做 1:N replacement；逐条复制审计 snapshot 并创建新的 `link_existing` 决定，不能挑 first/latest 或改写旧 payload。 |
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

若原 command 已经 applied `B -> A`：

```text
original key + exact B -> A payload  => REUSED(APPLIED)
new key + same B -> A identity       => REJECTED(command_identity_alias_key)
new identity B -> C                  => STALE_OPERATION after current-leaf re-read
```

第三种请求不得暗中变成 `merge A + C`。调用方必须显式指定新的 current leaves 和
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

本节冻结目标 schema contract，不授权创建、修改或执行 migration。具体 revision path
受 5.0 的 migration-history gate 约束。

### 5.0 `0071` Migration History Gate

只读核查日期：`2026-09-04`。

```text
0071 artifact state = PUBLISHED
0071 CI execution = CONFIRMED
0071 production/private execution = UNKNOWN
0071 rewrite status = PROHIBITED
future corrective revision = 0072; NOT AUTHORIZED
```

revision `0071` 已存在于 commit `fb9a73528726a6761c7bb4ba76ca62c5d6fe8529`，其
`down_revision = 0070`，并可由 `target/codex/upload-project` 到达。本地 Alembic 报告
`0071 (head)`。GitHub Actions runs `33832913808` 与 `33835228017` 已在 disposable
PostgreSQL 16 job 中成功执行 `0070 -> 0071` 并报告 `0071 (head)`；这证明 migration
artifact 已被执行过，但只证明 CI 临时数据库，不证明任何生产、私有或客户数据库状态。

同一 `fb9a735` 也已经包含 pre-amendment P3.1 production code 和 tests；这是仓库真实状态，
不是本修订对该实现的验收。本文的 `P3.1 implementation = NOT AUTHORIZED` 表示该既有实现
不构成符合本目标契约的 implementation baseline，且本轮不得继续修改或部署它；第 12 节的
future implementation 是后续另行授权的 corrective alignment，不是否认这些 artifact 已存在。

用户冻结规则以“任何环境执行过”为边界；disposable CI PostgreSQL 也属于环境。因此上述两次
成功 upgrade 已足够证明 `0071` 进入过 migration history，结论固定为 **`0071` immutable**。
即使未来证明所有生产、私有和客户数据库均未执行，也不得再原位重写、复用或删除 `0071`；
缺少 production deployment evidence 只保留为运维事实未知，不得重新打开 rewrite 分支。

未来若单独授权 schema correction，revision 必须是 `0072`，且只能遵循以下 forward-only
data gate；本轮不创建、修改或执行 migration。gate 中的“五张表”固定且不允许实现自行扩展
或缩减为：

```text
canonical_entity_evolution_commands
canonical_entity_evolution_decisions
canonical_entity_evolution_sources
canonical_entity_evolution_successors
canonical_entity_projection_assignments
```

| Existing database state | `0071 -> 0072` result after separate authorization |
| --- | --- |
| 五张旧 evolution audit tables 全部为空 | 允许在一个 transactional migration 中转换到目标 schema；完成后保持 single head。 |
| 任一旧 evolution audit table 非空 | 在第一条 schema-changing DDL 前以 `P3_1_0072_NONEMPTY_LEGACY_AUDIT` fail closed；Alembic revision、logical schema objects 和所有 row contents 保持事务前状态。旧行不足以确定性重建本文件冻结的 Command/Decision identities，禁止猜测 backfill。 |
| 无法读取全部五张表的 cardinality 或事务性前置条件不成立 | fail closed；不得开始 DDL，不得把未知当作空表。 |

`0072 -> 0071` 同样固定为 data gate：只有五张目标 evolution audit tables 全部为空时才允许
在一个 transaction 中恢复 exact old-`0071` schema；任一表非空时必须在第一条
schema-changing DDL 前以 `P3_1_0072_NONEMPTY_TARGET_AUDIT` fail closed。downgrade 不接受隐式
或默认的数据丢失授权。若未来必须迁移非空旧数据或降级非空新数据，需要新的设计修订和显式
数据处置授权，不能扩展 `0072` 的本契约。

`0071 <-> 0072` 两个方向都冻结为 **maintenance-only**，不是允许旧/新 application traffic
在线并发的 migration。未来执行前，部署控制面必须停止入口并证明除 migration operator 外，所有
可能读取或写入 `sys_libraries`、`canonical_entities`、`entities`、
`entity_resolution_decisions` 或五张 evolution audit tables 的 API、worker、Entity Resolution、
GraphGovernance、evolution、监控和人工 DB sessions 均已排空；无法取得该证据时 migration path
保持不可执行。该运维前置条件不由“当前没有查到 row”推断，也不因生成了 offline SQL 而自动满足。

空表 gate 成功后的 schema path 也不留实现选择：upgrade 在同一 transaction 内按
assignment -> successor -> source -> Decision -> command 的 child-first 顺序删除 **old 0071** 五表，
再按 command -> Decision -> source -> successor -> assignment 的 parent-first 顺序创建第 5 节 exact
target tables、普通 constraints 和 indexes。随后给 `entity_resolution_decisions` 增加 5.5 的 nullable
`evolution_assignment_id` 及 composite FK；只有该列和全部被引用 unique keys 已存在后，才创建 5.6
全部 constraint/immutability triggers，包括读取该列的 Entity/EntityResolutionDecision pairing
triggers。因为 gate 已证明旧五表为空，不做 rename/copy/backfill。
downgrade 先证明 target 五表为空，再按 reverse dependency order 删除全部 target triggers，之后删除
`evolution_assignment_id` composite FK/column，按 child-first 删除 target 五表并按 parent-first 原样
恢复 commit `fb9a735` 中的 old-`0071` objects；既有非 evolution EntityResolutionDecision rows 的
logical values 必须 unchanged。任一 DDL 失败整笔回滚。禁止在依赖列/unique key 尚不存在时创建
trigger，或在仍有 trigger 引用该列时删除 column。

判空不能使用“先 SELECT、后 DDL”的未锁窗口。connected Alembic execution 与 `--sql` offline
artifact 必须逐字执行同一 database-side preflight：在 PostgreSQL migration transaction 中、任何
cardinality query 或 DDL 前，按下列 migration-only total order 对全部受影响表逐一取得
`ACCESS EXCLUSIVE ... NOWAIT` lock：

```text
sys_libraries
canonical_entities
entities
entity_resolution_decisions
canonical_entity_evolution_commands
canonical_entity_evolution_decisions
canonical_entity_evolution_sources
canonical_entity_evolution_successors
canonical_entity_projection_assignments
```

五张 evolution tables 在该总序中的子序仍严格为 parent-first
`command -> Decision -> source -> successor -> assignment`。全部锁成功后，才以 database-side
`DO`/`EXISTS` 检查五张 evolution tables；通过后才执行第一条 schema-changing DDL，并把所有锁持有到
revision update 与 transaction commit。任一 lock 不可立即取得时，必须以 stable
`P3_1_0072_MAINTENANCE_LOCK_UNAVAILABLE` fail closed；transaction rollback 释放此前已取得的 locks，revision、
schema 和 rows 均不变。`NOWAIT` 禁止 migration 在持有 audit-table lock 时等待 Entity/ER table lock，
从而不能与反序旧 writer 形成 DDL deadlock。取得锁或 gate 任一步失败都不得开始 DDL。

不得用 Python `get_bind()` 查询结果决定是否输出后续 DDL，因为 `--sql` generation 没有数据库
结果。offline SQL 必须在第一条 DDL 前包含相同的九表有序 `LOCK TABLE ... NOWAIT`、cardinality
check 和 stable exception，并以 `psql -v ON_ERROR_STOP=1` 或等价 fail-fast 客户端在匹配 revision
的 disposable PostgreSQL 上实际执行。该 maintenance-only table lock profile 不得被复用为 runtime
command 的全库锁；第 9 节 runtime advisory-lock contract 保持不变。

### 5.0.1 Target Physical Column Profile

为避免 `0072` 实现自行发明类型/边界，以下 profile 对 5.1-5.5 的目标列具有约束力；各表字段清单
中标出 nullable 的列才可 NULL，其余列均 `NOT NULL`：

| Field family | Frozen PostgreSQL type / bound |
| --- | --- |
| 所有 row/FK/actor IDs | native `UUID`；仅字段清单明确 nullable 时可 NULL。`actor_id` 为 authenticated principal UUID，不在 reason text 中编码。 |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()`；不进入任何 identity。 |
| fingerprints | `VARCHAR(64)` + lowercase hex CHECK；必须恰为 64 chars。 |
| `idempotency_key` | `VARCHAR(256)`；1..256 Unicode scalar values、UTF-8 最多 1024 bytes，NFC 由 service 在 INSERT 前验证。 |
| `target_ref_key` | `VARCHAR(256)`；existing 固定为 36-char lowercase UUID，new 为 1..256 Unicode scalar values、UTF-8 最多 1024 bytes 且 NFC。 |
| `operation_kind`、status/state/kind、`actor_type` | `VARCHAR(32)` + 各节 closed-enum CHECK；`actor_type` 只允许 `user|service`。 |
| `contract_version`、`method`、`reason_code` | `VARCHAR(64)`；1..64 ASCII chars，token regex `^[a-z0-9][a-z0-9_.:/-]{0,63}$`。 |
| `reason_text` | `VARCHAR(512)`；1..512 Unicode scalar values、UTF-8 最多 2048 bytes。 |
| `request_id` | `VARCHAR(128)`；1..128 printable ASCII chars，禁止 control/whitespace-only value。 |
| `confidence` | `NUMERIC(7,6) NULL` + `0 <= value <= 1`；wire/JCS 规则见 5.1。 |

JSONB shape/default/size 与 collection count 冻结为：

| Column | Null/default | Top-level shape | Service JCS bytes / DB `jsonb::text` bytes | Count |
| --- | --- | --- | --- | --- |
| command `source_identity_snapshot`, `command_scope_snapshot` | non-NULL / no default | object | each <= 262144 | participants <= 1024；split targets <= 256 |
| Decision `successor_detail_snapshot` | always non-NULL / no default；`{}` only for cancelled | object | <= 1048576 | target slots <= 256 |
| Decision `split_partition_snapshot` | non-NULL for split pending/applied/rejected/stale；NULL for non-split or cancelled | array when non-NULL | <= 1048576 | <= 10000 assignments |
| Decision `projection_assignment_snapshot` | always non-NULL；`{}` only for cancelled | array for merge/split normal evaluation；object for reassign normal evaluation；object `{}` for cancelled | <= 1048576 | <= 10000 assignments |
| Decision `evidence_refs` | non-NULL / default `[]` | array | <= 262144 | <= 256 refs |
| successor `target_spec_snapshot` | nullable / no default | object when non-NULL | <= 65536 | n/a |
| assignment `partition_basis_snapshot` | non-NULL / no default | object | <= 65536 | n/a |

Service 在 canonicalization 前按 JCS UTF-8 bytes/count 拒绝超限；数据库以 CHECK 或 constraint trigger
按 stored `jsonb::text` octet length/count 再次拒绝。两种 byte representation 不宣称相等，只分别
承担 API 与持久化资源上限。JSON scalar/string nested depth 还固定最多 32；任何一列超限都在 root
INSERT 前 `REJECTED(payload_too_large)` 且零写入。实现不得未经设计修订扩大/缩小这些边界，也不得
把 nullable/default 留给 ORM 隐式决定。

### 5.1 `canonical_entity_evolution_commands`

每一行是一个 immutable logical evolution command root，不是 Decision。Command root 只
标识一次 evolution intent；可修正的 proposal 内容属于其 Decision versions。一个 command
可以有 D1、D2、D3 多条 Decision，但同一次修正不得创建第二个 root。

Command Identity 是下列 normalized JSON 的 SHA-256，不再使用自然语言判断“是否仍属同一
intent”。所有 operation 均包含 `contract_version`；contract version 改变即为不同 identity。

Canonical JSON 只允许使用 RFC 8785 JSON Canonicalization Scheme (JCS)，不得用数据库
JSONB 输出、语言默认 `json.dumps`/`JSON.stringify` 或自定义近似排序代替。JCS 前的 domain
normalization 固定为：

- parser 拒绝 duplicate object keys、invalid Unicode 和非 I-JSON 值；所有输入 string 必须
  已是 Unicode NFC，否则拒绝，不做静默转换。string escaping、object-key ordering 和 UTF-8
  encoding 完全采用 RFC 8785。
- UUID 使用小写标准连字符格式。`participant_canonical_entity_ids` 按 UUID string 排序；
  `target_refs` 先去重，再按 NFC 后的 `existing:<uuid>` / `new:<target_key>` UTF-8 bytes 排序。
- 数值必须有限并在 I-JSON safe range；negative zero 拒绝。数学等价的 `0`、`0.0` 按 JCS
  统一为 `0`。`confidence` 的持久化类型固定为 `NUMERIC(7,6)`，调用边界只接收 exact decimal、
  最多六位小数，不接收 binary float。
- 声明为 set 的其他 arrays 先按各节 stable key 去重排序；未声明为 set 的 arrays 保留业务
  顺序。`null` 保留。最终 fingerprint 输入是 JCS 生成的 UTF-8 bytes，不带 BOM、不带尾随换行，
  输出 lowercase 64-char SHA-256。
- request parser 必须拒绝带 UTF-8 BOM 的 input bytes；不得先剥离 BOM 再把请求当作等价 replay。

`confidence` 的 wire token 必须由 parser 直接读为 exact base-10 decimal，禁止先落入 IEEE-754
binary float。通过范围与 scale 校验后，token 先去掉无意义尾零：`1.000000 -> 1`、
`0.100000 -> 0.1`、`0.000001 -> 0.000001`、`0|0.0 -> 0`；本字段范围内禁止 exponent form，所得
plain token 与 RFC 8785 ECMAScript number serialization 的 bytes 必须一致，再进入完整对象 JCS。
负零、超过六位小数、超出 `0..1`、NaN/Infinity 或任何 binary-float API value 都在 fingerprint
前拒绝。其他 nested JSON numbers 仍严格走 RFC 8785/I-JSON number contract，不继承
`confidence` 的 `0..1` 业务范围。

Fingerprint 责任边界固定为 **service canonicalizer ownership**。唯一共享 canonicalizer 必须在
JSONB conversion 和任何 INSERT 前完成 domain validation、生成 RFC 8785 exact bytes、计算 hash，
并把 normalized snapshots 与 hash 一起提交。5.1/5.2 golden vectors 是该 canonicalizer 的跨实现
conformance contract。PostgreSQL 只静态保证 hash 的 64-lowercase-hex 格式与 uniqueness、JSON
top-level type，以及可由普通 constraint/constraint trigger 表达的 row-to-row shape/slot
correspondence；目标 schema **不实现 JCS SQL canonicalizer，也不重算或静态证明
`fingerprint == snapshot canonical bytes`**。绕过 service 的 raw SQL 因而不是受支持的写入口，
验收不得把 hash 格式 CHECK 误报成内容一致性保证。若未来要把重算责任下沉数据库，必须先另行
冻结 persisted canonical bytes 或 database canonicalizer，不得在 `0072` 中临时发明算法。

`idempotency_key`、created time、generated row UUID、row insertion order 和 lifecycle status
不进入 fingerprint。下列 pretty JSON 只定义字段 shape；5.1 末尾的 one-line golden bytes 才是
可直接用于互操作测试的输入。

#### Merge identity

```json
{
  "command_scope": {"survivor_canonical_entity_id": "A"},
  "contract_version": "canonical_entity_evolution/v1",
  "library_id": "L",
  "operation_kind": "merge",
  "source_identity": {"participant_canonical_entity_ids": ["A", "B"]}
}
```

`participant_canonical_entity_ids` 是包含 survivor 和全部 losing sources 的 distinct set，按
UUID 排序且至少两个。survivor **属于 command scope**，并且必须出现在 participant set；
`A+B->A` 与 `A+B->B` 是不同 Command Identity，participant 输入顺序不影响 identity。

#### Split identity

```json
{
  "command_scope": {
    "target_refs": [
      {"canonical_entity_id": "B", "kind": "existing"},
      {"canonical_entity_id": "C", "kind": "existing"}
    ]
  },
  "contract_version": "canonical_entity_evolution/v1",
  "library_id": "L",
  "operation_kind": "split",
  "source_identity": {"source_canonical_entity_id": "A"}
}
```

`target_refs` 是至少两个 distinct target slots 的 set，按
`existing:<canonical UUID>` 或 `new:<caller target_key>` 排序。new target 只在 identity 中
持久化稳定 `{"kind":"new","target_key":"..."}`；完整 new-target specification 属于
Decision payload。target slot set 改变就是不同 intent；partition 或 target specification
内容修正不改变 identity。

#### Reassign identity

```json
{
  "command_scope": {
    "from_canonical_entity_id": "A",
    "target_canonical_entity_id": "B"
  },
  "contract_version": "canonical_entity_evolution/v1",
  "library_id": "L",
  "operation_kind": "reassign",
  "source_identity": {"entity_projection_id": "E"}
}
```

standalone reassign 的 projection、from canonical 和 target canonical 都属于 identity；其中
任一项改变都是新 intent。expected observation、reason 和 evidence 仍属于 Decision payload。

Command Identity golden vectors 使用以下固定 ID：

```text
L  = 00000000-0000-4000-8000-000000000001
A  = 10000000-0000-4000-8000-000000000001
B  = 10000000-0000-4000-8000-000000000002
C  = 10000000-0000-4000-8000-000000000003
E1 = 20000000-0000-4000-8000-000000000001
E2 = 20000000-0000-4000-8000-000000000002
D1 = 30000000-0000-4000-8000-000000000001
```

Merge exact bytes（单行，无尾随换行）：

```json
{"command_scope":{"survivor_canonical_entity_id":"10000000-0000-4000-8000-000000000001"},"contract_version":"canonical_entity_evolution/v1","library_id":"00000000-0000-4000-8000-000000000001","operation_kind":"merge","source_identity":{"participant_canonical_entity_ids":["10000000-0000-4000-8000-000000000001","10000000-0000-4000-8000-000000000002"]}}
```

`sha256 = b4b17cdf0050dfc609920f611148d4430ccee579a6803b78c0a6ab6ea3b29cec`

Split exact bytes（单行，无尾随换行）：

```json
{"command_scope":{"target_refs":[{"canonical_entity_id":"10000000-0000-4000-8000-000000000002","kind":"existing"},{"canonical_entity_id":"10000000-0000-4000-8000-000000000003","kind":"existing"}]},"contract_version":"canonical_entity_evolution/v1","library_id":"00000000-0000-4000-8000-000000000001","operation_kind":"split","source_identity":{"source_canonical_entity_id":"10000000-0000-4000-8000-000000000001"}}
```

`sha256 = 6f88900a4a4d7cb9b5a5f3e490e34c205f4ef2acf96cfd953f11e1d568d75465`

Reassign exact bytes（单行，无尾随换行）：

```json
{"command_scope":{"from_canonical_entity_id":"10000000-0000-4000-8000-000000000001","target_canonical_entity_id":"10000000-0000-4000-8000-000000000002"},"contract_version":"canonical_entity_evolution/v1","library_id":"00000000-0000-4000-8000-000000000001","operation_kind":"reassign","source_identity":{"entity_projection_id":"20000000-0000-4000-8000-000000000001"}}
```

`sha256 = 6212d479317b2a466f16f57b146e79b67dc5e537d6d9a024d1d3b6a8c356041f`

三种 Command Identity 明确都不包含：

```text
split projection partition
new-target specification content beyond stable target_key
partition basis
reason or evidence
expected/observed precondition fingerprints
```

| Field | Contract |
| --- | --- |
| `id`, `library_id` | UUID primary ID，另有 `(id, library_id)` unique；library FK `RESTRICT`。 |
| `idempotency_key` | 调用方提供的稳定 logical-command key；必须非空且有界。 |
| `command_identity_fingerprint` | 上述 exact normalized JSON 的 64-char hash。 |
| `operation_kind` | `merge`、`split`、`reassign`；command root 的 immutable semantic kind。 |
| `source_identity_snapshot` | 创建 root 时冻结的 exact `source_identity` object。 |
| `command_scope_snapshot` | 创建 root 时冻结的 exact `command_scope` object。 |
| `contract_version` | 本 command 使用的 evolution/resolver contract version。 |
| `created_at` | 不可变审计时间。 |

数据库必须建立：

```text
UNIQUE (library_id, idempotency_key)
UNIQUE (library_id, command_identity_fingerprint)
UNIQUE (id, library_id)
```

root lookup/admission 顺序冻结为：

1. 在任何 root write 前验证 authentication/authorization、request envelope、RFC 8785 input、
   operation payload shape、显式 library ownership 和静态 cardinality。失败返回对应
   `REJECTED`，command/Decision/child/effect 全部零写入。
2. 做只读 lookup：按 `(library_id, idempotency_key)` 查询；命中且 identity 相同则使用该
   root 并进入 existing-root branch，命中但 identity 不同则
   `REJECTED(idempotency_key_conflict)`。
3. key 未命中时按 `(library_id, command_identity_fingerprint)` 查询；若 identity 已存在，
   返回 `REJECTED(command_identity_alias_key)` 并指明 existing `command_id`，不得保存 alias、
   不得复用 incoming key、不得创建第二 root；也未命中才进入 new-root branch，并只产生
   in-memory root candidate，不得立即 INSERT。
4. 两个 branch 都必须从 persisted command snapshots/incoming Decision payload 或 new candidate
   构造第 9 节完整 lock set；取得锁后重新执行步骤 2-3。若并发 winner 改变 lookup 结果，必须
   切换到重查后的 branch，不能沿用锁前 miss。
5. existing-root branch 在锁后先按 Decision Payload Identity 查询 exact persisted payload，并在
   同一锁内读取 actual current head。exact 命中立即按第 9/10 节返回 `REUSED`，cancellation exact
   命中返回 `CANCELLED`；这一步明确发生在 current-leaf、live-slot、CAS 和 precondition evaluation
   之前，因此 historical applied/superseded payload replay 不会被误报为 stale。exact miss 才校验
   actual-head CAS，并重读 current source/target identities、完整 projection set、live slots 与
   expected/observed precondition；closed head、CAS mismatch 或外部 state drift 按 5.2/10 节返回，
   不创建第二 root。
6. new-root branch 在锁后重读同一组 current identities/projections/live slots。source/target 已非
   request 所指 current leaf、lock set 变化、source slot 被 pending/applied command 占用，或
   projection live slot 被另一个 pending root 占用时，返回
   `STALE_OPERATION(current_identity_changed|lock_set_changed|source_or_projection_slot_occupied)`，
   command/Decision/child/effect 全部零写入。通过这些 admission checks 后仍不得立即 INSERT：先按
   5.2 计算 operation-specific observed precondition，并在 root INSERT **之前**对 proposed
   state-changing edges 做 defensive reachability/integrity check。合法 request 的 source 与 target
   都必须是 current leaves，因此若此时仍发现 target 可到达 source，说明 persisted lineage 已损坏
   或锁后状态不满足 admission；返回
   `STALE_OPERATION(lineage_integrity_error)`，command/Decision/child/effect 全部零写入，禁止保存
   `REJECTED(cycle)` audit root。通过该 defense 后才 INSERT root，比较 expected/observed
   precondition 并执行剩余 semantic policy evaluation：fingerprint mismatch 可以按 matrix
   `none -> stale` 追加 audit-only D1；合法 shape
   但违反非 cycle policy 可以按 `none -> rejected` 追加 audit-only D1；pending/applied 创建对应
   children/effects。也就是说，只有这个 post-root evaluation 层能产生 persisted D1
   `stale|rejected`，但 cycle/integrity failure 明确不属于该层。
7. 并发 unique race 必须在 nested savepoint 回滚后重新查询，再执行完全相同的判定；不得从
   failed SQLAlchemy transaction 返回成功。

因此 matrix 的 `none (D1)` 只描述已通过 root/slot/integrity admission 的新 command；步骤 1-6 中
明确标注为 root INSERT 前的 stale/rejected 不创建 audit-only orphan root。same-root 并发 waiter 在获得
lock 后必须重新 lookup，不能沿用锁前 miss。

new-root stale 的边界固定为两层，禁止实现自行选择：

1. **pre-root admission failure**：authentication/envelope 失败使用相应 `REJECTED`；root re-lookup、完整
   lock-set membership、current source/target leaf、source/projection live slot 或 lineage integrity 失败
   使用相应 `STALE_OPERATION`。两类都在 root INSERT 前返回，command/Decision/child/effect 全部零写入。
2. **post-admission observation stale**：上述 admission 全部通过且 lock set 未变化，但 caller 的 opaque
   expected precondition 与锁后 observation 因其他 snapshot field 不同，则先 INSERT root，再按
   `none -> stale` 追加 audit-only D1；source/successor/assignment/EntityResolutionDecision/Entity effect
   全部为 0。若同一差异同时触发第一层条件，第一层优先，禁止为了留下 audit row 降级 admission failure。

existing-root exact miss 的 precondition mismatch 始终走 5.2 matrix：pending head 为 RETURN-only，其他
eligible head 仅在矩阵允许时追加 audit Decision。所有 persisted audit-only root/Decision 也只是 outer
transaction 中 staged；调用方只有在 commit 成功后才能把其 `command_id`/`decision_id` 声称为 durable。
outer rollback 后必须报告未持久化，不能把 service return 当成 commit proof。

slot vocabulary 不得靠 assignment row 是否仍为 `resolved` 猜测：

- canonical source slot：`sources.resolution_state IN ('pending','applied')` 即被占用。`applied` source
  永久 historical，不能作为另一 root 的新 source；本 root 的 current pending source 只允许通过
  current-head CAS correction/cancellation 接管。
- Entity projection live slot：仅指 assignment 属于其 command **actual current head**，该 head
  `lifecycle_status='pending'`，且 assignment state 为 `resolved|pending`。它只在 pending intent
  期间阻止另一 root；同 root correction/cancellation 可按 CAS 接管。
- parent head 为 `applied|cancelled|superseded` 的 assignment 全部是 immutable history，不占
  projection live slot。因而 standalone `A -> B` applied 后，新的 Command Identity 可基于 Entity
  当前 pointer `B` 执行 `B -> C`；旧 applied assignment 保留且新 command/entity chain 从 NULL
  predecessor 开始，禁止用 cross-command supersede 伪造连续性。

projection live-slot uniqueness 是跨 assignment/Decision 行的 predicate，普通 partial index 无法
表达。目标 schema 的 deferred constraint trigger 必须拒绝 transaction end 时同一
`(library_id, entity_id)` 被两个 current pending heads 占用；runtime safety 仍由 scope-40 advisory
lock、锁后 query 和 CAS 提供，不得把该 trigger 误写成普通静态 UNIQUE。

因此本版明确选择 **拒绝 alias key**。同一 root 的 correction 必须继续使用创建 root 时的
idempotency key。只有 exact normalized JSON 改变时才是新 intent；新 intent 必须使用新 key，
并且涉及同一 source 时还须先满足 5.2 的 pending release gate。已 cancelled root 的相同
Command Identity 也不能换 key 重开；它仍命中 `command_identity_alias_key`。takeover 必须是
真正不同的 normalized identity，或在取消前作为同 root correction 完成。

### 5.2 `canonical_entity_evolution_decisions`

| Field | Contract |
| --- | --- |
| `id`, `library_id` | UUID primary ID，另有 `(id, library_id)` unique；library FK `RESTRICT`。 |
| `command_id` | 非空 root command ID；以 `(command_id, library_id)` composite FK 指向 command。D1、D2、D3 共享同一 Command Identity。 |
| `decision_payload_fingerprint` | 64-char hash，标识本 Decision version 的 normalized immutable payload。 |
| `operation_kind` | `merge`、`split`、`reassign`；必须等于 root operation。`reassign` 只改变一个 Entity projection，不创建 global canonical successor。 |
| `evaluated_outcome` | immutable initial result：`pending`、`applied`、`rejected`、`stale`、`cancelled`。即使 lifecycle 后续为 `superseded`，原评估结果仍可审计。 |
| `lifecycle_status` | 初始等于 `evaluated_outcome`；合法值另含 `superseded`，且只能按下述矩阵转换。 |
| `successor_detail_snapshot` | 本 version 的 normalized survivor/target/target-spec proposal；cancelled control Decision 固定为 `{}`，不得复制旧 proposal。 |
| `split_partition_snapshot` | split normal evaluation 的完整 normalized partition；非 split 或 cancelled 为 NULL。 |
| `projection_assignment_snapshot` | 本 version 的 normalized projection assignment proposal；只有 cancelled control Decision 为 `{}`。 |
| `reason_code`, `reason_text` | 必须说明人工/规则依据；`reason_text` 有界且不可承载秘密或原始大 payload。 |
| `method`, `confidence` | `method` 明确产生来源；`confidence` 为可选 `0..1` 审计值，永远不是 split 自动分流依据。 |
| `evidence_refs` | JSONB array，沿用稳定 reference shape；只保留最小必要证据 locator。 |
| `precondition_fingerprint` | 本 payload 的 expected observation 摘要；它持久化 wire payload 的 `expected_precondition_fingerprint`，二者不是两个不同 token。 |
| `observed_precondition_fingerprint` | evaluation 锁后实际 observation 摘要；`stale` Decision 必须同时保存 expected 与 observed。 |
| `supersedes_decision_id` | 可空的同 command 直接前驱；旧行保留。自引用 composite FK 必须同时携带 `library_id` 和本行 `command_id`，禁止跨 command predecessor。 |
| `actor_type`, `actor_id`, `request_id` | 创建、修正或取消 Decision 的 authenticated caller audit；不得由 reason text 代替。 |
| `created_at` | 不可变审计时间。 |

#### Server-issued precondition token

`precondition_fingerprint`/`expected_precondition_fingerprint` 是 current-state read/prepare API
签发给调用方的 **opaque token**；调用方只能原样 round-trip，不负责构造 snapshot。签发服务与锁后
evaluation 使用同一个 5.1 JCS canonicalizer，对下列 exact object 计算 lowercase SHA-256；锁后值另存
为 `observed_precondition_fingerprint`：

```json
{
  "canonical_states": [
    {
      "canonical_entity_id": "UUID",
      "current_source_resolution_state": null,
      "current_source_transition_id": null,
      "operational_status": "active"
    }
  ],
  "command_context": null,
  "contract_version": "canonical_entity_evolution/v1",
  "graph_governance_live_intents": [],
  "library_id": "UUID",
  "operation_kind": "merge|split|reassign",
  "projection_states": [
    {
      "active_resolution_decisions": [
        {
          "canonical_entity_id": "UUID-or-null",
          "decision_fingerprint": "64-lowercase-hex",
          "decision_id": "UUID",
          "decision_kind": "link_existing|create_new|pending_review|rejected",
          "subject_fingerprint": "64-lowercase-hex"
        }
      ],
      "canonical_entity_id": "UUID",
      "entity_id": "UUID"
    }
  ]
}
```

Exact shape rules：

- `canonical_states` 是 request 引用的完整 canonical set：merge 为全部 participants（含
  survivor）；split 为 source 加全部 existing targets；reassign 为 from 加 target。按 canonical UUID
  排序。每项读取 operational status，以及该 canonical 唯一 non-superseded source transition；没有时
  两个 current-source 字段都为 null，有时 ID/state 必须同时非 null。完整性异常不得签发 token。
- `projection_states` 是本 request 必须 partition/reassign 的完整 Entity set：merge 为所有 losing
  sources 的 direct projections，split 为 source 的全部 direct projections，reassign 恰为指定 Entity。
  按 Entity UUID 排序。每个 `active_resolution_decisions` 包含该 Entity 的**全部** active decisions，
  按 `(subject_fingerprint, decision_id)` 排序；canonical 为 JSON null 时保留 null，禁止过滤后再 hash。
- `graph_governance_live_intents` 只包含触及这些 Entity、`action_kind='entity_merge'` 且 status 为
  `pending_review|approved` 的 GraphGovernance live actions；其他 action kind 不进入本 P3.1 mutex token。
  item exact keys 为 `action_id`、`action_status`、`entity_ids`；actions 按 UUID 排序，`entity_ids`
  固定为该 action survivor/loser 的 distinct UUID set 并按 UUID 排序。没有 matching intent 时必须是
  `[]`。
- new-root token 的 `command_context` 必须为 null；correction token 的 exact object 为
  `{"command_id":"UUID","current_decision_id":"UUID"}`。CAS ID 仍须作为独立 request field，不能只靠
  token 隐含。Cancellation 不另签发 prepare token；cancelled Decision 的
  `precondition_fingerprint`（即 payload `expected_precondition_fingerprint`）固定复制 pending predecessor
  的 `observed_precondition_fingerprint`，并以 `expected_pending_decision_id` 执行独立 CAS。取得完整锁并
  确认该 pending head/proposal chain 后，service 仍按本节 exact snapshot fresh 重算 cancelled Decision 的
  `observed_precondition_fingerprint`，其中 `command_context` 固定为
  `{"command_id":"UUID","current_decision_id":"pending-predecessor-UUID"}`。expected 与 fresh observed
  **不做 equality gate**：command context 本来就可能不同，取消的 admission 只依赖鉴权、original key、
  actual-head CAS、完整 lock set 及 chain/proposal integrity。无法证明 lock set 或完整性时零写入失败；
  单纯 hash 不同不得阻止这个 no-effect release。
- 所有 objects 禁止 extra key，UUID/string/number 规则继承 5.1。签发后任何上述 membership、pointer、
  Decision、lineage、operational status、live intent 或 current command head 改变，锁后 observed hash
  都必须不同。exact replay 仍按第 10 节先于 token comparison。

API 只暴露 token，不需要暴露整个 snapshot；实现和测试必须用固定 repository fixture 对 merge、
split、reassign 各做 `prepare -> unchanged round-trip succeeds -> mutate one listed field -> stale`。
不得用更新时间、row order、latest record、未列入字段或 caller 自报 JSON 参与 token。

Decision Payload Identity 是下列 normalized JSON 的 SHA-256；它使用 5.1 的同一 RFC 8785
pipeline 和 target refs，不能以数据库生成 ID 替换 caller `target_key` 后重新计算：

```json
{
  "confidence": null,
  "evidence_refs": [],
  "expected_precondition_fingerprint": "64-lowercase-hex",
  "method": "manual_or_named_rule",
  "operation_payload": {},
  "reason_code": "bounded_code",
  "reason_text": "bounded_text"
}
```

- merge `operation_payload` 的 exact keys 是
  `projection_assignments`、`survivor_canonical_entity_id`。每个 assignment 的 exact keys 是
  `entity_id`、`from_canonical_entity_id`、`partition_basis_snapshot`、`reason_code`、`state`、
  `target_canonical_entity_id`；`state` 只能为 `resolved`，每个 direct losing projection 显式
  出现一次，按 Entity UUID 排序。
- split `operation_payload` 的 exact keys 是 `projection_assignments`、
  `target_specifications`。每个 assignment 的 exact keys 是 `entity_id`、
  `from_canonical_entity_id`、`partition_basis_snapshot`、`reason_code`、`state`、`target_ref`；
  `state` 只能为 `resolved|pending`。resolved 的 `target_ref` 必须是 5.1 的 exact existing/new
  shape，pending 的 `target_ref` 必须为 null。assignments 按 Entity UUID 排序。
- `target_specifications` 只包含 command scope 中 `kind=new` 的 slots；每项 exact keys 是
  `canonical_name`、`normalized_name`、`status`、`target_key`，其中 status 必须为 `active`。
  existing slots 不在此数组重复，数组按 NFC `target_key` UTF-8 bytes 排序；其 target-key set
  必须与 Command Identity 中所有 new target keys 完全相等。全 existing target 时必须是 `[]`。
- reassign `operation_payload` 只有 `projection_assignment`；其 exact keys 是 `entity_id`、
  `from_canonical_entity_id`、`partition_basis_snapshot`、`reason_code`、
  `target_canonical_entity_id`，且 Entity 必须与 Command Identity 的 projection 相同。
- cancellation control `operation_payload` 的 exact keys 是 `control_kind` 和
  `expected_pending_decision_id`；前者固定为 `cancel_pending`，后者必须等于 request CAS token
  和新 cancellation Decision 的 direct predecessor。它不含 projection/source/successor payload。

上述 v1 objects 禁止额外 keys。`partition_basis_snapshot` 必须是有界 canonical JSON object；
它可以为空，但不得承载 raw document 或秘密。

`evidence_refs` 是 set，按每个元素的 JCS bytes 排序去重；其余未声明为 set 的 nested arrays
保留业务顺序。observed precondition、时间、random/generated UUID、row insertion order、
lifecycle status、actor/request audit 和 new EntityResolutionDecision ID 不进入 payload
fingerprint。cancellation 的 common envelope 固定
`method=authorized_cancellation, confidence=null`；其 expected precondition 与 fresh observed 值分别按
上文 cancellation 特例持久化，reason/evidence 来自已鉴权请求。

Decision Payload golden vectors 均使用 5.1 的固定 IDs 和
`expected_precondition_fingerprint = "aaaaaaaa..."`（64 个 `a`）。以下每个 code block 都是
exact one-line UTF-8 bytes，无尾随换行。

Merge Decision：

```json
{"confidence":null,"evidence_refs":[],"expected_precondition_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","method":"manual","operation_payload":{"projection_assignments":[{"entity_id":"20000000-0000-4000-8000-000000000001","from_canonical_entity_id":"10000000-0000-4000-8000-000000000002","partition_basis_snapshot":{},"reason_code":"merge_survivor","state":"resolved","target_canonical_entity_id":"10000000-0000-4000-8000-000000000001"}],"survivor_canonical_entity_id":"10000000-0000-4000-8000-000000000001"},"reason_code":"manual_merge","reason_text":"merge B into A"}
```

`sha256 = da1de0e83a1ec3e95585b963c5c11983f0a6479460c097a8ab7f8653e3f2dd16`

Pending Split Decision：

```json
{"confidence":null,"evidence_refs":[],"expected_precondition_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","method":"manual","operation_payload":{"projection_assignments":[{"entity_id":"20000000-0000-4000-8000-000000000001","from_canonical_entity_id":"10000000-0000-4000-8000-000000000001","partition_basis_snapshot":{},"reason_code":"manual_partition","state":"resolved","target_ref":{"canonical_entity_id":"10000000-0000-4000-8000-000000000002","kind":"existing"}},{"entity_id":"20000000-0000-4000-8000-000000000002","from_canonical_entity_id":"10000000-0000-4000-8000-000000000001","partition_basis_snapshot":{},"reason_code":"needs_review","state":"pending","target_ref":null}],"target_specifications":[]},"reason_code":"manual_split","reason_text":"partition A into B and C"}
```

`sha256 = 8fe4b18375e32787ab7db770736ac14fefde2db9175b753b7c02cd82f82ac6e1`

Reassign Decision：

```json
{"confidence":null,"evidence_refs":[],"expected_precondition_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","method":"manual","operation_payload":{"projection_assignment":{"entity_id":"20000000-0000-4000-8000-000000000001","from_canonical_entity_id":"10000000-0000-4000-8000-000000000001","partition_basis_snapshot":{},"reason_code":"manual_reassign","target_canonical_entity_id":"10000000-0000-4000-8000-000000000002"}},"reason_code":"manual_reassign","reason_text":"move E1 from A to B"}
```

`sha256 = 1885d59500b9127daa44aba559f36e8a155edc049a5aedfc9390350f508a7320`

Cancellation Decision：

```json
{"confidence":null,"evidence_refs":[],"expected_precondition_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","method":"authorized_cancellation","operation_payload":{"control_kind":"cancel_pending","expected_pending_decision_id":"30000000-0000-4000-8000-000000000001"},"reason_code":"incorrect_pending_intent","reason_text":"cancel incorrect pending split"}
```

`sha256 = 17a8d1230e96fc15dea7bb9b6e816583c0c99f7e611bc2fb7d7e4ff790953564`

Non-zero exact-decimal Decision serialization vector（与上述 Reassign Decision 相同，仅
`confidence` 的 domain value 为 exact decimal `0.123456`）：

```json
{"confidence":0.123456,"evidence_refs":[],"expected_precondition_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","method":"manual","operation_payload":{"projection_assignment":{"entity_id":"20000000-0000-4000-8000-000000000001","from_canonical_entity_id":"10000000-0000-4000-8000-000000000001","partition_basis_snapshot":{},"reason_code":"manual_reassign","target_canonical_entity_id":"10000000-0000-4000-8000-000000000002"}},"reason_code":"manual_reassign","reason_text":"move E1 from A to B"}
```

`sha256 = 5ad0bad254764e45a7798de3af127ee73ecc69bd01eb855abbf926a10d25c18c`

correction request 必须携带 `expected_predecessor_decision_id` 作为 CAS token；锁后 current
head 不等即 `STALE_OPERATION(expected_predecessor_mismatch)` 且零写入，不能因 advisory lock
已经串行化就静默取代另一个 correction。

同 payload replay 在进入 lifecycle matrix 前处理。命中 current 或 historical Decision 都
返回 9 节定义的 `REUSED`；命中 historical `superseded` payload 只复用其原 outcome，并同时
报告 actual current head，绝不复活旧 proposal。cancellation replay 例外：它返回幂等
`CANCELLED`，因为 `REUSED.effective_outcome` 刻意不扩展为 cancellation control outcome。

完整的 current predecessor status x evaluated new result 矩阵如下。`APPEND` 都表示新行以
`supersedes_decision_id` 指向 current head；除矩阵明确写出的转换外一律禁止。New
`superseded` 列特意列出并全部非法，因为它不是 evaluated result：

| Current predecessor | New `pending` | New `applied` | New `rejected` | New `stale` | New `cancelled` | New `superseded` |
| --- | --- | --- | --- | --- | --- | --- |
| none (D1) | APPEND D1 + pending proposal rows | APPEND D1 + atomic effects | APPEND audit-only D1 | APPEND audit-only D1 | ILLEGAL: nothing to cancel | ILLEGAL: never an initial result |
| `pending` | APPEND；old Decision/source/assignment `pending|resolved -> superseded` | APPEND；释放 old pending 后写 applied lineage/effects | RETURN only；按第 9 节返回且零写入，old pending 保持 current | RETURN only；按第 9 节返回且零写入，old pending 保持 current | APPEND cancellation Decision；old pending proposal 全部进入 `superseded` | ILLEGAL: never an initial result |
| `rejected` | APPEND pending proposal；old Decision `rejected -> superseded` | APPEND applied effects；old Decision `rejected -> superseded` | APPEND audit-only；old Decision `rejected -> superseded` | APPEND audit-only；old Decision `rejected -> superseded` | ILLEGAL: no pending intent to release | ILLEGAL: never an initial result |
| `stale` | APPEND pending proposal；old Decision `stale -> superseded` | APPEND applied effects；old Decision `stale -> superseded` | APPEND audit-only；old Decision `stale -> superseded` | APPEND audit-only；old Decision `stale -> superseded` | ILLEGAL: no pending intent to release | ILLEGAL: never an initial result |
| `applied` | ILLEGAL: command closed | ILLEGAL: command closed | ILLEGAL: command closed | ILLEGAL: command closed | ILLEGAL: command closed | ILLEGAL: command closed |
| `cancelled` | ILLEGAL: command closed | ILLEGAL: command closed | ILLEGAL: command closed | ILLEGAL: command closed | ILLEGAL: command closed | ILLEGAL: command closed |
| `superseded` | ILLEGAL: never a current head；reload actual head | ILLEGAL | ILLEGAL | ILLEGAL | ILLEGAL | ILLEGAL |

Matrix admission rules：

- `pending`、`rejected`、`stale` 可以是 current head 且 correction-eligible；它们不是 closed
  command。只有 accepted direct successor 在同一 transaction 成功插入时，原 predecessor
  才能原地变为 `superseded`。
- `applied`、`cancelled` 是 closed-command heads：不得追加 successor，也不得原地转换。
  applied 之后的变化必须基于 current leaves 创建新的 Command Identity。
- `superseded` 是 historical row terminal：它已经恰有一个 direct successor，永远不能再次作为
  predecessor、current head 或 evaluated outcome，也不能原地转换。
- pending correction 若 evaluation 得到 `REJECTED` 或 `STALE_OPERATION`，不得写 Decision、
  source、successor 或 assignment，也不得释放原 pending。若未来要求持久化这类 failed
  attempt，必须另行设计 attempt audit，不能把它伪装成 successor。
- `superseded` 不能作为新行初始状态；它只能由本矩阵的 predecessor transition 产生。
- 请求 envelope、权限、library scope 在 matrix admission 前校验；未通过时零 lineage 写入。

```text
command CMD1
  identity: split A -> target slots {B, C}
  D1: pending, partition = {e1 -> B, e2 -> pending}
  D2: applied, partition = {e1 -> B, e2 -> C}
      command_id = CMD1, supersedes_decision_id = D1
      expected_predecessor_decision_id = D1
  D1.lifecycle_status: pending -> superseded
```

D1 与 D2 始终保持两个 targets；只有 partition payload 改变，所以 D2 是同一 root 的合法
correction。若 D2 仍为 pending，D3 使用同一规则。D1 的 payload、reason、evidence、source
identity 和 assignment content 不得修改或删除；只允许 D1 lifecycle、其 pending source 和
该 proposal 的全部 `resolved|pending` assignments 在同一 transaction 进入 `superseded`。

数据库还必须建立以下可静态执行的 chain 约束：

```text
UNIQUE (id, library_id, command_id)
UNIQUE (library_id, command_id, decision_payload_fingerprint)
FK (command_id, library_id) -> commands(id, library_id)
FK (supersedes_decision_id, library_id, command_id)
  -> decisions(id, library_id, command_id) NOT DEFERRABLE
PARTIAL UNIQUE INDEX (library_id, command_id)
  WHERE supersedes_decision_id IS NULL
PARTIAL UNIQUE INDEX (library_id, supersedes_decision_id)
  WHERE supersedes_decision_id IS NOT NULL
CHECK (supersedes_decision_id IS NULL OR supersedes_decision_id <> id)
PARTIAL UNIQUE INDEX (library_id, command_id)
  WHERE lifecycle_status = 'pending'
PARTIAL UNIQUE INDEX (library_id, command_id)
  WHERE lifecycle_status = 'applied'
```

这些约束加上 5.6 的 current-head constraint trigger，保证一个 command 只有一个 chain root、
每个 predecessor 最多一个 direct successor，并禁止向非 head、跨 command 或 self predecessor
追加。`supersedes_decision_id` 插入后不可变；`NOT DEFERRABLE` FK 可阻止跨 scope 和逐条 forward
reference，但 PostgreSQL 在一条 multi-row statement 结束时检查 immediate FK，不能单独排除
mutual cycle/rootless chain。因此“恰一 root、全链可达且无环、恰一 head”必须由 5.6 的 deferred
whole-chain trigger 在 transaction end 证明。current head 按“没有 successor row”确定，禁止用
latest/first row 推断。

#### Pending intent cancellation and release

本版选择显式 `cancelled` Decision，不允许静默覆盖或跨 command supersede。Cancellation 是
existing-root-only control path，绝不能调用 5.1 的 root create branch。只有已通过现有
application authorization boundary、具备该 library evolution-write 权限的 authenticated
caller 可以取消；P3.1 不新造角色系统。取消请求必须提供：

```text
command_id
original idempotency_key
expected_pending_decision_id
actor_type + actor_id
request_id
reason_code + bounded reason_text
evidence_refs optional; default []
```

鉴权在暴露 root existence 前完成。之后必须按 `(library_id, command_id)` 找到 root 并 constant-
time 比较 `original idempotency_key`；不存在分别返回 `REJECTED(command_not_found)` 或
`REJECTED(cancellation_root_key_mismatch)`，command/Decision 均零写入。取消 payload 按 5.2
计算 fingerprint；exact committed cancellation replay 返回
`CANCELLED(cancel_already_committed)`，`decision_id` 指向既有 cancelled Decision，所有表零写入。
非 exact replay 仍受 current-head CAS 和 closed-command rules 约束。

取消必须独占一个 outer transaction：取得完整且有序的 canonical/entity-projection/
resolution-subject advisory locks，`FOR UPDATE` 重读 command current head、live source rows、
proposal assignments、Entities 和 active EntityResolutionDecisions，并验证 expected pending ID。
验证成功后追加 `evaluated_outcome=cancelled, lifecycle_status=cancelled` 的 Decision，令其
`supersedes_decision_id` 指向旧 pending，再将旧 pending Decision、live source transitions 和
该 proposal 的全部 `resolved|pending` assignments 标记为 `superseded`。取消 Decision 必须没有
任何新 source、successor 或 assignment child rows；取消不得更新 Entity、不得 supersede
EntityResolutionDecision、不得改写任何历史 bridge。

CAS 失配、权限失败或任一步异常都整笔回滚，旧 pending 和 source slot 保持不变。新 intent
只有在 cancellation outer transaction 已成功 commit 后，才能以不同 normalized Command
Identity 和新 idempotency key 在后续 transaction 竞争 source slot；不得把取消与新 command
伪装成跨 root supersede，也不提供一个绕过已提交取消的隐式“恢复”操作。

### 5.3 `canonical_entity_evolution_sources`

每一行代表一个 proposed 或 applied canonical predecessor transition；`pending` 期间原 identity
仍承载既有 Entity pointers，但 new-resolution 读取必须返回 pending，只有 `applied` 后才不再是
current leaf：

```text
id
library_id
command_id
evolution_decision_id
source_canonical_entity_id
supersedes_source_transition_id nullable
resolution_state = pending | applied | superseded | historical_only
created_at
```

`command_id` 是有意冗余，用于让 PostgreSQL composite FK 静态证明 predecessor 与 successor
属于同 command；一致性由下列 FK 负责，不由 service 猜测。`resolution_state` 是 resolver
对该 source 的唯一权威状态，而 Decision header 不是第二个 current-pointer。

必须建立：

```text
PRIMARY KEY (id)
UNIQUE (id, library_id, command_id, source_canonical_entity_id)
UNIQUE (id, library_id, command_id, evolution_decision_id)
FK (evolution_decision_id, library_id, command_id)
  -> decisions(id, library_id, command_id)
FK (source_canonical_entity_id, library_id)
  -> canonical_entities(id, library_id)
FK (supersedes_source_transition_id, library_id, command_id,
    source_canonical_entity_id)
  -> sources(id, library_id, command_id, source_canonical_entity_id)
  NOT DEFERRABLE
UNIQUE (library_id, evolution_decision_id, source_canonical_entity_id)
PARTIAL UNIQUE INDEX (library_id, command_id, source_canonical_entity_id)
  WHERE supersedes_source_transition_id IS NULL
PARTIAL UNIQUE INDEX (library_id, supersedes_source_transition_id)
  WHERE supersedes_source_transition_id IS NOT NULL
CHECK (supersedes_source_transition_id IS NULL OR supersedes_source_transition_id <> id)
CHECK (resolution_state IN ('pending', 'applied', 'superseded', 'historical_only'))
PARTIAL UNIQUE INDEX (library_id, source_canonical_entity_id)
  WHERE resolution_state IN ('pending', 'applied')
```

`id` 是 source row 的全局 UUID 单列 primary key；后续 composite UNIQUE 只为 scoped FK 提供
可执行 referenced key，不得把 ORM 自行改成 composite primary key。因此一个 historical canonical
不可能同时拥有两条 current/pending evolution 分支。
correction 必须为新 Decision 追加完整 source transition，并指向同 command、同 source 的
直接上一 proposal。5.6 trigger 还要拒绝跨 command、跨 source、非 current predecessor 和
branching predecessor；`supersedes_source_transition_id` 插入后不可变，旧 source identity 与
其他 payload 不得覆盖或删除。

`historical_only` 只能是显式 persisted terminal evolution state：该 source 已被声明为不再是
current leaf，且没有 successor。P3.1 的 merge、split 与 reassign command 不创建该 terminal
state；缺少这种 persisted state 时，resolver 不得从 CanonicalEntity operational status 或
"没有找到 successor" 猜出 `historical_only`。

本版不授权能产生该语义的 `retire` operation，因此 target P3.1 insert trigger 必须拒绝三个
现有 operation 创建 `resolution_state='historical_only'`。该枚举只保留 resolver 的显式读取
契约；只有未来单独冻结 operation、parent Decision outcome 和创建约束的 migration 才能开放
writer。旧 `0071` 非空数据又被 5.0 fail-closed gate 拒绝，所以 `0072` 不存在需要猜测接纳的
legacy historical-only row。

因此 P3.1 对该状态的验证明确拆成两项：PostgreSQL 负例证明现有三种 writer 不能创建它；
resolver read-path 使用绕过 target-schema write triggers 的 repository test double，构造
persisted-row-shaped future/corruption fixture 来验证返回 vocabulary。该 fixture 不是合法 SQL
producer、不是 production backdoor，也不证明当前 target schema 能写入此状态；它只算
forward-compatibility unit contract，不得报告为 P3.1 end-to-end persisted writer PASS。

### 5.4 `canonical_entity_evolution_successors`

```text
id
library_id
command_id
evolution_decision_id
source_transition_id
target_ref_kind = existing | new
target_ref_key
target_canonical_entity_id nullable
target_spec_snapshot nullable
created_at
```

`command_id` 和 `evolution_decision_id` 是有意冗余，使 PostgreSQL 能静态证明 successor slot、
source transition 与 Decision 属于同一 command/version。`target_ref_key` 对 existing slot 是
canonical UUID string，对 new slot是 caller 的 NFC `target_key`；它与 `target_ref_kind` 一起是
持久化 slot identity，不从 target spec 或数据库生成 UUID 反推。

必须建立：

```text
PRIMARY KEY (id)
UNIQUE (id, library_id)
UNIQUE (id, library_id, command_id, evolution_decision_id)
FK (evolution_decision_id, library_id, command_id)
  -> decisions(id, library_id, command_id)
FK (source_transition_id, library_id, command_id, evolution_decision_id)
  -> sources(id, library_id, command_id, evolution_decision_id)
  NOT DEFERRABLE
FK (target_canonical_entity_id, library_id)
  -> canonical_entities(id, library_id)
UNIQUE (library_id, source_transition_id, target_ref_kind, target_ref_key)
CHECK (target_ref_kind IN ('existing', 'new'))
CHECK (
  (target_ref_kind = 'existing'
    AND target_canonical_entity_id IS NOT NULL
    AND target_spec_snapshot IS NULL)
  OR
  (target_ref_kind = 'new' AND target_spec_snapshot IS NOT NULL)
)
```

constraint trigger 还必须验证 existing key 等于 target UUID、new key/spec 等于 Decision 和
Command Identity 中的 stable slot，并禁止一个 source transition 漏掉或增加 command target
slots。merge 的每个 losing source 恰有一个 successor；split 的一个 source 恰有 Command
Identity 中至少两个 distinct target slots。顺序只用于 hashing 和 lock construction，永远不
表达优先级。applied split 的所有 successor `target_canonical_entity_id` 还必须非空且两两不同；
两个 distinct slots 不得映射到同一个 CanonicalEntity UUID。

对于 fully resolved split，显式 new-target specification 在 transaction 内创建
CanonicalEntity 后写入 `target_canonical_entity_id`。对于 pending split，new slot 保持 target
UUID 为 NULL、保留 immutable target specification；resolved projection 通过 assignment 的
`target_successor_id` 指向该 slot，不能因 UUID 尚未生成而丢失 partition 关系。pending source
不形成 applied successor relation。successor proposal 归属于本 Decision version；correction
追加新 version rows，旧 rows 随其 source transition 保留，禁止原地改 slot/target/specification。
该 parent-state/target-nullability 关系由 deferred constraint trigger 明确强制：parent Decision 或
source 为 pending 时，每个 `target_ref_kind='new'` row 的 target UUID 必须为 NULL；parent
Decision/source 为 applied 时，每个 successor（existing 或 new）的 target UUID 必须非 NULL。
pending row 携带 non-NULL new-target UUID 或 applied row 遗漏 UUID，均不是普通 CHECK 已覆盖的静态
保证，必须在 COMMIT 由 trigger 拒绝。一个没有 backlink 的新 CanonicalEntity row 无法静态归因给
某个 pending slot，因此数据库不得以该 trigger 禁止同 transaction 的无关 CanonicalEntity INSERT。
“pending split 不创建 orphan target”明确是锁后 service + outer-transaction invariant：service 只有在
fully resolved applied branch 才能 INSERT target，且必须以 target row-count、rollback 和
pending/applied 配对测试证明；不得再把这项声称为 FK、CHECK 或 constraint-trigger guarantee。

`source != target` **不是普通 SQL CHECK 可表达的静态保证**，因为 source 位于被引用的
transition row。目标 schema 采用 PostgreSQL `DEFERRABLE INITIALLY DEFERRED` constraint
trigger。精确谓词是：当且仅当 parent source
`resolution_state='applied'`，且 parent Decision 同时满足
`evaluated_outcome='applied' AND lifecycle_status='applied'`，successor 才是 state-changing
edge；此时 trigger 强制 target UUID 非空、target 与 source 同 library 且 UUID 不同。pending
proposal 即使已有 existing target 也不是 applied edge；pending self-target 由锁后 service
validation 拒绝。普通 CHECK 不得被文档或验收报告误称为已经保证此跨行 invariant。

### 5.5 `canonical_entity_projection_assignments`

```text
id
library_id
command_id
evolution_decision_id
entity_id
supersedes_assignment_id nullable
from_canonical_entity_id
target_successor_id nullable
target_canonical_entity_id nullable
assignment_state = resolved | pending | superseded
partition_basis_snapshot
reason_code
created_at
```

该表是 Entity projection current assignment 的 append-only audit，不是 historical
Fact reconciliation。它对 Entity 及 from/target CanonicalEntity 的引用必须使用各自
`(id, library_id)` composite FK 保证同库；new EntityResolutionDecision 对 assignment 的反向引用使用
下文精确三列 FK。P3.1 migration 还必须保留 `entity_resolution_decisions` 已有
`(id, library_id)` unique；目标 `0072` 另为该表增加 nullable
`evolution_assignment_id`，使一条 assignment 可被零到多条 replacement Decision 引用。该列只供
P3 applied replacement 使用，pending assignment 下必须没有引用它的 Decision。

每个 applied reassignment 都有完整的 1:N old/new Decision pairs；pair identity 由 new
EntityResolutionDecision 的 `evolution_assignment_id` 与 `supersedes_decision_id` 共同表达，不在
assignment 上保存任意一组 scalar previous/new IDs。`pending` projection 的
`target_successor_id`、target canonical 均为 NULL，且不得创建 replacement Decision。merge/split 中一个 resolved
projection 必须用 `target_successor_id` 指向本 Decision version 的 persisted slot；因此 pending
split 可以表示“已确定分到尚未创建的 new target”。standalone reassign 不建立 global successor，
其 `target_successor_id` 必须为 NULL。一个 projection 在同一 decision 中只能有一条
assignment。`command_id` 同样是有意冗余，并必须建立：

```text
PRIMARY KEY (id)
UNIQUE (id, library_id, entity_id)
UNIQUE (id, library_id, command_id, entity_id)
FK (evolution_decision_id, library_id, command_id)
  -> decisions(id, library_id, command_id)
FK (entity_id, library_id)
  -> entities(id, library_id) NOT DEFERRABLE
FK (from_canonical_entity_id, library_id)
  -> canonical_entities(id, library_id) NOT DEFERRABLE
FK (target_canonical_entity_id, library_id)
  -> canonical_entities(id, library_id) MATCH SIMPLE NOT DEFERRABLE
FK (supersedes_assignment_id, library_id, command_id, entity_id)
  -> assignments(id, library_id, command_id, entity_id)
  NOT DEFERRABLE
FK (target_successor_id, library_id, command_id, evolution_decision_id)
  -> successors(id, library_id, command_id, evolution_decision_id)
  NOT DEFERRABLE
UNIQUE (library_id, evolution_decision_id, entity_id)
PARTIAL UNIQUE INDEX (library_id, command_id, entity_id)
  WHERE supersedes_assignment_id IS NULL
PARTIAL UNIQUE INDEX (library_id, supersedes_assignment_id)
  WHERE supersedes_assignment_id IS NOT NULL
CHECK (supersedes_assignment_id IS NULL OR supersedes_assignment_id <> id)
CHECK (assignment_state IN ('resolved', 'pending', 'superseded'))
```

`id` 是 assignment row 的全局 UUID 单列 primary key；`UNIQUE (id, library_id, entity_id)` 是
`entity_resolution_decisions.evolution_assignment_id` 三列 FK 的精确 referenced key，四列 unique
不能替代它。其余 composite UNIQUE 只服务 scoped FK，不改变 row identity。这使同 command、同
Entity projection 的直接 predecessor 可由 composite FK 执行；
`supersedes_assignment_id` 插入后不可变。每个 accepted correction 必须重新提交锁后观察到的
完整 projection partition：已在本 command 任一旧 Decision 出现过的 Entity，新 row 必须指向
该 command/entity 没有 successor 的最近 assignment；只有此前从未在本 command 出现的 Entity
才允许建立一个 NULL predecessor root。5.6 trigger 还必须拒绝跨 projection、非 current
predecessor、第二 NULL root 和 branching predecessor。

caller observation 后若 direct projection set、任一 Entity pointer 或 active
EntityResolutionDecision 在锁后发生变化，本次请求必须
`STALE_OPERATION(precondition_mismatch)` 且 command/Decision/proposal/effect 零写入，旧 pending
保持 current；不得把新 projection 静默加进 payload。调用方重新读取后，可以在同 root、同
current-head CAS 下显式提交覆盖新完整 set 的 correction；其中真正首次出现的 Entity 才使用
NULL assignment predecessor。applied assignment 的 target、basis、reason 与 ER Decision links
永远不可变。

上述 assignment 基础 FK 只分别证明同库引用；nullable target 使用 `MATCH SIMPLE`，NULL 不被
误解为一个跨库 target。`entity_resolution_decisions` 的 target schema 还必须增加：

```text
evolution_assignment_id nullable
FK (evolution_assignment_id, library_id, entity_id)
  -> assignments(id, library_id, entity_id) MATCH SIMPLE NOT DEFERRABLE
CHECK (
  evolution_assignment_id IS NULL
  OR (decision_kind = 'link_existing'
      AND entity_id IS NOT NULL
      AND canonical_entity_id IS NOT NULL
      AND supersedes_decision_id IS NOT NULL)
)
PARTIAL UNIQUE INDEX (library_id, supersedes_decision_id)
  WHERE evolution_assignment_id IS NOT NULL
```

5.6 deferred pairing trigger 必须在 parent Evolution Decision applied 时按 **set** 证明：锁后观察到
的 assignment Entity 全部 active canonical-bearing supporting Decisions 恰好各有一个 new
replacement；不得只选择一条。每一 pair 的 previous/new `entity_id` 与 `subject_fingerprint` 相同；
previous canonical 等于 assignment `from_canonical_entity_id` 且在本 transaction 由 active 进入
superseded；new Decision 的 `evolution_assignment_id` 指向本 assignment，直接 supersede previous，
保持 active `link_existing` 且 canonical 等于 assignment target。new 还必须逐字段复制 previous 的
`graph_entity_candidate_id`、`observed_name`、`observed_normalized_name`、`observed_type_key`、
`identifier_snapshot`、`candidate_snapshot` 和 `evidence_refs`，只允许 canonical、decision fingerprint、
method/reason、supersedes link、assignment link、lifecycle 与 created time 按本 contract 变化。Entity 的
OLD/NEW pointer 分别等于 assignment from/target。普通 composite FK 本身不得被描述为已经证明这些
跨行状态与 snapshot 相等。

对 projection `E` 从 `A` reassignment 时，supporting set 冻结为锁后所有满足
`library_id=L AND entity_id=E AND lifecycle_status='active' AND canonical_entity_id=A AND
decision_kind IN ('link_existing','create_new')` 的 EntityResolutionDecision。该 set 必须非空；同一
Entity 只要还存在 canonical 非 NULL、active、但不指向 `A` 的 Decision，就说明 projection/subject
状态不一致，本次结果必须 `PENDING(entity_resolution_state_inconsistent)` 且不更新任何 pointer。
active `pending_review|rejected` Decision 的 canonical 按既有 CHECK 为 NULL，不属于 supporting set，
也不由 evolution 猜测或终结。supporting set 的 UUID/subject input order 不表达优先级；所有成员必须
在同一 transaction 被一一 replacement，少一条、多一条或跨 subject replacement 都整笔拒绝。

### 5.6 Schema invariants

未来获准的 migration 必须用普通 PostgreSQL constraints 静态保证：

```text
all FKs owned by the five P3 evolution audit tables are library-scoped
all persisted fingerprint fields have lowercase 64-char hex format
confidence is NULL or exact NUMERIC(7,6) in 0..1
JSON evidence/basis columns have their required top-level array/object type
current/pending source transition is partial-unique
one library + idempotency key has at most one command root
one stored command_identity_fingerprint has at most one command root per library
all Decision rows reference one persisted command root
one command + stored decision_payload_fingerprint has at most one Decision version
one command has no parallel pending chain and no parallel applied terminal Decision
Decision/source/assignment chains have at most one NULL root and no branching direct successor
source/assignment predecessor composite FKs bind the same command and source/projection
```

上句不覆盖既有 `EntityResolutionDecision.supersedes_decision_id` 的 legacy single-column self-FK，
不得把它误报为 library-scoped composite FK。对 P3 创建的 replacement pair，同库关系由 new
Decision 的 `(evolution_assignment_id, library_id, entity_id)` composite FK、assignment 对 Entity/
CanonicalEntity 的 scoped FKs，以及 deferred pairing trigger 对 previous/new/assignment 三方
`library_id` 相等的检查共同证明；legacy ER predecessor 本身的 schema contract 不在 P3.1 中改写。

下列跨行或 OLD/NEW invariants 不能由普通 SQL CHECK 完整表达，目标 schema 明确使用
PostgreSQL constraint/immutability triggers；service 同时前置验证以返回稳定错误：

```text
source != target for state-changing successor rows
predecessor is the current chain head and belongs to the expected parent Decision
each touched Decision/source/assignment chain has exactly one root and one head,
and every row reaches that root without revisiting a row
exact Command/Decision JSON shape and successor target-slot correspondence
assignment state/target-slot/target-canonical/ER-Decision pairing for each operation and outcome
every applied assignment has one non-empty, complete 1:N supporting-subject replacement set
new command root has exactly one initial Decision chain root by transaction end
only the lifecycle/update whitelist below is legal
DELETE of evolution command/decision/source/successor/assignment rows is forbidden
applied merge/split successor cardinality and split minimum target count
```

这里的 exact JSON shape 只指 closed key set、JSON types、operation-specific cardinality 和
snapshot/slot correspondence；它不包含 RFC 8785 bytes 或 fingerprint 重算。后两项严格属于 5.1
冻结的 service invariant。数据库能拒绝 fingerprint 格式错误或 JSON/slot shape 不一致的行，但不能证明
一个格式正确的 caller-supplied hash 是 snapshot 的真实 SHA-256。

append-only 的 trigger whitelist 冻结为：

- command、successor：禁止任何 UPDATE/DELETE。
- Decision：payload、FK、evaluated outcome、actor 和 timestamps 不可变；仅 current
  `pending|rejected|stale -> superseded`，且必须与一个合法 direct successor 同 transaction。
  `applied|cancelled|superseded` 不可变。
- source：除 current `pending -> superseded` 且 parent pending Decision 同时被取代外，禁止
  UPDATE；禁止 DELETE。`applied|historical_only|superseded` 不可变。
- assignment：除未 applied 的 current proposal `pending|resolved -> superseded` 且
  parent pending Decision 同时被取代外，禁止 UPDATE；applied assignment 不可变且其 replacement
  Decision set 在 outer commit 后关闭，禁止事后追加；所有 assignment 禁止 DELETE。
- 对 P3 applied assignment supporting set 内的每条 previous EntityResolutionDecision，必须在同一
  transaction 按既有 contract `active -> superseded`，并分别创建一条匹配的 active
  `link_existing` replacement；普通非 evolution resolution 的既有 supersede contract 不被本设计
  改写。P3 replacement 的 payload、`evolution_assignment_id` 与 predecessor link 插入后不可变，
  禁止 DELETE。
- `Entity.canonical_entity_id` 的 non-NULL old ID -> different non-NULL new ID 是 P3
  reassignment，必须在同一 applied transaction 由唯一 assignment、完整非空的 1:N previous/new
  ER Decision pair set 和 OLD/NEW pointer 完整配对；裸 pointer rewrite、NULL target、pair set 缺失/
  多余或不匹配 target 均由 deferred
  constraint trigger 拒绝。initial NULL -> non-NULL projection 仍走既有 Entity Resolution writer，
  但必须遵守第 9 节 lock/current-resolver service invariant；它不伪造 evolution assignment。

normal accepted correction 的 deferred pairing trigger 必须在 commit 前验证：恰有一个同
command direct successor Decision；旧 pending proposal 的全部 live source/assignment rows 都
进入 `superseded`；新 `pending|applied` Decision 创建完整 replacement source/target-slot/
assignment children，并按 5.3/5.5 predecessor rules 接续。旧 `rejected|stale` audit-only head 没有
proposal children，只配对新 Decision。

command-root pairing trigger 还必须拒绝 transaction end 时没有 initial Decision 的新 root，或
initial Decision 不以 NULL predecessor 成为该 command 唯一 chain root。`rejected|stale` initial
Decision 必须为 audit-only 且 child/effect count 为 0；这不能只依赖 service insertion order。

whole-chain trigger 必须从每个 touched chain 的唯一 head 递归跟随 predecessor 到唯一 NULL
root，row count 必须等于该 scope 下总 row count，途中 UUID 不得重复；0 root、2 roots、0 head、
2 heads、orphan component 和 single-statement mutual/long cycle 全部拒绝。对 source/assignment，
chain scope 分别是 `(library_id, command_id, source_canonical_entity_id)` 与
`(library_id, command_id, entity_id)`。对 merge/split resolved assignment，trigger 还必须证明其
`target_successor_id` 所属 source transition 的 `source_canonical_entity_id` 等于 assignment 的
`from_canonical_entity_id`；同 command/Decision 但 cross-source slot 也必须拒绝。

Cancellation 是唯一 no-child termination 例外：旧 pending Decision 必须被一个同 command、
`evaluated_outcome=lifecycle_status=cancelled` 的 direct successor Decision 接管；旧 proposal 的
全部 live source/assignment rows进入 `superseded`；cancelled Decision 下 source、successor、
assignment count 必须全部为 0。旧 successor proposal rows保持 immutable，并因其 parent source
已 superseded 而永不成为 applied edge。没有该 cancelled Decision、跨 command cancelled
Decision、或同 transaction 出现任何新 child/effect 时，constraint trigger 必须拒绝。

所有 whitelist updates、Decision successor insert 和 effects 必须位于同一 outer transaction；
deferred trigger 在 commit 前验证配对。任一步失败全部回滚。跨 library、cycle、将 reassign
伪装为 global lineage、或无法由 constraint 表达的 reachability 同样由锁后 service validation
拒绝；不能将 service invariant 误写为普通 SQL 静态保证。

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

`Entity.canonical_entity_id` 保持 current projection assignment。仅 P3.1 覆盖的 non-NULL old ID
到 different non-NULL new ID reassignment 禁止单独更新；initial NULL 到 non-NULL assignment 仍走
第 5.6 节保留的既有 Entity Resolution writer。所有 P3 reassignment command 的 atomic unit 必须是：

```text
lock complete scope
-> re-read lineage, Entity, and the complete active supporting-subject Decision set
-> validate preconditions and partition
-> append Evolution Decision / assignment audit and merge/split source/successor audit where applicable
-> mark every prior supporting EntityResolutionDecision superseded
-> flush all old active -> superseded updates to release the existing immediate active-subject unique index
-> for every supporting subject, append one new EntityResolutionDecision(link_existing)
-> link every new Decision to the assignment and its same-subject predecessor
-> update Entity.canonical_entity_id
-> flush; outer transaction commits or rolls back all rows together
```

每个新的 EntityResolutionDecision 必须与其 predecessor 保持相同 `subject_fingerprint` 和
`entity_id`，复制旧 decision 的 immutable observation、candidate snapshot 和 evidence snapshot，
使用新的 selected canonical、`method = canonical_evolution_v1` 和新 decision fingerprint，并以
`supersedes_decision_id` 指向该 same-subject old active decision、以 `evolution_assignment_id` 指向
本次 assignment。旧 Decision 只允许既有 `active -> superseded` lifecycle transition；其 payload、
snapshot 和 identity 绝不重写或删除。candidate 已 purge 时继续保留 snapshot，FK 可为 NULL。
由于既有 `(library_id, subject_fingerprint) WHERE lifecycle_status='active'` 是 immediate unique，完整
supporting set 校验通过后必须先更新全部 old rows 并 flush，再 INSERT 全部 replacements；禁止逐 subject
交错 supersede/insert，也禁止先插 new active row。该 flush 不是 commit，任何后续错误仍回滚整个 set。

完整 supporting set 为空、set 中任一 member 无法一一 replacement、存在 active canonical-bearing
Decision 与 Entity pointer 冲突、Entity 已不再指向 command 的 source，或 partition 未唯一确定时，
结果是 `PENDING`：不得更新 Entity，亦不得 supersede 任何旧 Decision。这样 legacy projection 和
多 subject projection 都不会被 first/latest 猜测或凭空归属到 split successor。

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

同一 projection 上未终结的 P3 pending intent，或 `stage_entity_merge` 已创建且状态为
`pending_review|approved` 的未 applied/cancelled/rejected action，都是 **live projection intent**。
该 intent 只用于 admission/mutual exclusion，不得被复制进 Canonical lineage，也不得跨系统
supersede。只有 exact persisted replay 未命中的 potentially mutating request 才进入 mutual-exclusion
admission；双方取得共同 scope-40 locks 后都必须查询对方的 live intent。先提交者占用 slot，后得
锁者不得把同一 request rebase 到 fresh state 后继续：

```text
GraphGovernance-first commit
  => evolution returns STALE_OPERATION(graph_governance_projection_intent_occupied)
     before evolution root INSERT

evolution-first commit (PENDING or APPLIED)
  => stage_entity_merge raises graph_governance_state_changed
     before GraphGovernance action/item INSERT
```

Evolution exact Decision replay、cancellation replay 和 GraphGovernance exact action replay 是明确的
non-mutating 例外：鉴权、incoming idempotency identity/hash、transaction health 及所需 shared locks
通过后，直接返回既有 row 的 actual current status，writes/effects 增量为 0，不参与 opposing live-intent
winner 竞争，也不得复活 terminal row。identity/hash 不同不是 replay，仍按各系统既有 conflict
vocabulary 拒绝。只有 exact miss 才继续下述 state token 和 live-intent gate。

`stage_entity_merge` 的现有 ontology entity hashes 不包含 Canonical projection state，不能承担 P3
precondition。未来 P3.1 integration 必须由 GraphGovernance entity-merge read/prepare API 另签发
`expected_canonical_evolution_guard_fingerprint` opaque token；issuer 和锁后 evaluator 使用 5.1 同一个
JCS/UUID/string contract，对以下 closed object 计算 lowercase SHA-256：

```json
{
  "canonical_states": [
    {
      "canonical_entity_id": "UUID",
      "current_source_resolution_state": null,
      "current_source_transition_id": null,
      "operational_status": "active"
    }
  ],
  "contract_version": "canonical_entity_evolution/v1",
  "entity_states": [
    {
      "active_resolution_decisions": [
        {
          "canonical_entity_id": "UUID-or-null",
          "decision_fingerprint": "64-lowercase-hex",
          "decision_id": "UUID",
          "decision_kind": "link_existing|create_new|pending_review|rejected",
          "subject_fingerprint": "64-lowercase-hex"
        }
      ],
      "canonical_entity_id": "UUID-or-null",
      "entity_id": "UUID"
    }
  ],
  "library_id": "UUID",
  "live_evolution_intents": [
    {
      "command_id": "UUID",
      "current_decision_id": "UUID",
      "entity_ids": ["UUID"],
      "operation_kind": "merge|split|reassign"
    }
  ],
  "operation_kind": "graph_governance_entity_merge"
}
```

`entity_states` 必须恰含 survivor/loser 两个 distinct Entities，按 Entity UUID 排序；每项包含全部
active EntityResolutionDecisions，并沿用 5.2 的 exact member shape 与排序。`canonical_states` 恰含
两项 non-NULL Entity pointers 引用的 distinct canonicals，按 canonical UUID 排序，并沿用 5.2 的
exact state shape；两项 pointer 都为 NULL 时必须是 `[]`。`live_evolution_intents` 恰含 actual current
head 为 pending 且其 live assignment 触及 survivor/loser 的 P3 intents，按
`(command_id,current_decision_id)` 排序；每项 `entity_ids` 是该 intent 触及这两个 Entities 的交集，
去重并按 UUID 排序。所有 objects 禁止 extra key；API 只暴露 token，不接受 caller 自报 snapshot。

GraphGovernance exact miss 在 shared locks 后 fresh 重算该 object；token 不等，或 observed
`live_evolution_intents` 非空，均在 action/item INSERT 前返回 `graph_governance_state_changed`。accepted
new action 把 expected guard fingerprint 放入既有 immutable action payload 并纳入 command hash，不新增
GraphGovernance schema column。Evolution 的 expected state 则按 5.2 精确绑定 matching
GraphGovernance live-intent absence。GraphGovernance action 只有进入既有 terminal
`applied|cancelled|rejected` 后才释放其 live slot；释放和状态转换沿用 GraphGovernance 自身事务与
审计 contract，不写 Evolution Decision。任何一方 rollback 都不占 slot。

因此同一初始 snapshot 的 mutating overlap 恰有一个 persisted intent/effect winner；后者使用各自
冻结的 stale vocabulary 且零写入。数据库 serialization/unique race 才返回/映射
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
decision、complete partition、expected precondition fingerprint 和 cycle condition。若差异改变完整
lock-set membership、current identity、live slot 或 lineage integrity，按 5.1 第一层 pre-root
admission stale 零写入；不得继续 INSERT root。只有这些 admission invariants 全部稳定、差异仅体现为
其余 opaque observation mismatch 时，new-root request 才按第二层 `none -> stale` 写 audit-only
root/D1；existing-root request 严格按 5.2 matrix 处理。这里的“不得继续”禁止的是 state-changing
effect，不得再被解释为可以任选“保留或回滚”矩阵已明确允许的 audit row。

scope `10 canonical_entity` 也是该 canonical 的 projection-membership mutex，用来阻止
`SELECT ... FOR UPDATE` 无法覆盖的 phantom Entity INSERT。契约如下：

1. Canonical evolution 在无锁 discovery 后，将全部 source/target、resolver/reachability 访问到的
   Canonical IDs（scope 10）、
   已发现/待创建的 Entity IDs（scope 40）和 resolution subjects（scope 50）一次性去重排序并
   取得。锁住 source scope 10 后再重读完整 projection set。
2. 任何创建 Entity 并设置 `canonical_entity_id`，或把既有 Entity pointer 从 old canonical 改到
   new canonical 的 writer，都必须先无锁 discovery，再一次性取得 proposed old/new 及 resolver
   访问到的全部 scope 10、Entity scope 40 和相关 subject scope 50，随后在锁内重跑 current
   resolver。新 Entity UUID 必须先在内存生成，才能进入完整 lock set。该规则覆盖 canonical
   resolution、graph materializer 和未来所有 projection writer。
3. 禁止先锁 scope 50 或 40 后再追加 scope 10。锁后发现无锁 discovery 漏了 Entity/subject 时，
   当前 transaction 必须 `STALE_OPERATION(lock_set_changed)` 并 rollback；只能在新 transaction
   以重新计算的完整 set 重试。
4. writer 先持有 source scope 10 并提交新 projection 时，evolution 随后取得该锁并在 re-read
   看见新 Entity；旧 partition/precondition 必须 stale 且在 root INSERT 前零写入。evolution 先
   持锁并 applied 时，writer 随后必须重跑 resolver：merge 可以显式落到唯一 current survivor；
   split 的 `forked|pending` 或 historical source 不得继续挂回旧 canonical。

不得用全库锁或只锁已发现 projection 替代 scope-10 membership mutex。

正式 result vocabulary 冻结为：

```text
APPLIED             all atomic effects staged in the owning transaction; durability follows outer commit
REUSED              an exact persisted Decision payload was found; never means success by itself
PENDING             incomplete split/projection decision, durable only after outer commit; no unsafe reassignment
STALE_OPERATION     no source/successor/assignment/effect mutation; caller re-reads current state
RETRYABLE_CONFLICT  no success claim; retry transaction from fresh read
REJECTED            invalid/scope/policy command, with explicit reason
CANCELLED           explicit audited termination of one current pending intent
```

所有 result 至少返回 `result`、`command_id nullable`、`decision_id nullable` 和非空 stable
`reason_code`。当 `result == REUSED` 时，`command_id`、`decision_id` 及以下四个字段必须全部
non-NULL：

```text
reused_decision_id
effective_outcome = APPLIED | PENDING | REJECTED | STALE
current_decision_status = pending | applied | rejected | stale | cancelled
current_decision_id
```

其他 result 的上述四个 replay/current-head 字段默认均为 NULL；唯一例外是紧随其后的 current
`pending` RETURN-only 分支，它只填充 `current_decision_status/current_decision_id`。这不限制
`APPLIED`、`PENDING`、`CANCELLED` 等结果按各自契约返回非空 `command_id/decision_id`。

5.2 矩阵中 current `pending` correction 对 new `rejected|stale` 的 RETURN-only 分支不是新
Decision，也不是 replay；返回 shape 冻结为：

```text
result = REJECTED | STALE_OPERATION
command_id = existing command root ID
decision_id = NULL
reused_decision_id = NULL
effective_outcome = NULL
current_decision_id = old pending Decision ID
current_decision_status = pending
expected_predecessor_decision_id = current_decision_id
reason_code = stable evaluation-specific rejection/stale reason
```

这里返回的 `expected_predecessor_decision_id` 是下一次显式 correction/cancellation 可使用的 actual
head CAS token，不代表本次 request 已被接受。Decision、source、successor、assignment、Entity、
EntityResolutionDecision 更新数必须全部为 0；旧 pending intent 继续占用 live slot。

`decision_id == reused_decision_id`，两者始终指向 exact payload 命中的 Decision；
`current_decision_id` 单独指向同 command 的 actual current head。命中 current head 时
`reason_code=exact_current_decision_replay`，命中 historical superseded Decision 时
`reason_code=exact_historical_decision_replay`。`effective_outcome` 来自
`reused_decision_id.evaluated_outcome`，不是从其可能已经变成 `superseded` 的 lifecycle 猜测；
`current_decision_status` 来自 actual current head。调用方只有在下列完整条件成立时，才能把
返回解释为已有 applied effect：

```text
result == REUSED
effective_outcome == APPLIED
current_decision_status == applied
reused_decision_id == current_decision_id
```

`REUSED` + `PENDING|REJECTED|STALE` 不是执行成功；复用 historical superseded payload 也不是
执行成功。调用方、materializer 和 API adapter 禁止仅按 `result == REUSED` 打开 success gate。
`STALE_OPERATION`、`RETRYABLE_CONFLICT`、`PENDING` 和 `CANCELLED` 不得统称为 `rejected`。

## 10. H-I. Replay, Idempotency, and Cycle Prevention

### Replay

`command_identity_fingerprint` 与 `decision_payload_fingerprint` 分别按 5.1、5.2 的 exact
canonical JSON 计算。lookup、replay 和 correction 顺序固定为：

1. 普通 apply/correct 先执行 5.1 的 envelope validation、read-only root lookup、完整锁和锁后
   re-lookup；same key/different identity 是
   `REJECTED(idempotency_key_conflict)`；different key/same identity 是
   `REJECTED(command_identity_alias_key)`，两者均零写入。Cancellation 只走 5.2 的
   existing-root-only control path，禁止进入 root candidate/INSERT 分支。
2. existing-root branch 在任何 current-leaf/live-slot admission 前，先按
   `(library_id, command_id, decision_payload_fingerprint)` 查找。精确命中
   `pending|applied|rejected|stale|superseded` Decision 时返回 `REUSED` 及 9 节全部字段；
   Decision、source、successor、assignment、EntityResolutionDecision 和 Entity 更新数都为 0。
   命中 cancelled control payload 时返回幂等 `CANCELLED`，同样零写入。
3. exact payload 未命中才读取“没有 direct successor”的唯一 current head，并校验 caller 的
   `expected_predecessor_decision_id`；既有 root 必须传 actual head ID。不相等返回
   `STALE_OPERATION(expected_predecessor_mismatch)` 且零写入。
4. existing root exact miss 随后才执行 current identity/projection/live-slot/precondition re-read。
   current head 为 `pending|rejected|stale` 时，仅按 5.2 matrix append；current head 为
   `applied|cancelled` 时拒绝不同 payload，分别返回
   `REJECTED(command_already_applied|command_cancelled)`，不追加 Decision。
5. new-root branch 必须传 NULL predecessor，并先通过 5.1 new-root admission 才 INSERT root、进入
   matrix `none (D1)`。若锁后 re-lookup 发现并发 winner，必须切换 existing-root branch 并从 exact
   payload lookup 重走，不能先做 current-leaf gate。
6. old superseded payload 的精确 replay 只指回 old Decision，并同时报告 actual current head；
   不得把 old payload 恢复为 current。若调用方有意再次提交相同业务分配，必须通过新的
   reason/evidence/precondition 形成可审计的新 payload fingerprint，并仍通过 current-head CAS。

不得跨 root 通过名称、target 相似、latest row 或“看起来等价的 applied edge”复用 command。
applied 后重放相同 command/payload 才是 `REUSED(APPLIED)`；基于已经 historical 的 source
或 target 构造不同 identity 必须在锁后返回 `STALE_OPERATION(current_identity_changed)` 且在
root INSERT 前零写入，而不是暗中改写为当前 leaves。

### Cycle prevention

在完整 lock set 内、root INSERT 与 successor write 前，对 proposed state-changing edge 执行同库
reachability/integrity check。合法 merge/split admission 要求所有 source 和 target 都是 current
leaves；因此 target 已可到达 source 在合法输入上不可达。若锁后读取仍观察到该条件，必须视为
persisted lineage integrity failure，返回 `STALE_OPERATION(lineage_integrity_error)`，不得写
command root、Decision、edge 或 effect，更不得持久化 `REJECTED(cycle)`。正常 sequential reverse
request 会在更早的 current-leaf admission 返回 stale：

```text
A -> B, then B -> A        STALE_OPERATION(current_identity_changed)
A -> B -> C, then C -> A   STALE_OPERATION(current_identity_changed)
```

不存在 post-admission persisted cycle-rejection branch。并发 `A -> B` 与 `B -> A` 必须由共同
scope-10 locks 串行化：winner applied 后 loser fresh re-read 命中 current-identity stale，最终只能
存在一个 edge，loser 零 root/Decision。

merge survivor self-membership 不生成 edge，因此不是 cycle exception。cycle protection
必须覆盖 multi-source merge 和 multi-target split；读时若遇到损坏 cycle，resolver 返回
`pending(lineage_integrity_error)`。

## 11. J. Transaction Ownership

Evolution service、current resolver 和 assignment writer 都不调用 `commit()` 或
`rollback()`；调用方拥有 outer transaction。它们可以使用 nested savepoint 来识别
unique/serialization race，但捕获 `IntegrityError` 前必须让 nested savepoint 完成 rollback；
禁止在 failed SQLAlchemy Session 上构造 `REUSED` 或其他成功结果。service 不得 rollback
调用方的 outer transaction，也不得在外层事务内留下部分 state。

`APPLIED`、`PENDING` 以及 matrix 明确允许持久化的 audit-only `REJECTED`/`STALE_OPERATION`
都只表示相应 rows/effects 已在健康 outer transaction 中 staged。调用方必须成功 commit 后才可对外
声称 durable；commit 失败或主动 rollback 时，不得返回一个暗示 `command_id`/`decision_id` 已持久化的
结果。pre-root stale 和 existing-pending RETURN-only stale/rejected 本来就是零写入，不需要也不能
伪造 audit durability。

accepted correction/cancellation 先在完整锁内计算最终 result，再开始 mutation。一次 applied
evolution 的事务边界至少包含：

```text
all advisory locks
all required FOR UPDATE reads
CanonicalEntity creation for fully resolved split, if any
Evolution Decision / sources / successors / assignments
new and superseded EntityResolutionDecision rows
Entity.canonical_entity_id updates
```

pending correction 的写序必须在同一 outer transaction 中先将旧 pending Decision/source/
assignment 切换为 `superseded` 并 flush 释放 immediate partial uniques，再插入新 Decision 和
完整 proposal/effect rows；deferred triggers 在 transaction end 验证 direct successor 配对。
这个中间 flush 不是 commit。若在 supersede 后、新 rows flush 后或部分 Entity update 后发生
任一异常，outer rollback 必须恢复旧 pending、全部旧 active supporting-subject
EntityResolutionDecisions 和全部 Entity
指针，并移除新 rows/new targets。cancellation 使用 5.2 的同等原子边界。

任何 applied reassignment 还必须遵守第 7 节的独立 immediate-unique 写序：完整 set 校验后，先把
全部 old supporting-subject EntityResolutionDecisions 更新为 superseded 并 flush，再批量 INSERT
全部 new active replacements，最后更新 Entity pointer。禁止 new-before-old 或逐 pair flush；deferred
pairing trigger 在 outer commit 证明 set 完整，任何异常由 outer rollback 恢复全部 old active rows。

`graph_extraction_materializer` 已在 `materialize_graph_extraction_job` 中拥有 outer
transaction；P3 integration 必须适配这一事实。GraphGovernance 和管理 command 同样由其
入口事务持有。mutation 开始后的任何异常、要求停止 mutation 的 stale 或 failed postcondition，
都必须使上述写入一起 rollback；不得只留下 Entity 更新或半条 lineage。该 rollback 规则不吞掉
5.1/5.2 matrix 明确允许 staged 并由 caller commit 的 audit-only D1；第一层 pre-root stale 与
RETURN-only 分支则始终保持零写入。

## 12. K. Exact Future P3.1 Implementation Scope

在明确授权 P3.1 implementation 后，允许的最小范围是：

1. `0071` 永久 immutable。只有另行授权的 corrective `0072` 可以实现第 5 节 tables、
   composite FKs、partial unique indexes、triggers，并在保留既有 EntityResolutionDecision
   `(id, library_id)` unique 的同时新增 5.5 冻结的 nullable `evolution_assignment_id` composite
   FK，且必须执行 5.0 的空表 forward/downgrade gates；不得修改
   historical Fact bridge，也不得为非空旧 rows 猜测 backfill。
2. ORM models、typed command/result contracts 和 Canonical evolution service；command
   必须显式接收 survivor、targets、partition、idempotency key、reason、method、evidence
   和 expected preconditions。
3. shared multi-identity lock service，并把 Entity Resolution subject lock 及所有
   `Entity.canonical_entity_id` writers 迁移到第 9 节的一次性 scope-10/40/50 contract。
4. `resolve_current_canonical_identity`，并把它接入 Entity Resolution candidate/existing
   mapping path。
5. Entity projection reassignment writer，与完整 1:N same-subject old/new
   EntityResolutionDecision supersede set 一起运行。
6. `graph_extraction_materializer` 的 current-canonical gate，以及 shared Fact Resolution
   preflight 的 canonical-only gate：非 `resolved` identity，或 `resolved` 但
   `resolution_eligible == false` 的 identity，均写 P2-compatible pending decision，且不创建
   LogicalFact、FactAssertion、KnowledgeRelation 或 RelationEvidence 的新 bridge。该 gate 不改变
   RawClaim promotion 模型或 API。
7. `stage_entity_merge` 对相同 Entity projection scopes 使用共享 lock API、第 8 节 server-issued
   canonical-evolution guard token 和 live-intent admission，保持其现有 ontology-specific effect
   语义不变；guard fingerprint 只进入既有 action payload/command hash，不新增 GraphGovernance column。

明确不属于 P3.1 implementation：StablePredicate evolution、Fact reconciliation、current
Fact lifecycle、RawClaim promotion semantics、Publication、Retrieval、graph UI，及任何对
历史 Fact/Assertion/Relation/Evidence FK 的 update。

## 13. L. Required Implementation Tests

未来 implementation 必须使用已有 pytest/SQLAlchemy 约定，至少覆盖：

| Area | Required evidence |
| --- | --- |
| Merge | 显式 existing survivor、无 self-edge、losing source 唯一解析、禁止自动新 C。 |
| Resolver status separation | 无 successor 的 `active`、`pending_review`、`disabled` leaf 都为 `resolved(self)`；仅 `active` 为 resolution-eligible。resolver fixture 中显式 terminal row 返回 `historical_only`；P3.1 三种 writer 创建该 row 必须被拒绝，且 disabled/缺失 successor 不得推断为它。 |
| Split resolver | 无 context 为 `forked`；唯一 Entity projection/subject context 为 `resolved`；pending/invalid context 为 `pending`；没有 first/latest fallback。 |
| Projection partition | 覆盖全部 direct projections、重复/遗漏拒绝、pending 不改 Entity、resolved 原子更新 Entity 与完整 1:N supporting-subject ER replacement set。 |
| Multi-subject ER replacement | 同一 Entity 至少两个 distinct active subjects 都指向 old canonical；applied reassignment 为每个 subject 恰建一个 same-subject snapshot-preserving replacement 并共同引用一条 assignment。空 set、遗漏/重复 member、cross-subject predecessor、snapshot 差异、active canonical conflict 均 fail closed，不得 first/latest。 |
| Sequential projection reuse | standalone `A -> B` applied 后再以新 Command Identity 执行同 Entity `B -> C`；旧 applied assignment immutable 且不占 live slot，新 command assignment 使用 NULL cross-command predecessor，每个 subject 的 ER Decision history 连续且最终 pointer 为 C。 |
| Pending new-target slot | D1 pending split 含 existing B 与 new slot T：不创建 CanonicalEntity；resolved assignment 以 `target_successor_id` 指向本 D1/T 且 target UUID 为 NULL。D2 applied 创建恰一个 T canonical、引用本 D2/T；D1 slot immutable，D1 source/assignments superseded 且全历史保留。 |
| History | 旧 EntityResolutionDecision 的 identity/payload/snapshots 保留，仅 supporting set 允许 audited `active -> superseded`；所有 Fact/Assertion/Relation/Evidence bridges 不变。 |
| Lifecycle matrix | 覆盖 5.2 每个 predecessor/new-result cell；未列转换必须被拒绝。`applied/cancelled` head 不接受 successor，`superseded` 恰有一个既有 successor 且不接受第二个；pending correction 的 stale/rejected evaluation 零写入。 |
| Pending split correction | D1 与 D2 都是 `A->{B,C}`；D1 `{e1->B,e2->pending}`，D2 `{e1->B,e2->C}`；同 root append、D1 superseded、无 idempotency conflict。 |
| Pending release/takeover | unauthorized/CAS mismatch 取消零写入；合法取消追加 audited cancelled Decision，旧 proposal 全部 superseded 且 cancelled Decision 的 successor/projection snapshots 均为 `{}`、零 child；验证 expected 复制 predecessor observed、fresh observed 使用 current pending context 且二者不作 equality gate；未取消时 competing identity 在 root INSERT 前零写入；只有取消 commit 后真正不同的新 identity 才能占用 slot，相同 identity + alias key 仍拒绝。 |
| Command identity | merge/split/reassign exact one-line bytes 与 SHA-256 golden fixtures；participant/target 输入顺序的等价 permutation 得到相同 hash；merge survivor 和任一 scope 字段改变得到不同 hash；NFC/UUID validation 与 no-trailing-newline 均验证。 |
| Decision payload identity | merge/split/reassign/cancel 四个 base golden fixtures及 non-zero six-decimal exact-number fixture；完整 member shape、evidence set permutation、projection/target-spec stable ordering、`0`/`0.0` normalization均验证；binary float、七位以上小数、NaN/Infinity、negative zero、duplicate key、non-NFC、input BOM、extra key 全部拒绝。 |
| Precondition token | merge/split/reassign 分别覆盖 server `prepare -> unchanged opaque token round-trip -> mutation of every listed snapshot field -> stale`；数组 permutation 正规化后 hash 相同，extra/missing key、caller-supplied snapshot 和不完整 active-subject set 拒绝。分别断言 lock/current/live-slot/integrity admission drift 是 pre-root 零写入、admission 稳定但其余 observation mismatch 是 new root + audit-only stale D1、existing pending mismatch 是 RETURN-only；audit D1 commit 后才 durable，outer rollback 后不可声称 persisted。 |
| Physical bounds | 5.0.1 每种 bounded string/JSON/count/depth 做 exact-limit accept 与 limit+1 reject；service JCS-byte gate 和 PostgreSQL stored-json gate 分别验证，均不得在拒绝时留下 root。 |
| Root lookup | same key/different identity 为 `idempotency_key_conflict`；different key/same identity 为 `command_identity_alias_key`；两者均不新增 alias/root/Decision。 |
| Replay same payload | current/historical payload 均零写入并返回 exact `reused_decision_id`、`effective_outcome`、actual current status/ID；cancel replay 返回幂等 CANCELLED。 |
| REUSED consumer gate | `REUSED(PENDING|REJECTED|STALE)` 与 historical superseded replay 均不得被 materializer/API 当作 applied；只有 9 节完整 applied 条件通过。 |
| Superseded lineage constraints | cross-command Decision/source/assignment predecessor、cross-source、cross-projection、cross-Decision target slot、self predecessor、第二 root 和 branching successor 均由数据库拒绝；predecessor insert 后不可变且不能在同 transaction 构造 cycle。 |
| Direct SQL constraint/trigger enforcement | 分别以 INSERT/UPDATE/DELETE 和 COMMIT 负例覆盖 5.6 的 FK/check/partial index、whole-chain、immutability、DELETE ban、lifecycle/pairing、no-child cancellation、slot/source/assignment/ER pairing 和 applied cardinality；另覆盖 non-NULL Entity pointer 裸 rewrite、缺失/错误 assignment、1:N ER pair set 缺失/多余、cross-subject、subject/entity/canonical/snapshot 或 OLD/NEW target mismatch。deferred invariant 必须断言在 COMMIT 失败并整笔回滚。hash 测试只声称 DB format/unique 责任，不伪造 JCS 重算保证。 |
| Cycle | sequential reverse/long-cycle request 在 current-leaf admission 返回 stale；异常 persisted graph 在 root INSERT 前返回 `STALE_OPERATION(lineage_integrity_error)` 且零写入，禁止 persisted `REJECTED(cycle)` fixture；并发反向补边只能一个 applied、另一个 stale 且零 root；multi-source/target、三连接环与损坏读时 fail closed。 |
| Cross-library | 五张 P3 audit tables 的 scoped FK 与 model/service 都拒绝跨 library source、target、Entity、Decision；P3 ER replacement 另验证 previous/new/assignment 三方 library 相同，不把 legacy ER predecessor 单列 FK 误报为 scoped。 |
| Lock API | complete set 去重后固定排序；Entity Resolution 与 GraphGovernance 共享 projection scope；锁后 stale re-read 返回正确 vocabulary。 |
| Projection phantom exclusion | evolution-vs-projection-writer 双连接覆盖 writer-first 与 evolution-first；所有 writer 先取 scope 10，再按总序取 40/50，锁后新 Entity 导致 stale/retry，historical/forked/pending source 不得收到新 pointer。 |
| PostgreSQL two-connection concurrency | 同 payload、竞争 correction、overlapping merge/split/reassign、cancel-vs-correction 均按下述允许结果和最终 cardinality 验证；必须有 timeout/no-deadlock assertion。 |
| Pending cancellation/takeover concurrency | cancel-vs-different-identity 覆盖 cancellation-first commit、new-intent-first 和 cancellation rollback；新 root/Decision/effect 在 cancellation commit 前必须为 0，rollback 后旧 pending 继续占 slot。 |
| GraphGovernance mutual exclusion | evolution-vs-`stage_entity_merge` 真实 PostgreSQL 双连接覆盖 governance-first/evolution-first；共同 projection lock、双方 live-intent admission、固定 loser vocabulary、唯一 persisted intent/effect winner 和 no-deadlock 都必须验证。另覆盖 GraphGovernance guard token 每个 field mutation/permutation/extra-key、只收集 matching entity-merge actions、expected token 持久化进既有 payload/hash，以及双方 exact replay 在 opposing intent 存在时仍零写入返回既有 current status。 |
| Correction/cancellation atomic rollback | correction 分别在 old proposal supersede flush、全部 old supporting-subject ER supersede flush、new ER/rows flush、部分 Entity update 后注入异常；禁止 new-active ER 先于 old supersede flush。cancellation 分别在 old proposal supersede flush、cancelled Decision flush 后注入异常；新 session 验证 logical row values、relationships、counts 和 lifecycle/pointers 与事务前一致。 |
| Migration upgrade/downgrade | single head、offline SQL、disposable PostgreSQL actual up/down、constraint negative inserts、source/assignment 单列 PK 与 assignment 三列 referenced UNIQUE、empty old-0071 success、old/target 五表逐表 nonempty fail-closed、maintenance quiescence、九张受影响表的固定 `NOWAIT` lock 总序及 stable busy failure。静态/actual DDL 都证明 target tables/keys -> ER column/FK -> dependent triggers 的 upgrade 顺序及 downgrade reverse order。 |
| New Fact guard | canonical `forked`/`pending`/`historical_only`，以及 `resolved` 但 `resolution_eligible == false`，仅产生 pending FactResolutionDecision，且无 Fact/Assertion bridge。 |
| Regression | Publication、Retrieval、RawClaim promotion、P2 historical Fact reads 保持既有行为。 |

PostgreSQL 双连接结果必须可判定：

- 首次 same root + same payload：结果 multiset 只能是 `{APPLIED, REUSED(APPLIED)}`，或对
  pending payload 为 `{PENDING, REUSED(PENDING)}`；最终 command=1、Decision=1、effects 一套。
- 同 D1、相同 D2 payload：只能是一个 `APPLIED|PENDING` winner 加一个相同 effective outcome
  的 `REUSED`；相对 D1 前态只新增 Decision=1 和一套 D2 child rows，D1 恰有一个 direct
  successor，Entity/ER effect 最多执行一次。
- 同 D1、不同 D2 payload 且都携带 D1 CAS：先取得锁者可成为 `APPLIED|PENDING` winner；
  后取得锁者必须 `STALE_OPERATION(expected_predecessor_mismatch)` 且零写入。数据库在进入
  锁后 CAS 前产生的 serialization/deadlock error 才能映射为 `RETRYABLE_CONFLICT`；确定性竞争
  fixture 不注入该错误，最终相对 D1 只新增 Decision=1 和 winner 的一套 child rows。
- 两个首次、不同 identities 的 merge/split/reassign overlap 同一 source 或 projection：先得锁者
  为 `APPLIED|PENDING`；loser 必须在 root INSERT 前
  `STALE_OPERATION(source_or_projection_slot_occupied|current_identity_changed)`。最终 command=1、
  Decision=1，只有 winner child/effect rows，loser `command_id/decision_id` 均为 NULL。
- 两个连接从同一初始 snapshot 并发提交 `A -> B` 与 `B -> A` state-changing edge：完整 scope-10
  lock sets 必须相同排序且发生等待；winner 为 `APPLIED`，loser 在锁后 current-leaf re-read 返回
  `STALE_OPERATION(current_identity_changed)`。最终 command=1、Decision=1、edge=1，递归查询无环，
  两个连接在 timeout 内无 deadlock。不得把 loser 降级为基于旧 snapshot 的 persisted cycle reject。
- 三个连接从同一初始 snapshot 并发提交 `A -> B`、`B -> C`、`C -> A`；fixture 使用三个均无
  direct Entity projection 的 active leaves，以免 projection-membership change 改写期望。每个连接都
  按全局 scope-10 UUID order 获取自己的完整去重 lock set，barrier 必须制造 overlap。结果 multiset 固定为
  `{APPLIED, APPLIED, STALE_OPERATION(current_identity_changed)}`；最终 command=2、Decision=2、
  edge=2，递归查询无环，loser 零 root/Decision，三个连接都在 timeout 内完成且无 deadlock。
- projection writer 与 evolution overlap，writer 先得 source scope 10：writer commit 后 evolution
  锁后看见新增/改挂 Entity，返回 `STALE_OPERATION(lock_set_changed|current_identity_changed)`，
  evolution command/Decision/child/effect 增量全为 0。evolution 先得锁：writer 等待后重跑
  resolver；merge 只可写唯一 current survivor，split `forked|pending` 时 Entity pointer 写入为 0，
  两种调度都不得留下指向 historical source 的新 projection。
- cancellation 与 D1 correction 竞争且 correction 先得锁：结果 multiset 是
  `{APPLIED|PENDING, STALE_OPERATION(expected_predecessor_mismatch)}`；最终 command=1、
  Decision=2、D1 只有 correction successor、无 cancelled Decision，只有一套 D2 children/effects。
- cancellation 与 D1 correction 竞争且 cancellation 先得锁：结果 multiset 是
  `{CANCELLED, STALE_OPERATION(expected_predecessor_mismatch)}`；最终 command=1、Decision=2、
  D1 只有 cancelled successor，D1 source/assignments 全部 superseded，cancelled Decision child=0，
  Entity/ER updates=0。
- cancellation 与真正不同 Command Identity 的 new intent 竞争且 cancellation 先得锁：new
  transaction 在 cancellation commit 前不得 INSERT root；commit 后取得锁并完成 fresh re-read，
  才可成为 `APPLIED|PENDING` winner。最终旧 root 的 D1 只有 cancelled successor，新 root 使用新
  key/identity，两个 root 之间没有 supersede FK；new intent 的 child/effect 恰一套。
- cancellation 与真正不同 Command Identity 的 new intent 竞争且 new intent 先得锁：因旧 pending
  slot 仍占用，new intent 必须在 root INSERT 前返回
  `STALE_OPERATION(source_or_projection_slot_occupied)`；随后 cancellation 可正常 commit。new
  intent 的 command/Decision/child/effect 增量全部为 0，调用方只能在 cancellation commit 后以新
  transaction 显式重试。
- cancellation 在持锁并暂存 supersede/cancel row 后 rollback，同时 new intent 等锁：new intent
  取得锁后必须观察到恢复的旧 pending，返回上述 slot-occupied stale；旧 root/Decision/proposal、
  Entity/ER pointers 与 transaction 前一致，不得出现新 root。该 fixture 必须在另一个连接确认
  cancellation 未 commit 时 command count 不变。
- evolution 与 `stage_entity_merge` 使用相同 projection 的双连接调度必须各覆盖一方先得锁。
  governance-first commit 时，evolution 必须在 root INSERT 前返回
  `STALE_OPERATION(graph_governance_projection_intent_occupied)`；evolution-first commit 时，
  `stage_entity_merge` 必须在 action/item INSERT 前返回 `graph_governance_state_changed`。fixture
  必须断言 shared scope-40 lock key 相同、结果只含一个 persisted intent/effect winner、loser 所属
  rows 增量为 0、没有 historical source 新 pointer，并在 timeout 内无 deadlock。任一 winner
  rollback 后 waiter 必须重读到 slot 未占用并可按原 precondition 继续；rollback 不算 winner。
- 上述每个 deterministic fixture 必须以 barrier 强制 overlap、在测试时限内无 deadlock，并验证
  每个 predecessor 最多一个 successor。仅真实 serialization/deadlock exception 可另测为
  `RETRYABLE_CONFLICT`，其 transaction 的 command/Decision/child/effect 写入必须全部为 0。

rollback tests 必须在真实 PostgreSQL outer transaction 中保存所有相关 rows/counts 前态；
异常或调用方主动 rollback 后，用新 session 验证旧 Decision/source/assignment 仍为 pending、
全部旧 supporting-subject ER Decisions 仍 active、Entity 仍指向旧 canonical、没有 replacement ER
Decision、D2/new target/部分 effect。service
返回不能代替 outer commit durability proof。cancellation rollback 还必须验证没有 cancelled
Decision，旧 source/assignments 未 superseded，旧 successor proposals 仍由 pending parent 持有。

target-slot positive test 固定为 `A -> {B, new:T}`：D1 的完整 partition 至少包含一个
`resolved -> T` 和一个 pending projection，D1 successor slots=2、new CanonicalEntity 增量=0；
D2 补全同一 target set 后 successor slots 再新增2、new CanonicalEntity 增量=1，D2 resolved
assignments 全部引用 D2 slots。D1 slots 及 payload 不变，只有 D1 source/assignments lifecycle
按 contract superseded。

Direct SQL negative suite 不能只做 INSERT：必须逐表尝试 payload/FK/predecessor UPDATE 与
DELETE，尝试 single-statement rootless cycle、incomplete normal correction、cancelled Decision
带 child、cross-source target slot、assignment target/ER mismatch、merge/split 错误 successor
cardinality；还要分别尝试 pending new slot 带 target UUID、applied new slot 缺 target UUID、
non-NULL Entity pointer 脱离 assignment 的裸 rewrite、applied assignment 的 1:N replacement set
为空/缺 member/多 member、previous ER 未 supersede、new ER 未引用 assignment、cross-subject
predecessor、subject/entity/canonical/snapshot 或 Entity OLD/NEW pointer 不匹配。普通非 evolution
ER supersede 与 initial NULL -> non-NULL pointer
继续按既有 contract 测试，不得被 P3 trigger 误拒绝。对 constraint triggers 在 transaction 内先
确认 statement 可暂存，再断言 COMMIT 失败且新 session 只见 transaction 前 logical rows/state。

migration tests 只允许验证 5.0 的 frozen `0072` path：

- empty old tables：`0070 -> old 0071 -> corrective 0072 -> old 0071 -> 0070`，每一跳验证
  revision、single head、exact schema objects；同时生成 offline upgrade/downgrade SQL，并在匹配
  revision 的 disposable PostgreSQL 实际执行两份 artifact 后验证同样结果，不能只检查文本。
- empty connected/offline upgrade 与 downgrade 都必须先证明 maintenance quiescence，并从 SQL/log
  证明按 5.0 固定总序对九张受影响表取得 `ACCESS EXCLUSIVE ... NOWAIT` lock，之后才执行
  database-side gate 和第一条 DDL。双连接 busy fixture 分别让另一连接持有 library、Canonical、
  Entity、ER 或任一 evolution table 的 conflicting lock；migration 必须在第一条 DDL 前以
  `P3_1_0072_MAINTENANCE_LOCK_UNAVAILABLE` fail closed、零 schema/revision/data change，且不得等待或 deadlock。
  conflicting session 结束并重新证明 quiescence 后才允许在新 transaction 重试；测试不得把“等待 concurrent
  writer 后继续 migration”当作受支持路径。
- 五张 old-`0071` audit tables **逐表参数化**：每个 case 指定一张 distinguished nonempty table，
  并只创建满足其 immediate FK 所必需的最小 parent closure；不得声称 child table 可以在合法 schema
  中单独非空。`0071 -> 0072` 必须得到 `P3_1_0072_NONEMPTY_LEGACY_AUDIT`，revision、schema 与
  每行数据均保持前态；同时静态断言 preflight 的 lock/check SQL 明确逐一引用五张表，避免 parent
  先命中导致 child predicate 从未被覆盖。禁止用 disabled triggers/superuser corruption 伪造 fixture，
  也禁止猜测 backfill。
- 五张 target audit tables 使用相同的 distinguished-table + minimum-FK-valid-parent-closure matrix；
  `0072 -> 0071` 必须得到 `P3_1_0072_NONEMPTY_TARGET_AUDIT`，revision、schema 与每行数据均保持
  前态，并静态断言五张表均被 lock/check SQL 引用；禁止静默丢 audit。
- 两个 nonempty gate 都必须分别走 connected Alembic 和已生成 offline SQL artifact；offline SQL
  必须先持有全部九表 locks，再在第一条 DDL 前自行查询五张 audit tables 并 raise 同一 stable error；使用
  fail-fast client，事务回滚后验证 revision/schema/data。
- production/private 是否已有 `0071` rows 未知；未来获准执行前由 migration 自身的相同
  preflight 判定，不得以 CI、部署记录缺失或人工假设替代。

未来 implementation 的 completion gate 必须逐项记录 command、exit code、pass/fail/skip count；
不得以 targeted tests 代替 regression/static/migration evidence：

| Gate | Required command/evidence |
| --- | --- |
| P3.1 targeted | `python -m pytest -q tests/test_p3_1_canonical_entity_evolution.py tests/test_p3_1_canonical_entity_evolution_pg.py tests/test_p3_1_canonical_entity_evolution_migration.py` |
| P1 regression | `python -m pytest -q tests/test_p1_1_canonical_entity_models.py tests/test_p1_2_canonical_entity_resolution.py tests/test_p1_2_canonical_entity_resolution_pg.py tests/test_p1_canonical_entity_acceptance.py` |
| P2 regression | `python -m pytest -q tests/test_p2_2_fact_foundation.py tests/test_p2_2_predicate_readiness_migration.py tests/test_p2_2_predicate_resolution_policy.py tests/test_p2_2_predicate_resolution_policy_migration.py tests/test_p2_3_graph_relation_fact_resolution.py tests/test_p2_4_fact_lifecycle.py tests/test_p2_5_raw_claim_fact_resolution.py` |
| Ruff | `python -m ruff check app tests alembic` |
| Compile | `python -m compileall -q app tests alembic` |
| Alembic head | `python -m alembic heads`，输出必须恰有 `0072 (head)`。 |
| Offline migration | `python -m alembic upgrade 0071:0072 --sql` 与 `python -m alembic downgrade 0072:0071 --sql` 均生成完整可执行 artifact；随后按 5.0 用 fail-fast client 在匹配起始 revision 的 disposable PostgreSQL 执行。 |
| Runtime migration | disposable PostgreSQL 实际执行 `0070 -> old 0071 -> 0072 -> old 0071 -> 0070`，并验证每跳 revision/schema/data 与 nonempty fail-closed matrix。 |

上述三个 future P3.1 test paths 是冻结的交付入口；implementation 可在其中拆分 fixtures，但不能删掉
任一入口或把不存在的文件算作 skip。只有需要真实 PostgreSQL 的 cases 可因 DSN 缺失按第 14 节
skip；ruff、compileall、Alembic heads、offline SQL generation 和非 PostgreSQL unit regressions 仍须运行。

可稳定自动化的 service/schema 行为使用 T2 strict RED -> GREEN。当前文档任务本身是 T0：
只需要文档 read-back、contradiction search 和 diff whitespace verification，不新增测试代码。

## 14. M. Known Limitations and PostgreSQL Runtime Requirements

`VECTOR_KB_PG_TEST_DSN` 当前未配置。以下只能在 implementation 获准且提供 disposable
PostgreSQL 后验证，当前不能声称 runtime PASS：

```text
composite FK enforcement
partial unique index enforcement
transaction advisory lock ordering and contention
true concurrent overlapping merge/split/reassignment
outer transaction rollback
corrective 0072 forward/downgrade path
```

`historical_only` 的 repository test-double read contract 不依赖 PostgreSQL，可以在后续 implementation
unit suite 中验证。P3.1 没有获准创建该 reserved terminal state 的 writer，因此合法 writer 的
end-to-end path 是阶段性 **out of scope**，不是 PostgreSQL 环境缺失；在未来 producer 设计另行
冻结前不得把它列为本阶段待跑的 runtime integration。

对应 PostgreSQL integration tests 必须在 disposable DSN 下显式运行；在该环境不可用时，
报告必须写为：

```text
SKIPPED — PostgreSQL runtime integration unavailable
```

`0071` immutable 结论已经由 CI execution 证据闭合。仍未知的是 production/private database
是否执行过 `0071` 及其五张 audit tables 是否为空；这不重新打开 rewrite 分支，只决定未来
获准的 `0072` preflight 是继续还是 fail closed。

GitNexus MCP 当前不可用；P3.0 已记录 `npx gitnexus analyze` 因 `tree-sitter-kotlin`
无法加载 `node-gyp-build` 失败。本设计依赖已记录的 manual source/migration/test fallback；
它不代替未来 implementation 前的 symbol-level impact analysis。

## 15. Amendment Acceptance and Stop Condition

本轮修订已经明确以下 contract，等待人工复审：

```text
A. explicit existing merge survivor semantics
B. fail-closed split and complete Entity projection partition semantics
C. exact per-operation Command Identity JSON and alias-key rejection
D. complete predecessor-status x new-result matrix and pending cancellation protocol
E. enforceable composite lineage FKs plus trigger/service invariant boundary
F. immutable command-root plus append-only evolution decision, source, successor, and assignment schema
G. four-state current resolver contract, strictly separate from CanonicalEntity operational status
H. Entity reassignment plus EntityResolutionDecision supersede transaction
I. GraphGovernance projection-level mutual exclusion
J. shared lock API and fixed total order
K. typed REUSED effective-outcome contract and replay behavior
L. cycle prevention and outer transaction ownership
M. conditional implementation boundary, expanded test matrix, and PostgreSQL runtime gap
```

仍未闭合：

```text
0071 production/private deployment and table cardinality = UNKNOWN
human design review = PENDING
PostgreSQL runtime integration = UNAVAILABLE in the current environment
```

因此最终状态保持 `P3.1 design = AMENDMENT REQUIRED`。下一步只能是人工复审；不得进入
implementation，不得修改/创建 `0071` 或 `0072`，不得
提交、推送或部署。未来即使人工复审通过，implementation 仍须单独授权，并重新检查本文件、
P3.0 真源、Alembic head、Git worktree 和每个待修改 symbol 的 impact。P3.2、P3.3、P3.4、
Publication、Retrieval、RawClaim promotion 和历史 Fact reconciliation 继续不获授权。
