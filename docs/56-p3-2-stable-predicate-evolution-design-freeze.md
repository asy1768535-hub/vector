# P3.2 StablePredicateIdentity Evolution Design Freeze

Status: **P3.2 design = FROZEN**

This document is the frozen design source for P3.2. It derives its constraints
from [P3.0](./54-p3-identity-evolution-read-only-baseline.md), the frozen
[P3.1 design](./55-p3-1-canonical-entity-evolution-design-freeze.md), and the
P2 fact contract in [51-p2-logical-fact-fact-assertion-design.md](./51-p2-logical-fact-fact-assertion-design.md).

Independent design review approved pre-freeze blob
`8e3cf704a38318925b6645991f6b10dcdaf1883b`. This checkpoint authorizes
P3.2 implementation and migration `0073` only. It authorizes no database
execution, deployment, or change to P3.3.

```text
P3.0 = SEALED
P3.1 = implemented and locally validated; production deployment not performed
P3.2 design = FROZEN
P3.2 implementation / migration 0073 = AUTHORIZED
P3.3 / P3.4 / Publication / Retrieval / current-Fact lifecycle = NOT AUTHORIZED
```

## 1. Goal and Boundary

P3.2 makes the current meaning of a `StablePredicateIdentity` evolve safely.
It supports an explicit predicate merge, predicate split, and a controlled
`StablePredicateMapping` reassignment. It changes only how future Fact
Resolution selects a current predicate.

P3.2 must preserve this distinction:

```text
StablePredicateIdentity.resolution_status
    = whether this predicate's own P2 policy is ready/rejected/pending

Predicate evolution status
    = whether this historical predicate has one deterministic current meaning
```

The two statuses are independent. A predicate whose P2 status is `resolved`
may be `forked` by a later split. A predicate whose evolution resolves to a
single successor may still be P2 `pending` because that successor has no valid
resolution policy.

P3.2 must not:

- update `LogicalFact.stable_predicate_identity_id`;
- update a historical Fact fingerprint, FactAssertion, KnowledgeRelation, or
  RelationEvidence bridge;
- rerun Fact Lifecycle, Conflict, Publication, Retrieval, RawClaim promotion,
  or any P3.3 reconciliation;
- infer a merge, split partition, mapping target, policy compatibility, or
  successor from a name, embedding, fuzzy match, latest row, first row, or LLM;
- reuse CanonicalEntity evolution tables or GraphGovernance Entity merge.

The P3.3 input is an append-only P3.2 predicate lineage plus unchanged
historical Facts. P3.2 is not allowed to create the P3.3 reconciliation bridge.

## 2. Current Evidence

The current implementation establishes the following facts.

| Owner | Current behavior | P3.2 consequence |
| --- | --- | --- |
| `StablePredicateIdentity` in `app/models/fact_foundation.py` | Stores namespace, key, contract version, temporal class, identity-policy version, P2 resolution status, and policy. Its scoped namespace/key/version identity is unique. | These content fields are historical identity data. P3.2 does not edit them in place. |
| `StablePredicateMapping` | Has a partial unique index: one active mapping per `(library_id, relation_type_id)`. It already has `active`, `superseded`, and `rejected` operational states. | A reassignment must supersede the old active mapping before inserting one replacement mapping. |
| `graph_relation_fact_resolution._active_predicates` | Reads active mappings for a relation type, then loads the mapped predicate. | This is the P3.2 current-predicate admission point. It must become evolution-aware. |
| `preflight_graph_relation_candidate_fact` | Stops with a P2-compatible pending/rejected `FactResolutionDecision` before Fact writes when mapping or predicate readiness is not usable. | A `forked`, `pending`, or `historical_only` predicate must take this no-Fact path. |
| `LogicalFact.stable_predicate_identity_id` | Has a same-library `RESTRICT` foreign key and is used by lifecycle and conflict grouping. | Historical Fact predicate identity cannot be rewritten; P3.3 later derives current facts. |
| `FactResolutionDecision.stable_predicate_identity_id` | Is an audit snapshot for the decision at that time. | Old decisions remain unchanged. A later attempt creates/supersedes a normal P2 decision, not a rewritten snapshot. |

The P3.1 shared lock contract already reserves scope `20 stable_predicate` and
scope `30 logical_fact`. P3.2 must extend that shared API, not introduce a
second advisory-lock hash scheme.

## 3. Proposed Vocabulary

### 3.1 Operation kinds

```text
merge       A + B -> explicitly named survivor
split       A -> B + C + ... (at least two successor slots)
reassign    one StablePredicateMapping changes from predicate A to predicate B
```

`reassign` is a mapping correction, not a predicate merge and not a Fact
reconciliation. It has no predicate-successor edge.

### 3.2 Current-predicate resolver results

`resolve_current_stable_predicate_identity()` returns exactly one of:

```text
resolved        exactly one persisted current predicate was selected
forked          more than one successor exists and no mapping context selects one
pending         a current evolution or mapping assignment is incomplete
historical_only no eligible current predicate may be selected
```

`historical_only` is returned only from an explicit persisted terminal source
state. P3.2 merge, split, and reassign do not create that terminal state; the
value is reserved so the resolver can read a future governed retirement without
changing its return type. Until such a writer is separately designed, the
P3.2 service and migration triggers reject attempts by these three operations
to create it. A predicate's P2 `resolution_status` (including `rejected`) and
the absence of a successor never imply `historical_only`.

This is a read-model compatibility value only. P3.2 adds no retirement command,
endpoint, role, table, or writer branch for it; implementation must not expand
this reserved enum value into a fourth workflow.

The `historical_only` resolver branch is tested in P3.2 with an isolated
repository/read-model fixture only. It is not a claimed PostgreSQL writer path
or end-to-end producer; bypassing migration triggers to manufacture one is not
an acceptance test.

The resolver accepts an optional persisted mapping context:

```text
predicate_id
mapping_id optional
relation_type_id optional
```

For a completed split, an unscoped call for old `A` returns `forked`. A call
with the exact historical mapping row and its persisted resolved assignment may
return `resolved(B)` only after the split head is `applied` and the complete
mapping partition is resolved. While a current split head has *any* pending
mapping assignment, every context for that split returns `pending`, including a
mapping such as `R1` whose individual assignment already names `B`. This is an
atomic admission rule: a partial partition is an intent under correction, not
a partially usable current predicate. The resolver never chooses the first or
latest successor.

`resolved` only describes lineage. Fact Resolution separately checks that the
selected predicate has P2 `resolution_status == resolved`, an exactly one
active mapping for the relation type, and a valid `resolution_policy`.

### 3.3 Evolution decision lifecycle

P3.2 follows P3.1's two-layer result vocabulary:

```text
APPLIED | PENDING | REUSED | REJECTED | STALE_OPERATION |
RETRYABLE_CONFLICT | CANCELLED
```

Persisted decision values are:

```text
evaluated_outcome = pending | applied | rejected | stale | cancelled
lifecycle_status  = pending | applied | rejected | stale | cancelled | superseded
```

`cancelled` is an explicit, audited terminal control decision. It is the only
way to release an old pending intent without applying it. `abandoned` is not a
second synonym and is not introduced.

## 4. Merge, Split, and Mapping Semantics

### 4.0 Locked current-leaf admission

Every existing predicate named by a merge, split, or reassignment is an
explicit identity, not a request to follow lineage. After acquiring the shared
scope-20 locks, the service resolves every supplied source, merge survivor,
existing split slot, and reassignment target. Each must return `resolved` with
the same UUID that was supplied. `forked`, `pending`, `historical_only`, or a
different resolved UUID returns
`STALE_OPERATION(predicate_current_identity_changed)` before a command root or
any child row is inserted.

This means a merge survivor, an existing split successor, and a reassignment
target must all be locked current leaves. The service must never silently
redirect a request for historical `B` to `B`'s successor. A new split target is
the only exception because it has no historical row yet; it is created as a new
current leaf only by the applied transaction described below.

### 4.1 Predicate merge

For `A + B -> A`, the survivor `A` must be supplied explicitly. There is no
newest, first, highest-confidence, or lexical survivor rule.

Only retired sources have successor rows. In this example `B -> A` is the
lineage edge; `A` does not receive a self-edge. All active mappings that point
to `B` receive a resolved mapping assignment to `A`. Active mappings already
pointing to survivor `A` remain unchanged.

An applied merge requires a deterministic compatibility snapshot showing that
the source and survivor have identical canonical values for:

```text
temporal_class
identity_policy_version
resolution_policy (RFC 8785 canonical JSON)
```

Different namespace, key, or contract-version strings are not silently treated
as equal; they are the reason an explicit merge may be requested. They must be
recorded in the evidence snapshot. If any required policy value differs, the
command is rejected with `predicate_policy_incompatible`; no target policy is
copied or edited.

### 4.2 Predicate split

`A -> {B, C, ...}` requires at least two explicit successor slots. Each slot is
either an existing predicate ID or a fully specified new target definition.
For a new target, the definition is the complete normalized target object in
section 5.2, including all immutable P2 identity and policy fields and a valid
resolved policy. The service may create a new target only while atomically
applying a fully resolved decision. A pending decision does not create a
speculative `StablePredicateIdentity` row. It does persist immutable successor
proposal rows with a null target predicate ID, so a correction or cancellation
retains the complete target intent.

Every active mapping of `A` must be represented in the decision's mapping
partition as either:

```text
resolved -> one declared successor slot
pending  -> no target
```

An omitted mapping is invalid. A pending mapping blocks future Fact Resolution
for the whole split, including mappings whose own assignment is already
`resolved`. The old active mappings remain as historical ontology projections,
but the current resolver returns `pending` until the split partition is fully
applied. Therefore no new Fact, Assertion, KnowledgeRelation bridge, or
RelationEvidence bridge is written from any mapping in that incomplete split.

For example, a valid correction sequence is:

```text
D1: A -> {B, C}; mapping R1 -> B; mapping R2 -> pending
D2: A -> {B, C}; mapping R1 -> B; mapping R2 -> C
```

`D2` supersedes `D1` within the same command root. It must not shrink the
target set to `A -> {B}` and must not silently substitute a different target.

### 4.3 Mapping reassignment

`reassign` changes one named current mapping from predicate `A` to named
predicate `B`. It is valid only when:

- the supplied mapping is the currently active row for its relation type;
- its stored predicate is exactly `A`;
- `B` is lineage-`resolved` and P2-ready for that mapping context;
- `A != B`; and
- no live predicate-evolution or mapping-assignment slot already owns that
  mapping/source context.

The old mapping becomes `superseded` with its existing lifecycle fields, one
new mapping becomes active, and an append-only P3.2 assignment records the
old/new pair. The mapping update is a controlled current-projection lifecycle
transition, never a naked bulk foreign-key rewrite.

## 5. Command and Decision Identity

### 5.0 Request envelope and authorization

P3.2 reuses the existing authenticated application boundary. Its exact mutation
permission is the existing library-level `admin` action resolved through
`authorize_library_management()`; it creates no `predicate_evolution`,
`cancel_pending`, or other Casbin action, no role, P3.2-specific administrator
bypass, or alternate direct-SQL interface. It preserves the helper's two
existing modes exactly: with organization authorization enabled, both the
organization and membership must have `status=active`, and either the
membership's `organization_admin` role or Casbin `admin` must allow management;
with it disabled,
`User.is_superuser` or Casbin `admin` is required. P3.2 adds no third allow
path. Prepare, mutation, replay lookup, command
inspection, and cancellation all require that same management authorization
before library ownership, command/root, identity-collision, Predicate, mapping,
or Decision lookup can disclose existence. The internal current-predicate
resolver remains a service read under its caller's already-authorized Fact
Resolution flow and does not expose evolution audit records directly.

A normal merge/split/reassign request contains exactly:

```text
library_id
idempotency_key
operation-specific command fields from section 5.1
expected_precondition_fingerprint
expected_predecessor_decision_id = null for a new root; required for correction/retry
requested_effect = stage | apply
confidence
evidence_refs optional; default []
method
reason_code + reason_text
request_id
```

`actor_type` and `actor_id` come from the authenticated server context and are
persisted with the Decision; they are not caller-selectable fields. For a
cookie/session credential they are exactly `actor_type=user` and
`actor_id=User.id`. For an API-key credential they are exactly
`actor_type=service` and
`actor_id=credential_api_key_audit_identity(user).api_key_id`. The API-key
authenticator binds a request-local, server-owned
`CredentialApiKeyAuditIdentity` containing exactly `api_key_id` and
`organization_id` in both authorization modes. This is a separate attribute and
type from `CredentialOrganizationScope`: it must not call
`bind_credential_organization()` when organization authorization is disabled,
change `credential_organization_scope(user)`, or be consumed by existing graph
publication, graph catalog, or other authorization paths. Only the P3.2
mutation/audit boundary consumes it. Its organization must own the requested
library, and the backing `User` must still pass
`authorize_library_management()`. A missing API-key audit identity or any other
service-principal shape is rejected. It must not fall back to `User.id` when
organization authorization is disabled. `actor_type=service` is an audit
identity, not an authorization bypass.
Cancellation contains exactly `library_id`, `command_id`, `original_idempotency_key`,
`expected_pending_decision_id`, `evidence_refs`, `reason_code`, `reason_text`,
and `request_id`; its method and requested effect are server-fixed to
`authorized_cancellation` and `cancel`. It requires the same existing
library-level `admin` action and no separate permission literal.

The application boundary must prove that every supplied command Predicate,
mapping, relation type, existing target, and referenced predecessor belongs to
the authorized library. An unauthenticated, unauthorized, cross-library, or
caller-forged actor request follows the existing application error contract,
returns no root/Decision/target identifiers, and writes nothing. Exact replay is
an idempotency behavior after this authorization gate, never an authorization
bypass.

### 5.0.1 Canonicalization shared by every identity

Command, Decision, target-specification, and precondition identities use the
same service-owned pipeline: validate the exact schema, domain-normalize, encode
with RFC 8785 JCS as UTF-8 with no BOM or trailing newline, then take lowercase
SHA-256. PostgreSQL `jsonb::text`, default `json.dumps`/`JSON.stringify`, and
insertion order are not fingerprint inputs.

Before JCS, every UUID is a lowercase hyphenated string, every free string is
Unicode NFC, duplicate object keys and undeclared keys are rejected, and hashes
are exactly 64 lowercase hexadecimal characters. The only open-content objects
are the existing P2 `resolution_policy`, `partition_basis_snapshot`, and each
application evidence-reference object; their owning validators still enforce
the bounds in section 6.0. `confidence` is null or an exact base-10 decimal in
`0..1` with at most six fractional digits; exponent form, binary floats,
negative zero, NaN, and Infinity are rejected. Arrays declared below as sets are
sorted by their frozen rule and reject duplicates. Other arrays retain their
declared business order. P3.2 inherits P3.1's exact plain-decimal normalization
for `confidence`, including removal of insignificant trailing zeroes before JCS.
The request parser rejects a UTF-8 BOM rather than silently stripping it. JSON
nesting depth is at most 32.

### 5.1 Command Identity

Command Identity excludes reason, evidence, confidence, mapping partition,
expected/observed preconditions, and actor/request audit. Those belong to a
Decision version so that a pending split can be corrected within the same root.

Merge uses exactly this object:

```json
{
  "schema": "p3_2_stable_predicate_evolution_command_v1",
  "operation": "merge",
  "library_id": "<uuid>",
  "source_predicate_ids": ["<retired-source-uuid>", "<retired-source-uuid>"],
  "survivor_predicate_id": "<uuid>"
}
```

`source_predicate_ids` contains only predicates that will become historical;
it never contains the survivor. It is a distinct set with at least one member,
sorted by canonical UUID text. The survivor must differ from every source.
Thus `A+B->A` stores `source_predicate_ids=[B]`; changing the survivor creates a
different Command Identity.

Split uses exactly this object:

```json
{
  "schema": "p3_2_stable_predicate_evolution_command_v1",
  "operation": "split",
  "library_id": "<uuid>",
  "source_predicate_id": "<uuid>",
  "successor_slots": [
    {"kind": "existing", "predicate_id": "<uuid>"},
    {
      "kind": "new",
      "predicate_id": "<deterministic-final-uuid>",
      "target_spec_fingerprint": "<sha256>"
    }
  ]
}
```

`successor_slots` is a distinct set with at least two members. Domain sorting
is `(kind_rank, predicate_id)`, where `existing=0`, `new=1`, and
`predicate_id` is compared as canonical UUID text. Duplicate predicate IDs,
duplicate existing slots, duplicate new target fingerprints, and any slot equal
to the split source are rejected before hashing. RFC 8785 sorts object members;
this domain rule, not RFC 8785, sorts the array.

Reassign uses exactly this object:

```json
{
  "schema": "p3_2_stable_predicate_evolution_command_v1",
  "operation": "reassign",
  "library_id": "<uuid>",
  "mapping_id": "<uuid>",
  "from_predicate_id": "<uuid>",
  "to_predicate_id": "<uuid>"
}
```

The named mapping and both predicate IDs are identity fields; `from` and `to`
must differ. Cancellation does not create a second command or a cancel Command
Identity. It is a control Decision appended to the original root.

Within one library:

- same idempotency key + different command identity:
  `REJECTED(idempotency_key_conflict)`;
- same command identity + different idempotency key:
  `REJECTED(command_identity_alias_key)`;
- exact command identity and exact Decision payload: `REUSED`, including
  `reused_decision_id`, `effective_outcome`, `current_decision_id`, and
  `current_decision_status`;
- a caller must not interpret `REUSED` alone as an applied effect.

`effective_outcome` is `APPLIED`, `PENDING`, `REJECTED`, or `STALE`. Only
`REUSED(APPLIED)` whose reused Decision is also the current applied head means
an already-applied effect. Cancellation replay returns idempotent `CANCELLED`.

### 5.2 New split target object and final UUID

Every `kind: new` slot contains this complete object in its Decision payload and
successor row:

```json
{
  "schema": "p3_2_stable_predicate_target_v1",
  "namespace": "<bounded-string>",
  "key": "<bounded-string>",
  "contract_version": "<bounded-string>",
  "temporal_class": "static_fact | state_fact | measurement_slot | event_fact",
  "identity_policy_version": "<bounded-string>",
  "resolution_status": "resolved",
  "resolution_policy": {"<RFC-8785-normalized-P2-policy>": "..."}
}
```

`target_spec_fingerprint` is SHA-256 of exactly that object. The final
`StablePredicateIdentity.id` is deterministic and is also the scope-20 lock key:

```text
identity_key_json = RFC8785({
  "contract_version": contract_version,
  "key": key,
  "library_id": library_id,
  "namespace": namespace
})

target_predicate_id = UUIDv5(
  6ba7b811-9dad-11d1-80b4-00c04fd430c8,
  "urn:vector-kb:p3.2:stable-predicate-identity:v1:"
  + SHA256(identity_key_json)
)
```

There is no synthetic reservation UUID and no later random row UUID. A pending
proposal persists `planned_target_predicate_id=target_predicate_id` but creates
no Predicate row. An applied transaction inserts the Predicate with exactly
that ID. If that UUID is occupied by a different scoped identity, the request
fails closed as `target_predicate_uuid_collision`; if the scoped uniqueness key
already exists under any ID, it fails as
`new_target_identity_already_exists` and must be resubmitted as `kind: existing`.
Exact replay is checked before either collision admission.

Two commands proposing the same scoped identity therefore take the same real
Predicate scope-20 lock. A concurrent loser fails fast under section 9. On a
fresh caller retry after the winner commits, lock-time re-read returns
`REJECTED(new_target_identity_already_exists)` with zero writes; after winner
rollback the fresh retry may proceed. The service itself does not sleep or
retry. The existing scoped unique constraint is the final backstop. An
unexpected unique violation becomes `RETRYABLE_CONFLICT` only after the outer
transaction rolls back with no target or lineage rows left.

The Decision and successor copies store both `target_spec_snapshot` and
`target_spec_fingerprint`. Service validation proves JCS/hash agreement before
write; a deferred constraint trigger proves JSONB equality, planned UUID
agreement, and slot membership at commit. PostgreSQL is not claimed to
recompute JCS.

### 5.3 Decision Payload Identity

Every normal Decision fingerprint covers exactly this common envelope:

```json
{
  "confidence": null,
  "evidence_refs": [],
  "expected_precondition_fingerprint": "<sha256>",
  "method": "<bounded-method>",
  "operation_payload": {},
  "reason_code": "<bounded-code>",
  "reason_text": "<bounded-text>",
  "requested_effect": "stage | apply"
}
```

`requested_effect` is caller-selectable only for a normal Decision and is part
of Decision Payload Identity, never Command Identity. Its deterministic mapping
is:

- `stage`: after authorization, current-head CAS, locked-current-leaf admission,
  and precondition validation succeed, persist `evaluated_outcome=pending` with
  the complete operation-specific proposal and zero Predicate/mapping effect;
- `apply`: persist `applied` only when every frozen apply precondition is
  satisfied. A valid split with any pending assignment persists `pending` with
  zero effect; merge/reassign have no equivalent incomplete-partition fallback
  and therefore evaluate to `applied`, `rejected`, or `stale`;
- authorization, envelope, current-leaf, collision, and stale-admission failures
  keep their earlier precedence and cannot be converted to `pending` merely by
  selecting `stage`.

Changing `stage` to `apply` is a different Decision payload under the same
Command Identity. It is therefore a same-root correction and requires the
actual current pending Decision ID as predecessor CAS. No server default is
allowed; omission or any other literal is `REJECTED(invalid_envelope)` before
root lookup or write.

For merge, `operation_payload` is exactly:

```json
{
  "mapping_assignments": [
    {
      "from_predicate_id": "<source-uuid>",
      "mapping_id": "<uuid>",
      "partition_basis_snapshot": {},
      "reason_code": "<bounded-code>",
      "state": "resolved",
      "target_ref": {"kind": "existing", "predicate_id": "<survivor-uuid>"}
    }
  ],
  "policy_compatibility": [
    {
      "contract_version": "<bounded-string>",
      "identity_policy_version": "<bounded-string>",
      "key": "<bounded-string>",
      "namespace": "<bounded-string>",
      "predicate_id": "<uuid>",
      "resolution_policy": {},
      "temporal_class": "<temporal-class>"
    }
  ],
  "survivor_predicate_id": "<uuid>"
}
```

`mapping_assignments` contains every active mapping of every retired source and
is sorted by `mapping_id`. `policy_compatibility` contains the survivor and all
retired sources, sorted by `predicate_id`; it is the exact comparison snapshot
required by section 4.1.

For split, `operation_payload` is exactly:

```json
{
  "mapping_assignments": [
    {
      "from_predicate_id": "<source-uuid>",
      "mapping_id": "<uuid>",
      "partition_basis_snapshot": {},
      "reason_code": "<bounded-code>",
      "state": "resolved",
      "target_ref": {"kind": "new", "predicate_id": "<deterministic-final-uuid>"}
    },
    {
      "from_predicate_id": "<source-uuid>",
      "mapping_id": "<uuid>",
      "partition_basis_snapshot": {},
      "reason_code": "<bounded-code>",
      "state": "pending",
      "target_ref": null
    }
  ],
  "successor_slots": [
    {"kind": "existing", "predicate_id": "<uuid>"},
    {
      "kind": "new",
      "predicate_id": "<deterministic-final-uuid>",
      "target_spec_fingerprint": "<sha256>",
      "target_spec_snapshot": {
        "schema": "p3_2_stable_predicate_target_v1",
        "namespace": "<bounded-string>",
        "key": "<bounded-string>",
        "contract_version": "<bounded-string>",
        "temporal_class": "<temporal-class>",
        "identity_policy_version": "<bounded-string>",
        "resolution_status": "resolved",
        "resolution_policy": {}
      }
    }
  ]
}
```

A pending assignment has `target_ref=null`; a resolved assignment has exactly
the two-key target reference shown above, with `kind=existing|new` matching the
declared successor slot that has the same Predicate UUID. Assignments are sorted
by `mapping_id`; successor slots use the section 5.1 mixed-slot rule. The slot
set and every full new-target specification must agree with the Command
Identity.

For reassign, `operation_payload` is exactly:

```json
{
  "mapping_assignment": {
    "from_predicate_id": "<uuid>",
    "mapping_id": "<uuid>",
    "partition_basis_snapshot": {},
    "reason_code": "<bounded-code>",
    "state": "resolved",
    "target_ref": {"kind": "existing", "predicate_id": "<to-uuid>"}
  }
}
```

For cancellation, the common envelope is retained but `confidence=null`,
`method="authorized_cancellation"`, `requested_effect="cancel"`, and
`operation_payload` is exactly:

```json
{
  "control_kind": "cancel_pending",
  "expected_pending_decision_id": "<uuid>"
}
```

Cancellation has no source/successor/assignment proposal in its payload and
creates no child row. `evidence_refs` is a set sorted by each element's JCS
bytes after duplicate rejection. P3.2 deliberately retains P3.1's open-content
stable-reference object rather than inventing a second evidence taxonomy. Its
validator requires each element to be a nonempty bounded canonical JSON object
containing only a minimal stable locator; raw evidence, secrets, and free-form
document bodies are forbidden. The other arrays use only the explicit sort
rules above. Observed preconditions, evaluated/lifecycle outcomes, generated row
IDs, timestamps, actor/request audit, and insertion order are excluded from
Decision Payload Identity.

### 5.4 Expected and observed precondition snapshot

The prepare/read API issues an opaque expected fingerprint; callers may only
round-trip it and may not submit a snapshot. Prepare and lock-time evaluation
hash exactly this object:

```json
{
  "schema": "p3_2_stable_predicate_precondition_v1",
  "library_id": "<uuid>",
  "operation": "merge | split | reassign",
  "command_context": null,
  "predicate_states": [
    {
      "contract_version": "<bounded-string>",
      "identity_policy_version": "<bounded-string>",
      "key": "<bounded-string>",
      "lineage_status": "resolved | forked | pending | historical_only",
      "namespace": "<bounded-string>",
      "p2_resolution_status": "resolved | pending | ambiguous | rejected",
      "predicate_id": "<uuid>",
      "resolution_policy_fingerprint": "<sha256-or-null>",
      "resolved_predicate_id": "<uuid-or-null>",
      "temporal_class": "<temporal-class>"
    }
  ],
  "mapping_states": [
    {
      "evolution_assignment_id": "<uuid-or-null>",
      "mapping_id": "<uuid>",
      "mapping_status": "active | superseded | rejected",
      "predicate_id": "<uuid>",
      "relation_type_id": "<uuid>"
    }
  ],
  "source_slots": [
    {
      "current_transition": null,
      "source_predicate_id": "<uuid>"
    }
  ],
  "mapping_slots": [
    {
      "current_assignment": null,
      "mapping_id": "<uuid>"
    }
  ],
  "new_target_states": [
    {
      "planned_target_predicate_id": "<deterministic-final-uuid>",
      "planned_uuid_occupant": null,
      "scoped_identity": {
        "contract_version": "<bounded-string>",
        "key": "<bounded-string>",
        "namespace": "<bounded-string>"
      },
      "scoped_identity_existing_predicate_id": null,
      "target_spec_fingerprint": "<sha256>"
    }
  ]
}
```

When non-null, `command_context` is exactly
`{"command_id":"<uuid>","current_decision_id":"<uuid>"}`. A non-null
`current_transition` is exactly
`{"command_id":"<uuid>","decision_id":"<uuid>","source_transition_id":"<uuid>","status":"pending | applied | historical_only"}`.
A non-null `current_assignment` is exactly
`{"assignment_id":"<uuid>","command_id":"<uuid>","decision_id":"<uuid>","state":"pending | resolved"}`.
No object accepts extra keys.

Membership is operation-specific and complete:

- merge: predicate states are the survivor plus every retired source; mapping
  states are every active mapping of retired sources; source slots are every
  retired source; mapping slots correspond one-for-one with those mappings;
  new-target states are empty;
- split: predicate states are the source plus every existing successor; mapping
  states are every active source mapping; the source-slot set contains the
  source; mapping slots correspond one-for-one with those mappings; new-target
  states contain every new slot;
- reassign: predicate states contain `from` and `to`; mapping states contain
  exactly the named mapping; source slots contain `from` and `to` so an
  overlapping predicate evolution is observed; mapping slots contain the named
  mapping; new-target states are empty.

`predicate_states` is sorted by `predicate_id`; `mapping_states` and
`mapping_slots` by `mapping_id`; `source_slots` by `source_predicate_id`; and
`new_target_states` by `planned_target_predicate_id`, all as canonical UUID
text. Equal keys are rejected. `resolution_policy_fingerprint` is null only
when the stored policy is null, otherwise it hashes the exact RFC 8785 policy object.
`scoped_identity_existing_predicate_id` reports the row found by the
`(library_id, namespace, key, contract_version)` uniqueness lookup and is null
when none exists. A non-null `planned_uuid_occupant` is exactly
`{"contract_version":"<bounded-string>","key":"<bounded-string>","library_id":"<uuid>","namespace":"<bounded-string>","predicate_id":"<planned-uuid>"}`
for the row found by primary-key lookup; it is null when that UUID is free.
These two observations independently detect scoped-identity reuse and UUID
occupation, including a globally occupied UUID from another library.

For a new root `command_context=null`; for a correction it names the exact root
and actual current head. The mutation path re-runs the same observation after
the complete lock set is held. Exact Decision replay is checked first. A
current-leaf, live-slot, collision, or lock-set-membership change is a pre-root
admission failure with zero writes. If admission remains valid but the opaque
expected hash differs from the fresh observed hash, the section 7 matrix decides
whether to append an audit-only `stale` Decision or return without writes.

Cancellation has no separately prepared snapshot. Its
`expected_precondition_fingerprint` equals the pending predecessor's stored
`observed_precondition_fingerprint`; its `expected_pending_decision_id` is the
independent CAS. Under the full original lock set, the service recomputes the
same operation-specific object with `command_context` naming that predecessor
and stores the fresh observed fingerprint. Expected/observed equality is not a
cancellation gate; authorization, head CAS, complete lock membership, and
proposal-chain integrity are the gate. This makes correction, stale detection,
exact replay, and cancellation deterministic without trusting caller JSON.

### 5.5 Golden vectors

These are normative UTF-8 JCS bytes with no BOM or trailing newline; their
SHA-256 outputs are lowercase hexadecimal. Implementations must reproduce every
value exactly; reformatting the
displayed JSON is not a new vector. Fixture IDs are `L=00000000-0000-4000-8000-000000000001`,
`A=10000000-0000-4000-8000-000000000001`,
`B=10000000-0000-4000-8000-000000000002`,
`M1=20000000-0000-4000-8000-000000000001`,
`M2=20000000-0000-4000-8000-000000000002`, and
`R1=40000000-0000-4000-8000-000000000001`.

Target specification:

```text
sha256 = 3d7756b5823c55e8c0bbcc3f8e1f91837c268f72497e40fff75d65ab9da6e2d4
{"contract_version":"v1","identity_policy_version":"p2_identity_v1","key":"target-c","namespace":"urn:test:predicate","resolution_policy":{"effective_time_policy":{"source":"none"},"event_temporal_identity_policy":null,"measurement_policy":null,"modality_policy":{"source":"fixed","value":"confirmed"},"object_policy":{"source":"target_entity"},"polarity_policy":{"source":"fixed","value":"affirmed"},"qualifier_policy":{"assertion_bearing":[],"evidence_only":[],"identity_bearing":[]},"schema_version":"p2_v1","temporal_class":"static_fact","valid_time_policy":{"source":"none"}},"resolution_status":"resolved","schema":"p3_2_stable_predicate_target_v1","temporal_class":"static_fact"}
```

Scoped identity input and resulting final Predicate UUID:

```text
sha256 = ab016458be293780453ef9d74b7f1f8f1d496c4d90bda809081cb6b9baaea127
{"contract_version":"v1","key":"target-c","library_id":"00000000-0000-4000-8000-000000000001","namespace":"urn:test:predicate"}
target_predicate_id = b0700668-7622-55ce-9ba5-b62cc7b260c1
```

Command identities:

```text
merge sha256 = 1752568c139591e1d57b3721b70ec286ebb2a3b1a8c77d4ef64b765394c6162f
{"library_id":"00000000-0000-4000-8000-000000000001","operation":"merge","schema":"p3_2_stable_predicate_evolution_command_v1","source_predicate_ids":["10000000-0000-4000-8000-000000000002"],"survivor_predicate_id":"10000000-0000-4000-8000-000000000001"}
split sha256 = d7d34bf1914f9cff25b1d9bebe7f5106803dffe7c3afa487cccd9f661417c86e
{"library_id":"00000000-0000-4000-8000-000000000001","operation":"split","schema":"p3_2_stable_predicate_evolution_command_v1","source_predicate_id":"10000000-0000-4000-8000-000000000001","successor_slots":[{"kind":"existing","predicate_id":"10000000-0000-4000-8000-000000000002"},{"kind":"new","predicate_id":"b0700668-7622-55ce-9ba5-b62cc7b260c1","target_spec_fingerprint":"3d7756b5823c55e8c0bbcc3f8e1f91837c268f72497e40fff75d65ab9da6e2d4"}]}
reassign sha256 = 02d522f3b7b7049a5b18e3a58b9546bc86ab83a6f9c29751d52155f62575078e
{"from_predicate_id":"10000000-0000-4000-8000-000000000001","library_id":"00000000-0000-4000-8000-000000000001","mapping_id":"20000000-0000-4000-8000-000000000001","operation":"reassign","schema":"p3_2_stable_predicate_evolution_command_v1","to_predicate_id":"10000000-0000-4000-8000-000000000002"}
```

Decision Payload identities use
`expected_precondition_fingerprint=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`:

```text
merge sha256 = 68e3d53f169fc0ed9b83c14d20b0bcd678562ed7fafdbda3589964319b02f126
{"confidence":null,"evidence_refs":[],"expected_precondition_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","method":"manual","operation_payload":{"mapping_assignments":[{"from_predicate_id":"10000000-0000-4000-8000-000000000002","mapping_id":"20000000-0000-4000-8000-000000000001","partition_basis_snapshot":{},"reason_code":"merge_survivor","state":"resolved","target_ref":{"kind":"existing","predicate_id":"10000000-0000-4000-8000-000000000001"}}],"policy_compatibility":[{"contract_version":"v1","identity_policy_version":"p2_identity_v1","key":"predicate-a","namespace":"urn:test:predicate","predicate_id":"10000000-0000-4000-8000-000000000001","resolution_policy":{"effective_time_policy":{"source":"none"},"event_temporal_identity_policy":null,"measurement_policy":null,"modality_policy":{"source":"fixed","value":"confirmed"},"object_policy":{"source":"target_entity"},"polarity_policy":{"source":"fixed","value":"affirmed"},"qualifier_policy":{"assertion_bearing":[],"evidence_only":[],"identity_bearing":[]},"schema_version":"p2_v1","temporal_class":"static_fact","valid_time_policy":{"source":"none"}},"temporal_class":"static_fact"},{"contract_version":"v1","identity_policy_version":"p2_identity_v1","key":"predicate-b","namespace":"urn:test:predicate","predicate_id":"10000000-0000-4000-8000-000000000002","resolution_policy":{"effective_time_policy":{"source":"none"},"event_temporal_identity_policy":null,"measurement_policy":null,"modality_policy":{"source":"fixed","value":"confirmed"},"object_policy":{"source":"target_entity"},"polarity_policy":{"source":"fixed","value":"affirmed"},"qualifier_policy":{"assertion_bearing":[],"evidence_only":[],"identity_bearing":[]},"schema_version":"p2_v1","temporal_class":"static_fact","valid_time_policy":{"source":"none"}},"temporal_class":"static_fact"}],"survivor_predicate_id":"10000000-0000-4000-8000-000000000001"},"reason_code":"manual_merge","reason_text":"merge B into A","requested_effect":"apply"}
split sha256 = b76f0d0a3d2ef69c339931f47a486af49dc4dfc6233a0f8e84d5b36158c829f7
{"confidence":null,"evidence_refs":[],"expected_precondition_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","method":"manual","operation_payload":{"mapping_assignments":[{"from_predicate_id":"10000000-0000-4000-8000-000000000001","mapping_id":"20000000-0000-4000-8000-000000000001","partition_basis_snapshot":{},"reason_code":"manual_partition","state":"resolved","target_ref":{"kind":"existing","predicate_id":"10000000-0000-4000-8000-000000000002"}},{"from_predicate_id":"10000000-0000-4000-8000-000000000001","mapping_id":"20000000-0000-4000-8000-000000000002","partition_basis_snapshot":{},"reason_code":"needs_review","state":"pending","target_ref":null}],"successor_slots":[{"kind":"existing","predicate_id":"10000000-0000-4000-8000-000000000002"},{"kind":"new","predicate_id":"b0700668-7622-55ce-9ba5-b62cc7b260c1","target_spec_fingerprint":"3d7756b5823c55e8c0bbcc3f8e1f91837c268f72497e40fff75d65ab9da6e2d4","target_spec_snapshot":{"contract_version":"v1","identity_policy_version":"p2_identity_v1","key":"target-c","namespace":"urn:test:predicate","resolution_policy":{"effective_time_policy":{"source":"none"},"event_temporal_identity_policy":null,"measurement_policy":null,"modality_policy":{"source":"fixed","value":"confirmed"},"object_policy":{"source":"target_entity"},"polarity_policy":{"source":"fixed","value":"affirmed"},"qualifier_policy":{"assertion_bearing":[],"evidence_only":[],"identity_bearing":[]},"schema_version":"p2_v1","temporal_class":"static_fact","valid_time_policy":{"source":"none"}},"resolution_status":"resolved","schema":"p3_2_stable_predicate_target_v1","temporal_class":"static_fact"}}]},"reason_code":"manual_split","reason_text":"partition A into B and C","requested_effect":"stage"}
reassign sha256 = 9fd80e6d95d8b30e927e6c3096fd262161834b6067e37b8bfe640668de7d1c9f
{"confidence":null,"evidence_refs":[],"expected_precondition_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","method":"manual","operation_payload":{"mapping_assignment":{"from_predicate_id":"10000000-0000-4000-8000-000000000001","mapping_id":"20000000-0000-4000-8000-000000000001","partition_basis_snapshot":{},"reason_code":"manual_reassign","state":"resolved","target_ref":{"kind":"existing","predicate_id":"10000000-0000-4000-8000-000000000002"}}},"reason_code":"manual_reassign","reason_text":"move M1 from A to B","requested_effect":"apply"}
cancel sha256 = 444889da10a1ab3b58b00dcc86d77449a1cf9ad60bd02f1fdf43fe6b60f6fc92
{"confidence":null,"evidence_refs":[],"expected_precondition_fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","method":"authorized_cancellation","operation_payload":{"control_kind":"cancel_pending","expected_pending_decision_id":"30000000-0000-4000-8000-000000000001"},"reason_code":"incorrect_pending_intent","reason_text":"cancel incorrect pending split","requested_effect":"cancel"}
```

The merge precondition vector uses the same valid P2 policy fingerprint
`fca3559dfa4804e445d6151e87fd82b4d7ac3e7732d2b007a9db2fc4217b66a9`:

```text
sha256 = 58e2ad78c21f47a088e3ad88b9a43f3f24a2128895c0451605bfaf2efa05ee9c
{"command_context":null,"library_id":"00000000-0000-4000-8000-000000000001","mapping_slots":[{"current_assignment":null,"mapping_id":"20000000-0000-4000-8000-000000000001"}],"mapping_states":[{"evolution_assignment_id":null,"mapping_id":"20000000-0000-4000-8000-000000000001","mapping_status":"active","predicate_id":"10000000-0000-4000-8000-000000000002","relation_type_id":"40000000-0000-4000-8000-000000000001"}],"new_target_states":[],"operation":"merge","predicate_states":[{"contract_version":"v1","identity_policy_version":"p2_identity_v1","key":"predicate-a","lineage_status":"resolved","namespace":"urn:test:predicate","p2_resolution_status":"resolved","predicate_id":"10000000-0000-4000-8000-000000000001","resolution_policy_fingerprint":"fca3559dfa4804e445d6151e87fd82b4d7ac3e7732d2b007a9db2fc4217b66a9","resolved_predicate_id":"10000000-0000-4000-8000-000000000001","temporal_class":"static_fact"},{"contract_version":"v1","identity_policy_version":"p2_identity_v1","key":"predicate-b","lineage_status":"resolved","namespace":"urn:test:predicate","p2_resolution_status":"resolved","predicate_id":"10000000-0000-4000-8000-000000000002","resolution_policy_fingerprint":"fca3559dfa4804e445d6151e87fd82b4d7ac3e7732d2b007a9db2fc4217b66a9","resolved_predicate_id":"10000000-0000-4000-8000-000000000002","temporal_class":"static_fact"}],"schema":"p3_2_stable_predicate_precondition_v1","source_slots":[{"current_transition":null,"source_predicate_id":"10000000-0000-4000-8000-000000000002"}]}
```

## 6. Proposed Persistent Lineage

P3.2 uses predicate-specific tables. It must not overload the P3.1 canonical
entity tables because their source, successor, assignment, and safety rules
belong to a different identity domain.

### 6.0 Physical bounds

All primary IDs and foreign-key IDs are PostgreSQL `UUID`; every new audit
`created_at` is `TIMESTAMPTZ NOT NULL DEFAULT now()`. The existing nullable
`StablePredicateMapping.superseded_at` contract is unchanged. UUID primary keys
also receive every composite unique tuple named below. Unless explicitly stated otherwise, foreign
keys are `ON DELETE RESTRICT` and `NOT DEFERRABLE`, string/JSON columns have no
default, and a null foreign-key tuple is legal only where the operation-shape
rules allow it. Only the assignment/replacement cyclic pair named in section
6.1 is deferrable.

| Value | SQL/storage bound | Service bound before JCS |
| --- | --- | --- |
| namespace / predicate key | `VARCHAR(128) NOT NULL` | NFC, 1..128 Unicode scalars, UTF-8 <=512 bytes |
| idempotency key | `VARCHAR(256) NOT NULL` | NFC, 1..256 Unicode scalars, UTF-8 <=1024 bytes |
| contract/schema/identity-policy/method/reason code | `VARCHAR(64) NOT NULL` | 1..64 ASCII chars; `^[a-z0-9][a-z0-9_.:/-]{0,63}$` |
| operation/kind/status/actor type | `VARCHAR(32) NOT NULL` | one exact enum literal declared by this document |
| reason text | `VARCHAR(512) NOT NULL` | NFC, 1..512 Unicode scalars, UTF-8 <=2048 bytes; no secret/raw document |
| request ID | `VARCHAR(128) NOT NULL` | 1..128 printable ASCII chars; not whitespace-only |
| fingerprint | `VARCHAR(64) NOT NULL` | `^[0-9a-f]{64}$` |
| command payload | `JSONB NOT NULL` object | <=262144 JCS bytes; sources <=1024; successors <=256 |
| Decision identity / operation payload | operation snapshot is a `JSONB NOT NULL` object | full section 5.3 Decision JCS <=1048576 bytes; operation JCS <=1048576 bytes; assignments <=4096 |
| evidence refs | `JSONB NOT NULL DEFAULT '[]'` array | <=262144 JCS bytes; <=256 refs |
| target specification | nullable `JSONB` object | <=65536 JCS bytes |
| partition basis | `JSONB NOT NULL` object | <=65536 JCS bytes |

JSON shape/count/byte limits are checked by the service before persistence. For
each stored JSONB value, an ordinary CHECK or local deferred constraint trigger
also enforces the same numeric cap over stored `jsonb::text` UTF-8 octets. The
service independently caps both the full section 5.3 Decision-identity JCS and
its operation-payload JCS; the database can enforce only the stored operation
snapshot cap, not reconstruct JCS. JCS and stored-JSONB byte representations
are independent limits and are not asserted equal. Every JSON value has maximum
nesting depth 32. Any service-side string, shape, count, depth, or byte-limit
failure is `REJECTED(payload_too_large|invalid_envelope)` before command-root
insertion and leaves zero writes. Only the service validates JCS byte identity.

The reproducible 4096-assignment size fixture uses source `A`, existing
successor `B`, second existing successor
`C=10000000-0000-4000-8000-000000000003`, and mapping IDs
`20000000-0000-4000-8000-000000000001` through
`20000000-0000-4000-8000-000000004096`. For every assignment it uses
`partition_basis_snapshot={}`, `reason_code="x"`, `state="pending"`, and
`target_ref=null`. Its common Decision fields use `confidence=null`, empty
evidence, the 64-character lowercase `a` precondition hash,
`method=reason_code=reason_text="x"`, and `requested_effect="stage"`. The RFC
8785 UTF-8 payload is exactly 807343 bytes.

### 6.1 Exact tables, columns, keys, and foreign-key tuples

`stable_predicate_evolution_commands`:

| Column | Contract |
| --- | --- |
| `id` | UUID primary key plus `UNIQUE(id, library_id)`. |
| `library_id` | UUID NOT NULL; `RESTRICT` FK to `sys_libraries(id)`. |
| `idempotency_key` | bounded non-null string. |
| `command_identity_fingerprint` | non-null lowercase hash. |
| `operation_kind` | `merge | split | reassign`. |
| `command_payload_snapshot` | exact normalized section 5.1 object. |
| `contract_version` | exactly `p3_2_stable_predicate_evolution/v1`. |
| `created_at` | immutable audit time. |

It has `UNIQUE(library_id, idempotency_key)` and
`UNIQUE(library_id, command_identity_fingerprint)`. It has no predecessor or
supersedes column.

`stable_predicate_evolution_decisions`:

| Column | Contract |
| --- | --- |
| `id` | UUID primary key; also participates in the referenced owner unique below. |
| `library_id`, `command_id` | UUID NOT NULL; `(command_id, library_id)` FK to command. |
| `decision_payload_fingerprint` | non-null lowercase hash. |
| `operation_kind` | non-null and equal to command operation. |
| `requested_effect` | immutable `stage | apply | cancel`; `cancel` is valid only for the cancellation control Decision. |
| `evaluated_outcome` | immutable `pending | applied | rejected | stale | cancelled`. |
| `lifecycle_status` | same initial value, plus the only later value `superseded`. |
| `operation_payload_snapshot` | exact operation payload from section 5.3. |
| `reason_code`, `reason_text`, `method`, `confidence`, `evidence_refs` | remaining common Decision envelope fields; `confidence` is `NUMERIC(7,6) NULL` with `0..1`. |
| `expected_precondition_fingerprint`, `observed_precondition_fingerprint` | non-null lowercase hashes from section 5.4. |
| `supersedes_decision_id` | nullable direct predecessor. |
| `actor_type`, `actor_id`, `request_id` | `actor_type VARCHAR(32)` in `user | service`; actor UUID and request string are non-null. |
| `created_at` | immutable audit time. |

It has `UNIQUE(id, library_id, command_id)`,
`UNIQUE(library_id, command_id, decision_payload_fingerprint)`, a partial
`UNIQUE(library_id, command_id) WHERE lifecycle_status <> 'superseded'`, and a
partial `UNIQUE(library_id, supersedes_decision_id) WHERE
supersedes_decision_id IS NOT NULL`. Its predecessor FK is exactly
`(supersedes_decision_id, library_id, command_id) ->
decisions(id, library_id, command_id)`.

`stable_predicate_evolution_sources`:

| Column | Contract |
| --- | --- |
| `id` | UUID primary key. |
| `library_id`, `command_id`, `evolution_decision_id` | UUID NOT NULL; command/Decision ownership is intentionally redundant. |
| `source_predicate_id` | non-null same-library Predicate FK. |
| `supersedes_source_transition_id` | nullable direct predecessor for the same command/source. |
| `evolution_status` | `pending | applied | historical_only | superseded`; P3.2 writers reject `historical_only`. |
| `created_at` | immutable audit time. |

Required referenced uniques are `(id, library_id, command_id,
source_predicate_id)` and `(id, library_id, command_id,
evolution_decision_id, source_predicate_id)`. The Decision FK is
`(evolution_decision_id, library_id, command_id) -> decisions(id, library_id,
command_id)`. The Predicate FK is `(source_predicate_id, library_id) ->
stable_predicate_identities(id, library_id)`. The predecessor FK is
`(supersedes_source_transition_id, library_id, command_id,
source_predicate_id) -> sources(id, library_id, command_id,
source_predicate_id)`.

It has `UNIQUE(library_id, evolution_decision_id, source_predicate_id)`, a
partial root unique on `(library_id, command_id, source_predicate_id) WHERE
supersedes_source_transition_id IS NULL`, a partial one-successor unique on
`(library_id, supersedes_source_transition_id) WHERE
supersedes_source_transition_id IS NOT NULL`, and a partial live-source unique
on `(library_id, source_predicate_id) WHERE evolution_status IN
('pending','applied','historical_only')`.

`stable_predicate_evolution_successors`:

| Column | Contract |
| --- | --- |
| `id` | UUID primary key. |
| `library_id`, `command_id`, `evolution_decision_id`, `source_transition_id`, `source_predicate_id` | UUID NOT NULL; all ownership/source IDs are intentionally redundant. |
| `target_ref_kind` | `existing | new`. |
| `planned_target_predicate_id` | non-null UUID named by the slot and used for scope 20. |
| `target_predicate_id` | nullable same-library Predicate FK; actual materialized target. |
| `target_spec_fingerprint`, `target_spec_snapshot` | both null for existing; both non-null for new. |
| `created_at` | immutable audit time. |

The additional referenced uniques are `(id, library_id, command_id,
evolution_decision_id)` and `(id, library_id, command_id,
evolution_decision_id, source_transition_id, source_predicate_id)`. The Decision FK is
`(evolution_decision_id, library_id, command_id) -> decisions(id, library_id,
command_id)`. The source FK is `(source_transition_id, library_id, command_id,
evolution_decision_id, source_predicate_id) -> sources(id, library_id,
command_id, evolution_decision_id, source_predicate_id)`. The materialized target FK is
`(target_predicate_id, library_id) -> stable_predicate_identities(id,
library_id)`.

It has `UNIQUE(library_id, source_transition_id,
planned_target_predicate_id)`. Shape checks require an existing slot to have
`target_predicate_id=planned_target_predicate_id` and null spec fields. A new
pending proposal has a null `target_predicate_id`; a new applied proposal has
`target_predicate_id=planned_target_predicate_id`; both new shapes carry the
full spec and hash.

It also has the non-unique aggregate-validation index
`INDEX(library_id, command_id, evolution_decision_id)`; this is not another
semantic uniqueness rule.

`stable_predicate_mapping_evolution_assignments`:

| Column | Contract |
| --- | --- |
| `id` | UUID primary key. |
| `library_id`, `command_id`, `evolution_decision_id` | UUID NOT NULL; ownership IDs are intentionally redundant. |
| `source_transition_id` | must be null for `reassign`; required for merge/split. |
| `old_mapping_id`, `relation_type_id`, `source_predicate_id` | non-null same-library FKs/snapshots. |
| `target_successor_id`, `target_predicate_id` | nullable target; successor required for resolved merge/split, forbidden for reassign. |
| `new_mapping_id` | nullable replacement mapping, paired below. |
| `assignment_state` | `pending | resolved | superseded`. |
| `partition_basis_snapshot`, `reason_code` | non-null bounded object/string. |
| `supersedes_assignment_id` | nullable direct predecessor for the same command and old mapping. |
| `created_at` | immutable audit time. |

Required referenced uniques are `(id, library_id)` and `(id, library_id,
command_id, old_mapping_id)`. It has these exact FKs:

```text
(evolution_decision_id, library_id, command_id)
  -> decisions(id, library_id, command_id)
(source_transition_id, library_id, command_id, evolution_decision_id,
 source_predicate_id)
  -> sources(id, library_id, command_id, evolution_decision_id,
             source_predicate_id)
(old_mapping_id, library_id)
  -> stable_predicate_mappings(id, library_id)
(relation_type_id, library_id)
  -> relation_types(id, library_id)
(source_predicate_id, library_id)
  -> stable_predicate_identities(id, library_id)
(target_successor_id, library_id, command_id, evolution_decision_id,
 source_transition_id, source_predicate_id)
  -> successors(id, library_id, command_id, evolution_decision_id,
                source_transition_id, source_predicate_id)
(target_predicate_id, library_id)
  -> stable_predicate_identities(id, library_id)
(supersedes_assignment_id, library_id, command_id, old_mapping_id)
  -> assignments(id, library_id, command_id, old_mapping_id)
```

For merge/split the expanded successor FK is executable because
`source_transition_id` is non-null. It statically rejects an assignment that
points at another source's successor inside the same multi-source Decision. For
reassign both `source_transition_id` and `target_successor_id` are null by shape,
so no successor relationship is claimed. The service and deferred graph trigger
repeat this check for stable error mapping, but it must not be weakened to the
shorter same-Decision FK.

It has `UNIQUE(library_id, evolution_decision_id, old_mapping_id)`, a partial
root unique on `(library_id, command_id, old_mapping_id) WHERE
supersedes_assignment_id IS NULL`, and a partial one-successor unique on
`(library_id, supersedes_assignment_id) WHERE supersedes_assignment_id IS NOT
NULL`. It also has a partial live-mapping unique on `(library_id,
old_mapping_id) WHERE assignment_state IN ('pending','resolved')`. Static checks
require an `assignment_state=pending` row to have `target_successor_id`,
`target_predicate_id`, and `new_mapping_id` all null. A resolved merge/split row
has a non-null successor link; its `target_predicate_id` equals that successor's
materialized target and may be null only while the parent Decision is pending
and the referenced `kind=new` target has not been created. A resolved reassign
row has no successor link and names the command's existing `to_predicate_id`.
Every row under a pending Decision has `new_mapping_id=null`; every row under an
applied Decision has both target Predicate and replacement mapping non-null.
For every applied assignment, `new_mapping_id != old_mapping_id`.
The cross-row portions of these shape rules are deferred-trigger checks, not
ordinary CHECK constraints.

`stable_predicate_mappings` receives `UNIQUE(id, library_id)` and one nullable
`evolution_assignment_id UUID`. Historical pre-P3.2 mappings remain null and
are not backfilled. The assignment/replacement pair uses these exact cyclic
foreign keys, both `DEFERRABLE INITIALLY DEFERRED`:

```text
assignments(new_mapping_id, library_id)
  -> stable_predicate_mappings(id, library_id)
stable_predicate_mappings(evolution_assignment_id, library_id)
  -> assignments(id, library_id)
```

An applied replacement must carry its assignment ID; a pre-P3.2 mapping must
not pretend to be an evolution replacement.

### 6.2 Trigger timing and executable invariants

Immediate `BEFORE UPDATE OR DELETE` row triggers reject all command and
successor mutations. Decision, source, and assignment rows reject DELETE and
allow UPDATE only when the old row is the actual current head and the only
changed column is its lifecycle/state moving to `superseded`. A mapping row may
change `active -> superseded` or receive `evolution_assignment_id` only inside
the audited pairing verified below; all other P3.2-linked rewrites fail.
Immediate `BEFORE INSERT` child guards also prove that each source, successor,
and assignment is an exact member of its persisted command/Decision snapshot;
they reject late or invented child rows without scanning the command graph.

The migration uses the smallest trigger set that can prove cross-row state. An
`AFTER INSERT CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED FOR EACH ROW`
on commands performs only the cheap “exactly one Decision chain root and one
current head” check. One `AFTER INSERT` deferred constraint trigger on the newly
appended Decision performs the command-scoped aggregate validation below once;
source, successor, and assignment rows do not each launch the same whole-command
scan. Local deferred chain triggers on a superseded Decision/source/assignment
prove that its direct successor or cancellation exists at commit; this permits
the transient no-head ordering forced by the non-deferrable partial unique while
rejecting a standalone supersession. Local deferred triggers on an affected
mapping or Predicate validate only their referenced pair/identity. Every trigger
query is anchored by a frozen `(library_id, command_id)` or row-ID index; no
trigger scans unrelated commands.

The stable database object names are
`trg_stable_predicate_evolution_append_only` (the same per-table trigger name),
`ct_stable_predicate_evolution_command_has_decision`,
`ct_stable_predicate_evolution_decision_graph`,
`ct_stable_predicate_evolution_chain_local`,
`ct_stable_predicate_mapping_evolution_pair`, and
`ct_stable_predicate_identity_evolution_guard`. Their functions use the same
stem with `fn_` instead of `trg_`/`ct_`. Migration prechecks fail if an existing
object already owns one of these names with a different definition.

The Predicate guard applies when an ID is referenced by any P3.2 source,
existing/materialized successor, assignment target, or planned target whose row
already exists. It prevents edits to namespace, key, contract version, temporal
class, identity-policy version, P2 resolution status, and resolution policy for
all lineage participants; for a new target it additionally proves exact stored
specification equality. Unrelated P2 Predicate rows retain their existing writer
contract. At transaction end the affected command is re-read and the triggers
prove:

- operation/library/command redundancy agrees across every complete FK tuple;
- the normalized command and Decision component columns agree with their JSON
  snapshots and hashes where PostgreSQL can compare stored values; JCS hash
  generation remains the service responsibility;
- `requested_effect=stage` has `evaluated_outcome=pending`,
  `requested_effect=cancel` has `evaluated_outcome=cancelled`, and
  `requested_effect=apply` has only `pending | applied | rejected | stale` under
  the section 5.3 evaluation rules;
- a newly inserted Decision has `lifecycle_status=evaluated_outcome`; only an
  older current row may later move to `superseded`; each persisted command has
  exactly one Decision root, one current head, and at least that one Decision;
- Decision/source/assignment predecessor chains have one direct successor, no
  cross-command/source/mapping edge, and no cycle;
- `rejected`, `stale`, and `cancelled` Decisions create zero child rows and zero
  Predicate/mapping effects; cancellation's only child-table mutation is to
  supersede the preceding pending proposal rows;
- merge has only retired sources, one explicit survivor, compatible persisted
  policies, no survivor self-edge, and all retired-source active mappings in
  the assignment set;
- split has at least two exact successor slots and a complete assignment for
  every active source mapping; a pending head has no materialized new target or
  mapping replacement anywhere in the partition;
- each new target row has the planned deterministic UUID and exactly the stored
  target specification; existing/new slot shape is not interchangeable;
- an assignment source predicate equals its old mapping, and relation type is
  identical across old and new mappings;
- a resolved merge/split assignment targets the successor belonging to its exact
  Decision **and source transition**, as already enforced by the expanded
  composite FK; a reassign targets exactly the command's `to_predicate_id`; an unresolved
  assignment has no target or replacement, while a resolved assignment to an
  unmaterialized new slot has a successor link but no target row or replacement;
- an applied assignment has exactly one old mapping changed to `superseded`
  with non-null `superseded_at`, and one different active replacement with null
  `superseded_at`; the replacement's predicate equals the assignment target,
  its relation type equals the old mapping, and
  `assignment.new_mapping_id`/`mapping.evolution_assignment_id` identify each
  other;
- a live source row has the same status as its current parent Decision
  (`pending` or `applied`); a superseded source is paired with a successor
  correction/cancellation, and P3.2 never writes `historical_only`;
- a cancellation Decision creates no child row and does not mutate a successor,
  Predicate, mapping, Fact, Assertion, or bridge; it only supersedes the pending
  predecessor's Decision/source/assignment rows; and
- an applied source/target graph cannot form a predicate lineage cycle.

These deferred checks run at outer commit or explicit `SET CONSTRAINTS ALL
IMMEDIATE`; the service must not force them early, commit, or own outer
rollback. Negative tests must fail at constraint-check/commit time and then
prove the whole transaction rolled back. The frozen 4096-assignment staged
split fixture is 807343 JCS bytes, leaving 241233 bytes below the 1048576-byte
Decision-payload cap. Count and byte limits are independent: 4097 assignments
are rejected by count even when small, and any payload over the byte cap is
rejected even when its assignment count is lower. The 4096-assignment fixture
must observe exactly one aggregate Decision validation invocation and indexed
work proportional to that command's sources/successors/assignments, not one
whole-command scan per child row.

### 6.3 Mapping-status writer boundary

The "no unaudited mapping status rewrite" guarantee is deliberately scoped to
the P3.2 evolution path. Deferred triggers distinguish proposal from effect:

- For the current `pending` Decision, every partition assignment has
  `new_mapping_id = NULL`; the named old mapping remains `active`, no mapping
  carries that assignment's `evolution_assignment_id`, and no replacement row
  exists. An individually resolved partition member is still only a proposal.
- For an `applied` Decision, every assignment is resolved. The old mapping
  changes `active -> superseded`, `new_mapping_id` is non-null, and exactly one
  replacement mapping points back through `evolution_assignment_id` in the
  same outer transaction.
- A superseded pending proposal remains immutable audit. It does not require
  its old mapping to remain active forever: an applied correction may consume
  that mapping. A replacement mapping created by one applied assignment may
  later be superseded only when a later applied P3.2 assignment names it as
  `old_mapping_id` and creates the next replacement. The two assignments are
  connected through that mapping row, not by a forbidden cross-command
  assignment-predecessor link.
- A correction of a mapping proposal sets `supersedes_assignment_id` to the
  actual current assignment for that same command/old mapping. A mapping first
  touched by the correction has a null predecessor; no placeholder predecessor
  row is manufactured.
- Cancellation supersedes the current proposal Decision/source/assignments but
  leaves every old mapping active and creates no replacement.

At commit, triggers therefore validate the current chain tip rather than
requiring every historical replacement mapping to remain active. A P3.2
service has no mapping-status writer path outside these transitions.

Repository evidence at this checkpoint contains no supported runtime mapping
writer outside P3.2: `_active_predicates()` is read-only, and the `0066`
mapping insert is migration/bootstrap history rather than a runtime service.
P3.2 evolution is therefore the sole supported runtime owner of mapping
lifecycle transitions. There is no non-evolution runtime-writer compatibility
promise or test in this stage.

A future runtime mapping writer is prohibited from being called supported until
a separate design amendment names its owner and requires it to acquire scope-20
for every old/new Predicate, discover and lock the affected mapping slots,
re-read after locking, and pass a PostgreSQL two-connection writer-versus-
evolution phantom test. Negative SQL tests still prove that an
evolution-linked mapping cannot be changed, deleted, or paired with a
replacement outside the audited transaction. Arbitrary privileged direct SQL
against an otherwise unlinked P2 mapping cannot be statically prevented and is
unsupported; maintenance/quiescence owns that operational boundary.

## 7. Lifecycle, Correction, and Pending Release

Exact Decision-payload replay is handled before this matrix. It returns
`REUSED` with the original effective outcome and the actual current head;
cancellation replay returns idempotent `CANCELLED`. For a different payload,
the complete legal current-predecessor x evaluated-new-result matrix is:

`pending` is an explicit staged intent for all three operations, not only for
an incomplete split. Its exact child/effect shape is:

| Operation/outcome | Source/successor rows | Assignment rows | Predicate/mapping effect | Resolver consequence |
| --- | --- | --- | --- | --- |
| merge `pending` | one `pending` source per retired predicate; existing-survivor successor proposals | one `resolved` proposal per active retired-source mapping | none; old mappings stay active | every source/mapping proposal returns `pending` |
| split `pending` | one `pending` source and all exact existing/new successor proposals; new target IDs remain planned only | complete partition, each member `resolved` or `pending` | none; no new Predicate or replacement mapping | the entire source partition returns `pending` |
| reassign `pending` | none | exactly one `resolved` target proposal with no successor link | none; old mapping stays active | that mapping context returns `pending` |
| merge/split `applied` | `applied` sources and exact successors | every assignment `resolved` | all required new targets and mapping replacements are atomic | normal applied lineage rules |
| reassign `applied` | none | exactly one `resolved` assignment | old mapping superseded and one replacement active | replacement target may resolve normally |
| `rejected` / `stale` / `cancelled` | none on the new Decision | none on the new Decision | none | prior current state is unchanged except cancellation releases its pending proposal |

Thus an evaluator may intentionally stage a fully specified merge, split, or
reassign as `pending`; “all assignments resolved” does not itself authorize
effects. Only `evaluated_outcome=applied` creates targets or replacements.

| Current predecessor | New `pending` | New `applied` | New `rejected` | New `stale` | New `cancelled` | New `superseded` |
| --- | --- | --- | --- | --- | --- | --- |
| none (D1) | APPEND D1 plus pending proposals | APPEND D1 plus atomic effects | APPEND audit-only D1 | APPEND audit-only D1 | ILLEGAL: nothing to cancel | ILLEGAL: never an evaluated result |
| `pending` | APPEND correction; supersede old Decision/source/assignments | APPEND correction and atomic effects; supersede old pending proposal | RETURN only with zero writes; old pending remains current | RETURN only with zero writes; old pending remains current | APPEND cancellation; supersede old pending proposal and create no child/effect | ILLEGAL: never an evaluated result |
| `rejected` | APPEND retry; old Decision becomes superseded | APPEND retry and atomic effects; old Decision becomes superseded | APPEND audit-only retry; old Decision becomes superseded | APPEND audit-only retry; old Decision becomes superseded | ILLEGAL: no pending intent | ILLEGAL: never an evaluated result |
| `stale` | APPEND retry; old Decision becomes superseded | APPEND retry and atomic effects; old Decision becomes superseded | APPEND audit-only retry; old Decision becomes superseded | APPEND audit-only retry; old Decision becomes superseded | ILLEGAL: no pending intent | ILLEGAL: never an evaluated result |
| `applied` | ILLEGAL: command closed | ILLEGAL | ILLEGAL | ILLEGAL | ILLEGAL | ILLEGAL |
| `cancelled` | ILLEGAL: command closed | ILLEGAL | ILLEGAL | ILLEGAL | ILLEGAL | ILLEGAL |
| `superseded` | ILLEGAL: never a current head; reload actual head | ILLEGAL | ILLEGAL | ILLEGAL | ILLEGAL | ILLEGAL |

Every APPEND requires an exact current-head CAS and sets the new Decision's
direct predecessor. `RETRYABLE_CONFLICT` is never persisted and leaves the
command/Decision/child/effect counts unchanged after outer rollback.
`historical_only` is a source evolution state, not an evaluated Decision result,
so it does not appear as a new-result column.

Only a current `pending` decision can be cancelled. Cancellation has an outer
transaction boundary containing lock acquisition, old pending lifecycle
supersession, source/assignment release, and the new terminal control decision.
It does not create successors, targets, mapping replacements, Facts, or Fact
Resolution decisions. A rollback restores the old pending slot completely.

Cancellation requires the same existing library-level `admin` authorization
through `authorize_library_management()` as the original mutation. There is no
separate cancellation authority. The terminal Decision persists actor, method,
reason code/text, evidence, request ID, the expected pending Decision ID, and
lock-time observed precondition. A different command cannot cancel or
supersede the pending intent. A new command may occupy the released
source/mapping slot only after cancellation commits and fresh lock-time
admission succeeds.

No command may supersede another command. A corrected decision, source, or
mapping assignment must retain the same command ID and direct predecessor ID.

## 8. Resolver and Fact-Resolution Admission

`_active_predicates` must evolve from "return predicate rows" to "return the
single active mapping row and resolve its current predicate in persisted mapping
context." The caller may proceed only when all checks are true:

```text
one active StablePredicateMapping for this relation type
AND current predicate lineage status == resolved
AND no current pending split exists for that predicate's source partition
AND no current pending reassignment owns this mapping
AND selected predicate is P2-ready
AND lock-time re-read observes the same mapping/current identity
```

Resolution is iterative, not one-hop. It maintains both a visited Predicate-ID
set and a visited mapping-ID set, starting from the supplied predicate and
optional persisted mapping context. At every hop it applies this exact order:

1. Validate that the current Predicate and mapping, when present, belong to the
   requested library; that the mapping names the current Predicate; and that a
   supplied relation type agrees with the mapping. Add both IDs to their
   visited sets and fail closed on repetition.
2. Re-read the current Decision/source/assignment tips that own the current
   Predicate or mapping. Any current pending merge/split source transition or
   pending reassignment returns `pending`; an incomplete split takes precedence
   before any individually resolved assignment can be used. An explicit
   persisted terminal source returns `historical_only`.
3. For an applied reassign assignment whose `old_mapping_id` is the current
   mapping, require its applied parent Decision, exact target Predicate, and
   exact `old_mapping_id -> new_mapping_id` replacement pair. Advance mapping
   context to `new_mapping_id` and Predicate context to that mapping's target,
   then repeat from step 1.
4. For an applied predicate source transition, require its applied parent
   Decision and complete successor graph. A merge has exactly one successor;
   advance the Predicate to it and, when mapping context exists, advance the
   mapping through that source's resolved assignment replacement. A split has
   at least two successors: without mapping context return `forked`; with
   mapping context require the exact resolved assignment for that source and
   old mapping, then advance both Predicate and mapping through its target and
   replacement. Repeat from step 1 after either operation.
5. If no applicable current applied edge remains, require any mapping context
   to be the active mapping that names the current Predicate; otherwise fail
   closed for integrity. Return the current persisted Predicate as `resolved`;
   Fact Resolution then applies the separate P2-ready checks below.

The mapping context therefore advances along every persisted
`old_mapping_id -> new_mapping_id` chain while Predicate context advances along
the corresponding applied successor/assignment chain. Each generation repeats
all pending, forked, historical-only, and integrity checks. An incomplete split
at any intermediate generation returns `pending`; an unscoped split at any
generation returns `forked`. A cycle, missing required edge, wrong parent
Decision or lifecycle status, broken assignment/replacement pair, or
mapping/Predicate/library mismatch returns `pending(predicate_evolution_integrity)`
with no Fact or bridge. The resolver must not choose a first/latest row, stop
after one hop, or infer a target by name, similarity, embedding, or LLM.

Failure results are deterministic:

| Condition | P2-compatible FactResolutionDecision result | Fact/bridge effect |
| --- | --- | --- |
| no active mapping | `pending(predicate_mapping_pending)` | none |
| multiple mappings or corrupted cardinality | `pending(predicate_mapping_ambiguous)` | none |
| predicate lineage forked | `pending(predicate_evolution_forked)` | none |
| predicate lineage pending | `pending(predicate_evolution_pending)` | none |
| incomplete split, including a context whose own assignment is resolved | `pending(predicate_split_partition_incomplete)` | none |
| pending mapping reassignment | `pending(predicate_mapping_reassignment_pending)` | none |
| predicate lineage historical-only | `pending(predicate_evolution_historical_only)` | none |
| lineage/mapping integrity failure at any hop | `pending(predicate_evolution_integrity)` | none |
| target P2 policy not ready | `pending(predicate_not_ready)` | none |
| target P2 status rejected | `rejected(predicate_rejected)` | none |
| lock-time mapping/current target differs | `pending(predicate_current_identity_changed)` | none |

Graph-candidate and RawClaim-backed Fact Resolution share this preflight. P3.2
does not create a RawClaim promotion path or retrofit earlier decisions.

The incomplete-split row takes precedence over individual mapping assignment
context. In particular, the `R1 -> B` portion of a pending `A -> {B, C}` split
cannot create a new Fact before the `R2` partition has been applied. If that
split is cancelled or corrected, no P3.2-created Fact needs to be removed.

When the current predicate is resolved, a new normal FactResolutionDecision
records the selected predicate as that attempt's snapshot. It does not modify a
previous decision that recorded the predecessor predicate.

## 9. Lock and Transaction Contract

P3.2 must call P3.1's shared `lock_graph_identity_scopes()` helper. The fixed
order remains:

```text
(library_id, scope_type, scope_key)
10 canonical_entity
20 stable_predicate
30 logical_fact
40 entity_projection
50 entity_resolution_subject
```

The shared helper receives one backward-compatible option,
`wait: bool = True`. Existing P3.1 callers retain the blocking default; P3.2
mutation and evolution-aware Fact Resolution must call it with `wait=False`.
That branch keeps the same scope hash and sorted order but uses
`pg_try_advisory_xact_lock` for each key. It adds no retry loop, sleep, second
lock namespace, or whole-library lock. A false result raises the stable internal
`GraphIdentityLockBusy`; because every lock precedes every write, the
application transaction boundary ends the zero-write transaction (releasing
any earlier transaction locks) before returning
`RETRYABLE_CONFLICT(predicate_evolution_lock_busy)`. No Decision ID is claimed
durable on this path.

After advisory acquisition, every P3.2 mapping/source row lock uses `FOR UPDATE
NOWAIT`. A busy row follows the same rollback/result rule. This fail-fast path
is the P3.2 runtime wait bound; it does not change the existing P2/P3.1 default
for callers outside this feature.

Predicate evolution builds its full scope set before mutation: all source,
survivor, and existing successor predicate IDs use scope 20; every new target
uses its deterministic final Predicate UUID from section 5.2. The locked UUID
is therefore always an existing or proposed `StablePredicateIdentity.id`, never
a synthetic reservation key. The service then locks affected mapping rows with
`FOR UPDATE NOWAIT`, re-reads live slots, scoped-identity collisions, and
preconditions, and fails closed if membership or current identity has changed.
It never uses a whole-database lock.

Fact Resolution must build its P3 scope set after no-lock discovery and before
Fact writes: current canonical identities where required by P3.1, selected
predicate scope 20, and predicted Fact scope 30. It must **not** put a Fact
Resolution subject into scope 50: the frozen P3.1 scope 50 means only an Entity
Resolution subject fingerprint. After acquiring shared scopes 10/20/30, Fact
Resolution re-resolves mapping and predicate lineage. If that changes the
predicted lock set or current identity, it writes only the pending result
defined in section 8 and creates no Fact bridge.

P3.2 must preserve the existing P2 lock relationship during this transition.
Today Fact Resolution uses the legacy resolution-subject and logical-fact lock
keys, and Fact Lifecycle shares the legacy logical-fact key. P3.2 does not
remove or replace those keys. A Fact Resolution attempt uses this bridge order:

1. perform no-lock discovery and build the complete shared P3 scope set;
2. acquire all shared locks through `lock_graph_identity_scopes(wait=False)` in the
   applicable `10 -> 20 -> 30` order; scopes 40/50 are absent from this Fact
   Resolution set, while the helper's global ordering contract remains intact;
3. acquire the legacy resolution-subject lock and, when a Fact identity is
   available, the legacy logical-fact lock, preserving their existing P2 order;
4. re-read canonical projections, mapping, predicate lineage, current Decision,
   and expected Fact identity before any Decision, Fact, Assertion, or bridge
   write.

No path may acquire a shared P3 lock after taking a legacy lock. The legacy
resolution-subject key remains the only Fact Resolution subject lock; it is not
relabelled as P3 scope 50. Fact Lifecycle continues using the legacy
logical-fact lock in P3.2, so it still contends with
Fact Resolution. Removing the legacy bridge or migrating Fact Lifecycle belongs
to a later separately authorized checkpoint. Tests must prove both the
predicate-evolution/shared-lock contention and the Fact-Resolution versus
Fact-Lifecycle legacy-lock contention; replacing one with the other is not an
equivalent test.

The service owns no `commit()` or outer `rollback()`. Every new-root,
same-root correction, retry, or cancellation uses one outer transaction in this
exact order:

1. load only the requested `Library` by the envelope's `library_id`; before
   authorization, do not query any command, Predicate, mapping, Decision,
   predecessor, target, or identity-collision row;
2. call `authorize_library_management(db, user=user, library=library)` and stop
   with the existing non-disclosing authorization error contract on failure;
3. derive the immutable server-owned `actor_type`/`actor_id` from the
   authenticated cookie/session or API-key context exactly as section 5.0
   defines;
4. only after steps 1-3, perform no-lock scope discovery for the authorized
   command and build the complete advisory/row-lock set;
5. acquire sorted advisory locks and all required `FOR UPDATE NOWAIT` rows,
   then re-read the complete current graph and validate library ownership,
   admission, identity collision, exact replay, actual-head CAS, and the
   section 7 matrix;
6. when a predecessor exists and the matrix permits APPEND, change the current
   predecessor Decision and every current source/assignment tip that belongs to
   it to `superseded`, then `flush()` once. This deliberate transient no-head
   state releases the non-deferrable Decision/source/assignment partial uniques;
   immutable successor proposal rows and old mappings are not changed here;
7. insert the new Decision, followed by exactly the child graph allowed for its
   evaluated outcome. A new root skips step 6. A pending-predecessor evaluation
   of `rejected` or `stale` follows the matrix's zero-write RETURN and therefore
   performs neither step 6 nor step 7;
8. for an applied Decision, allocate assignment/replacement UUIDs before their
   inserts. Insert each assignment with its planned `new_mapping_id`, mark the
   old mapping `superseded`, and `flush()` to release the existing active-mapping
   partial unique before inserting the one replacement mapping carrying the
   assignment's `evolution_assignment_id`;
9. let the deferred chain, graph, and bidirectional-pair constraints validate
   the complete state at outer commit.

For any pending merge/split/reassign, step 7 persists the complete proposal with
all replacement links null and step 8 is omitted. Cancellation inserts only its
terminal control Decision after superseding the pending predecessor graph; it
creates no new child. Each `flush()` is only a statement boundary: deferred
constraints must not be forced early, and the service must not commit or own the
outer rollback. Any exception or outer rollback restores every predecessor
status and old active mapping while removing the new Decision, lineage, target,
and replacement rows together.

## 10. Migration Direction

The next migration is `0073`; implementation must recheck that `0072` is still
the single Alembic head before creating it. `0071` is immutable and `0072`
remains the only P3.1 corrective migration.

### 10.1 Upgrade DDL and ordering

`0073` is one PostgreSQL transactional Alembic migration. It must not call
`COMMIT`, use autocommit blocks, issue `CREATE INDEX CONCURRENTLY`, or permit a
second concurrent DDL path. Failure at any statement rolls back the entire
schema change. The upgrade order is fixed:

1. set bounded local `lock_timeout`/`statement_timeout`, take the deployment
   advisory lock, and acquire the maintenance locks below;
2. run non-destructive prechecks for the expected `0072` schema and key names;
3. create command and Decision tables, then source and successor tables;
4. create assignment table without the cyclic mapping FKs;
5. add `UNIQUE(id, library_id)` and nullable `evolution_assignment_id` to
   `stable_predicate_mappings`; do not backfill it;
6. add the two deferred cyclic FKs, remaining indexes/checks, immediate
   immutability guards, and deferred constraint triggers;
7. validate the final catalog shape in the same transaction and commit once.

Step 1 executes exactly `SET LOCAL lock_timeout = '5s'`, `SET LOCAL
statement_timeout = '300s'`, followed by this single server-side block:

```sql
DO $p3_2_migration_lock$
BEGIN
    IF NOT pg_try_advisory_xact_lock(356131050375775214) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55P03',
            MESSAGE = 'migration_busy';
    END IF;

    BEGIN
        LOCK TABLE sys_libraries IN SHARE MODE NOWAIT;
        LOCK TABLE relation_types IN SHARE MODE NOWAIT;
        LOCK TABLE stable_predicate_identities IN SHARE ROW EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mappings IN ACCESS EXCLUSIVE MODE NOWAIT;
    EXCEPTION
        WHEN lock_not_available THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'migration_busy';
    END;
END
$p3_2_migration_lock$;
```

The advisory key is the signed big-endian first eight bytes of
`SHA256("vector-kb:p3.2:0073:migration:v1")`. Both an advisory-lock false result
and any table `NOWAIT` failure therefore return the same stable SQLSTATE
`55P03` and message `migration_busy`, and the surrounding migration transaction
rolls back. The migration owns this exact SQL as one constant and passes it to
Alembic unchanged for online execution and offline SQL rendering. It must not
use Python `get_bind()`, inspect a client-side result row, branch on offline
mode, or emit a plain `SELECT pg_try_advisory_xact_lock(...)` whose false result
could be ignored.

It must not backfill or reinterpret existing Predicate identities/mappings,
LogicalFacts, FactAssertions, FactResolutionDecisions, KnowledgeRelations, or
RelationEvidence.

### 10.2 Required maintenance/quiescence boundary

`0073` requires an application maintenance window. Before Alembic starts, stop
admission and drain or terminate every transaction that can write
`stable_predicate_identities`, `stable_predicate_mappings`, or execute graph/
RawClaim Fact Resolution against those mappings. This includes any ad hoc
migration/bootstrap/administrative write session and graph worker; it does not
assert that a supported P2 runtime mapping writer exists. Transactions must
commit or roll back before migration; killing a session is an operator action,
not migration logic.

With only the migration connection active, the server-side block above acquires
these table locks in this exact order, each with `NOWAIT`:

```text
sys_libraries                 SHARE
relation_types                SHARE
stable_predicate_identities   SHARE ROW EXCLUSIVE
stable_predicate_mappings     ACCESS EXCLUSIVE
```

Failure to acquire any lock produces a stable `migration_busy` failure and the
whole migration rolls back; it must not wait indefinitely or continue with a
partial lock set. New P3.2 tables are created only after these existing-table
locks are held. A deployment-scoped PostgreSQL advisory transaction lock also
serializes two `0073` runners; it is migration-only and is not a runtime
whole-library lock.

The representative `0072` mapping DML shape is schema-compatible after a
successful upgrade because the new mapping link is nullable and triggers do not
require a P3.2 assignment for ordinary pre-P3.2 rows. This is a migration
compatibility property, not support for an evolution-unaware runtime writer.
The runbook keeps every writer stopped between schema commit and
application-version health verification, so old and new binaries never race
during cutover. Schema compatibility is not semantic rollback compatibility:
the existing P2 Fact Resolution reader does not inspect P3.2 pending lineage.

The new application therefore starts with P3.2 mutation admission disabled;
every API/worker instance must be P3.2-aware and pass health checks before that
admission opens. Rollback to the old P2
binary or downgrade to `0072` is allowed only inside a fresh maintenance window
after admission is stopped and a single transaction proves every P3.2 table is
empty and every mapping `evolution_assignment_id` is null. Once any P3.2
command/Decision/child exists or any mapping carries that link, the old binary
is semantically unsafe and prohibited; recovery is roll-forward to a P3.2-aware
binary while writers remain stopped. Audit-only rejected/stale rows count as
P3.2 data for this barrier. The runbook must never describe “schema still
loads” as permission to run an evolution-unaware Fact writer.

### 10.3 Downgrade and concurrent-writer proof

Downgrade uses the same quiescence and one transaction. Before its empty-state
check it executes the same two `SET LOCAL` statements as upgrade and one
downgrade-only server-side block. The block acquires the same advisory and
existing-table locks first, then locks every P3.2 audit table in this additional
exact order with `ACCESS EXCLUSIVE NOWAIT`:

```sql
DO $p3_2_downgrade_lock$
BEGIN
    IF NOT pg_try_advisory_xact_lock(356131050375775214) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55P03',
            MESSAGE = 'migration_busy';
    END IF;

    BEGIN
        LOCK TABLE sys_libraries IN SHARE MODE NOWAIT;
        LOCK TABLE relation_types IN SHARE MODE NOWAIT;
        LOCK TABLE stable_predicate_identities IN SHARE ROW EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mappings IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_commands IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_decisions IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_sources IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_evolution_successors IN ACCESS EXCLUSIVE MODE NOWAIT;
        LOCK TABLE stable_predicate_mapping_evolution_assignments IN ACCESS EXCLUSIVE MODE NOWAIT;
    EXCEPTION
        WHEN lock_not_available THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'migration_busy';
    END;
END
$p3_2_downgrade_lock$;
```

Only after every lock is held may downgrade prove every P3.2 table empty and
every `StablePredicateMapping.evolution_assignment_id` null. Because the audit
tables remain access-exclusive through transaction end, no writer can insert a
command or audit row between the empty-state check and `DROP`. Advisory-false
or any existing/new-table lock failure returns the same SQLSTATE `55P03` and
message `migration_busy`, with schema and data unchanged. The migration owns
this block as one constant and emits it unchanged online and offline without
Python result/offline branching.

Downgrade fails closed before dropping anything if either empty-state condition
is false. It removes dependent triggers and cyclic FKs first, then the mapping
column/key only after dependent tables are removed in reverse order. It never
deletes audit history to make downgrade succeed.

Executable PostgreSQL coverage must force both schedules with two connections:

- a two-connection `0072`-shape mapping DML fixture holding a relevant row/table
  lock before `0073` makes the migration fail quickly as `migration_busy`, with
  the `0072` catalog unchanged;
- `0073` holding its maintenance locks first prevents that DML fixture from
  entering; after migration commit, the same unmodified DML shape succeeds with
  a null evolution link, proving only additive schema compatibility and no
  partial-schema visibility;
- a P3.2 audit writer holding any new-table lock first makes downgrade fail
  quickly as `migration_busy`, with schema and data unchanged; downgrade holding
  its full lock block first prevents that writer from entering, and no audit row
  can appear after the empty-state check;
- a failed DDL statement and a failed final catalog assertion each leave the
  exact `0072` schema; and two concurrent migration runners cannot both proceed.

Offline SQL, catalog assertions, and a disposable PostgreSQL upgrade/downgrade
round trip are all required. In-memory databases cannot substitute for these
lock, DDL, FK, trigger, or rollback tests.

## 11. Required Test Matrix

Before P3.2 can be frozen for implementation, the implementation plan must
retain these tests.

| Area | Required cases |
| --- | --- |
| merge | explicit survivor, survivor/source locked-current-leaf rejection, compatible-policy merge, incompatible-policy rejection, staged pending merge has complete proposals and zero effects, loser mapping reassignment, old LogicalFact unchanged |
| split | 1-to-N successor slots, existing-successor locked-current-leaf rejection, complete mapping partition, applied split direct resolver `forked`, applied mapping-context resolver `resolved`, ambiguous mapping `pending`; any incomplete split returns `pending` for every mapping context, including individually resolved assignments |
| correction/cancellation | every predecessor/new-result matrix cell, partial split correction, rejected correction preserves old pending intent, authorized cancellation audit/release, missing library `admin` cancellation rejection, cross-command cancellation/supersede rejection, cross-source and cross-projection predecessor rejection, cancellation/new-intent race; force a failure after predecessor Decision/source/assignment tips are superseded and flushed but before the replacement graph is complete, then prove outer rollback restores every old head and effect exactly; prove the successful correction has the exact transient-no-head ordering from section 9 and that no Fact or bridge is created from `R1 -> B` while `R2` remains pending |
| authorization/envelope | with organization authorization enabled, active organization plus active `organization_admin` and active-member Casbin `admin` allow cases, while inactive organization, inactive/missing membership, and ordinary member deny; with it disabled, `is_superuser` and Casbin `admin` allow cases plus ordinary-user denial; cookie/session audit is exactly `user/User.id`, and in both modes API-key audit is exactly `service/api_key_id` from the separate request-local `CredentialApiKeyAuditIdentity`; prove this context does not change `credential_organization_scope(user)` or existing publication/catalog behavior; missing API-key audit identity, fallback to `User.id`, another service-principal shape, unauthenticated, cross-library IDs, forged actor fields, unknown/extra fields, and oversize/deep JSON all fail before sensitive existence disclosure/root lookup and leave every P3.2/effect table unchanged; cancellation uses the same helper modes and no separate permission literal; exact replay is tested only after authorization |
| requested effect | omission and literals other than exact `stage | apply` are rejected before root lookup/write; complete merge, split, and reassign with `stage` persist pending proposals and zero effects; complete merge/reassign with `apply` become applied or fail without an implicit pending fallback; incomplete split with `apply` becomes pending with zero effects; changing `stage -> apply` requires the exact current pending predecessor and remains in the same Command root; cancellation is server-fixed to `cancel` and callers cannot submit it |
| command identity | merge sources exclude survivor and reject duplicates; merge source permutation and split mixed-slot permutation produce identical bytes/hash; mixed slots use `existing=0/new=1` then Predicate UUID; changed survivor/source/slot/mapping/from/to changes identity; alias key rejected |
| Decision identity | merge/split/reassign/cancel exact JSON golden fixtures; mapping-assignment, policy-compatibility-member, successor, and evidence-reference permutations normalize by their frozen rules; nested policy arrays retain P2 business order; missing/extra key, duplicate key/member, non-NFC, invalid UUID/hash/number, BOM, and trailing newline rejected; observed state/audit IDs do not enter the hash |
| precondition | merge/split/reassign prepare token round trip; mutate every predicate/mapping/source-slot/mapping-slot/new-target/command-context field and observe deterministic stale/admission behavior; caller-supplied snapshot rejected; cancel copies predecessor observed hash, recomputes fresh observed state, and does not use hash equality as its release gate |
| target specification | persisted full target JSON plus fingerprint agreement; deterministic planned UUID equals final Predicate PK and scope-20 key; pending successor proposal has no Predicate row; UUID/scoped-identity collision rejection and exact replay-before-collision; two connections proposing the same scoped identity use the same final UUID, with fail-fast loser and fresh retry after winner commit/rollback |
| reassign | pending proposal leaves old mapping active and blocks that context; applied result supersedes old mapping plus exactly one active replacement; source/target locked-current-leaf rejection, source/target mismatch rejection, no historical Fact rewrite |
| replay | exact payload replay outcome fields, historical replay plus actual head, cancellation replay, idempotency conflict, current-head CAS failure, and only current `REUSED(APPLIED)` treated as effect |
| schema/lineage integrity | every required column/null/default/check, including persisted immutable `requested_effect`, redundant command/Decision/source ID, only actually referenced composite uniques, full FK tuple, partial unique, and frozen trigger/function name inspected in ORM/migration/catalog; direct SQL cross-command/source/mapping predecessor, cycle, append-only mutation, requested-effect/outcome mismatch, Decision/status/effect mismatch, deferred assignment/replacement pair mismatch, target-spec/participant-immutability mismatch, command-without-Decision, and outer rollback rejection; the expanded `(target_successor_id, library_id, command_id, evolution_decision_id, source_transition_id, source_predicate_id)` FK rejects a successor from another source even inside the same Decision; the exact 4096-assignment/807343-byte fixture invokes aggregate Decision validation once and uses command-scoped indexes, 4097 small assignments fail the count cap, and a lower-count oversized payload independently fails the byte cap |
| mapping writer boundary | current pending assignment leaves old mapping active with no replacement; applied assignment creates the exact deferred pair; a later applied P3.2 assignment may supersede that replacement; naked update/delete/replacement rejection; repository evidence confirms no supported non-evolution runtime mapping writer, and a future writer remains prohibited until a separate amendment plus shared scope-20/row-lock/re-read contract and two-connection phantom test |
| Fact Resolution | graph candidate and RawClaim pending gates for forked/pending/historical-only and every incomplete split, isolated read-model historical-only fixture, P2 pending/ambiguous/rejected status does not imply historical-only, resolved target success only after a complete applied partition, no bridge on any unresolved result; iterative merge-to-split, split-to-merge with mapping context, and multi-reassign chains advance both visited Predicate and mapping IDs; pending at an intermediate generation, unscoped intermediate split, missing/cross-library edge, wrong Decision/status, broken mapping pair, and corrupt Predicate/mapping cycle all fail closed without first/latest/one-hop selection |
| concurrency | PostgreSQL two-connection merge/split/reassign versus Fact Resolution; deterministic final-target UUID scope-20 contention, fail-fast advisory/row-lock busy with partial transaction-lock release, no sleep/retry/deadlock, only one effective writer, and post-lock re-read; Fact Resolution uses shared 10/20/30 only, never scope 50, while retaining legacy subject/fact locks and Fact Lifecycle contention |
| migration | `0072 -> 0073` single-transaction offline/actual SQL and downgrade; no autocommit or concurrent DDL; online execution and offline rendering contain the identical direction-specific server-side lock block and no Python result/offline branch; execute the rendered offline SQL in a two-connection fixture with the advisory key held and, separately, each ordered existing-table lock held, and prove advisory-false and every table-lock `NOWAIT` failure expose exact SQLSTATE `55P03` plus message `migration_busy`; exact advisory/table lock order; `0072`-shape DML-first `migration_busy`, migration-first additive DML-shape compatibility without claiming a supported old runtime writer, pre-admission empty-state old-binary rollback, post-P3.2-row old-binary prohibition/roll-forward recovery, failure rollback to exact `0072`, dual-runner exclusion, empty/nonempty P2 upgrade, and upgrade/downgrade round trip; downgrade online/offline SQL contains the exact additional command/Decision/source/successor/assignment `ACCESS EXCLUSIVE NOWAIT` sequence, audit-writer-first is busy with schema/data unchanged, downgrade-first blocks the writer through the empty check/drop, post-check insertion is impossible, and nonempty-P3.2 downgrade fails closed |
| regression | P1/P2 historical fact reads, P3.1 canonical resolver and GraphGovernance mutual exclusion remain unchanged |

If `VECTOR_KB_PG_TEST_DSN` is unavailable at implementation time, PostgreSQL
runtime cases may be skipped only on a developer-local run and must be reported
exactly as:

```text
SKIPPED — PostgreSQL runtime integration unavailable
```

They may not be simulated by an in-memory database. This skip is a blocking,
unverified gate: no implementation freeze, merge, “implementation complete”, or
release-ready claim is permitted until the same revision passes every required
runtime case against a controlled disposable PostgreSQL database and records the
exact command, server version, discovered case count, and pass result.

## 12. Explicit Non-goals and Stop Point

P3.2 stops after predicate lineage, mapping lineage, current-predicate
resolution, and the Fact-Resolution admission gate are implemented and tested.
It does not make old LogicalFacts current, regroup FactAssertions, recompute
conflicts, change lifecycle queries, migrate RelationEvidence, or expose a new
retrieval/publication view.

Those are P3.3 decisions and remain blocked until a separate reconciliation
design freeze is approved.

## 13. Freeze Gates

The independent review of pre-freeze blob
`8e3cf704a38318925b6645991f6b10dcdaf1883b` confirmed all of the following;
they are frozen implementation gates:

1. The distinction between predicate P2 readiness and P3.2 evolution status is
   complete and has no path that treats `forked` as a usable predicate.
2. Merge policy compatibility is sufficiently conservative and its exact
   canonical JSON comparison is acceptable.
3. Split target definitions persist complete normalized P2 policy data, have an
   unambiguous JCS hash input and collision rule, and create a new predicate
   only in an applied transaction.
4. An incomplete split returns `pending` for all of its mappings and cannot
   create a Fact or bridge before the whole partition is applied.
5. Existing survivor/successor/reassignment target predicates are verified as
   locked current leaves, with no implicit lineage traversal.
6. Mapping assignment lineage has enforceable same-command predecessor FKs,
   an expanded assignment-to-successor FK that includes source transition and
   source Predicate and therefore rejects cross-source references statically,
   explicitly deferred bidirectional assignment/replacement links, a bounded
   lifecycle-specific pending/applied rule, a bounded P3.2 mapping-writer rule,
   and all non-static invariants assigned to constraint triggers rather than
   merely described as SQL constraints.
7. New-target creation uses one deterministic UUID as planned ID, final
   `StablePredicateIdentity.id`, and scope-20 key; it closes commit/rollback and
   UUID/scoped-identity collision races without a synthetic reservation key.
8. Merge source membership and every Command/Decision array have an explicit
   duplicate rule and domain sort independent of RFC 8785 object-key ordering.
9. Merge, split, reassign, and cancel have complete exact Decision payloads;
   `requested_effect` is mandatory `stage | apply` for normal requests, is part
   of Decision rather than Command Identity, has the exact pending/effect and
   same-root correction rules in section 5.3, and is server-fixed to `cancel`
   for cancellation; expected/observed preconditions have one exact
   operation-specific snapshot and cancellation rule; section 5.5 golden
   bytes, hashes, and UUIDv5 all reproduce exactly.
10. The schema freezes every necessary column, null/default, composite unique,
    full same-library FK tuple, partial unique, immediate guard, and deferred
    constraint-trigger timing required for executable DDL; each Decision status
    has the exact child/effect shape in sections 6-7, and aggregate validation
    runs once per appended Decision rather than once per child.
11. All three operations have an explicit pending shape and no pending proposal
    can create a target Predicate, replacement mapping, Fact, or bridge.
12. Authentication, both existing `authorize_library_management()` modes,
    active-organization rejection in organization mode, cookie/session
    `user/User.id` audit, and API-key `service/api_key_id` audit through the
    separate request-local `CredentialApiKeyAuditIdentity` are frozen; the
    audit identity cannot change `CredentialOrganizationScope` or existing
    publication/catalog behavior. Unbound or other service-principal shapes,
    cross-library requests, and envelope/resource violations are rejected
    before sensitive lookup/existence disclosure or any write; no new
    permission, P3.2-specific administrator bypass, service-actor bypass, or
    replay bypass exists.
13. The current Fact Resolver iterates every applied generation while advancing
    and cycle-checking both Predicate and mapping IDs, repeats pending/forked/
    historical/integrity rules at each hop, and fails closed on any corrupt or
    incomplete intermediate state; it acquires shared scopes 10/20/30 before
    the frozen P2 legacy subject/fact bridge, never uses Entity Resolution scope
    50 for a Fact subject, preserves Fact Lifecycle mutual exclusion, and P3.2
    runtime lock acquisition fails fast without a retry/sleep branch.
14. The full predecessor/new-result matrix has no implicit transition,
    correction uses the exact lock/CAS -> supersede all old tips -> flush ->
    insert replacement graph -> apply effects -> deferred-commit-validation
    order, any failure restores the complete predecessor graph, and
    `historical_only` is sourced only from an explicit persisted terminal state
    that has no P3.2 writer.
15. `0073` is a single-transaction, non-concurrent DDL migration with mandatory
    maintenance/quiescence, exact advisory/table lock order inside one
    direction-specific server-side block emitted unchanged online/offline,
    downgrade-only access-exclusive locking of every P3.2 audit table before
    its empty-state check, deterministic
    SQLSTATE `55P03` plus `migration_busy` for advisory-false or table-lock
    failure without Python result branching, additive `0072` DML-shape schema
    compatibility without a supported old runtime-writer claim, a
    pre-data old-binary rollback gate, a post-P3.2-data roll-forward-only rule,
    rollback proof, and fail-closed nonempty-P3.2 downgrade coverage.
16. A controlled disposable PostgreSQL run is a mandatory implementation merge
    gate; a developer-local `SKIPPED` result is recorded but never accepted as
    runtime evidence.
17. P3.3 remains entirely outside the implementation scope.

These gates are closed at this checkpoint. P3.2 implementation and migration
`0073` creation are authorized. Deployment, migration execution against a
server database, P3.3, and all other excluded stages remain prohibited.
