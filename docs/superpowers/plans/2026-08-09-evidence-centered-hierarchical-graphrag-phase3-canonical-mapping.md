# Evidence-Centered Hierarchical GraphRAG Phase 3: Canonical Mapping

Status: Phase 0-3 complete; Phase 3 real shadow canary/handoff acceptance is
in progress. Phase 4-6 not started. M0-M5 remain isolated from publication,
entity, relation, candidate, and materialization writes. The real canary is
shadow/read-only only, and mapping-to-publication remains unauthorized.

This plan follows Phase 2 shadow persistence and replay. It separates the
question "was a relation extracted?" from the question "which frozen
canonical relation does it represent?".

## 1. Guardrails and Audit Result

### 1.1 GitNexus attempt

The required first attempt was:

```text
npx gitnexus analyze
```

It failed before indexing because the `tree-sitter-kotlin` dependency could
not load `node-gyp-build`. GitNexus is therefore unavailable for this M0
audit. The fallback used `rg`, targeted source reads, existing focused tests,
and the Phase 2 boundary handoff. A future milestone must retry the required
impact analysis before editing any existing production symbol.

### 1.2 Audited callers and flows

| Boundary | Definitions and direct callers | Risk |
|---|---|---|
| Raw claim core and occurrence | `RawClaimV1`; `GraphRawClaim`; `GraphRawClaimOccurrence`; `create_or_get_raw_claim`; direct production caller is `graph_claim_shadow_worker._persist_raw_claims` | HIGH: immutable data and retention |
| Decision projection | `ClaimDecisionProjectionV1`; `build_claim_decision_projection`; `create_or_get_claim_decision`; direct production caller is `graph_claim_shadow_worker._persist_decisions` | HIGH: append-only identity and scope |
| Canonical staging | `stage_unit_candidate_occurrences`; called by `graph_extraction_worker._persist_candidate_result` | HIGH: shared worker and candidate writes |
| Candidate aggregation | `recompute_job_candidate_aggregates`; called by `_persist_candidate_result` | HIGH: candidate identity, conflict and aggregate semantics |
| Frozen ontology validation | `validate_job_candidates`; called by `_persist_candidate_result`; relation lookup uses `relation_type_key` in `load_ontology_rule_set_v1` output | HIGH: schema and endpoint gates |
| Candidate routing | `apply_job_candidate_routes`; called by `_persist_candidate_result` | HIGH: confidence and review behavior |
| Materialization | `_publishable_relation_candidates`, `_materialize_job_transaction`, `materialize_graph_extraction_job`; called by both single-unit and batch branches in `run_graph_extraction_worker` and by materializer tests | CRITICAL: entity/relation writes |
| Automatic publication | `auto_publish_graph_extraction_job`; called by both worker branches after materialization | CRITICAL: active product graph |
| Publication planning | `plan_graph_publication`; called by auto-publication and governance publication flows | CRITICAL: publication manifest and scope |
| Publication activation | `activate_graph_publication`; called by auto-publication, the v0.5 publication API, and coordinated purge/governance flows | CRITICAL: active snapshot and rollback |
| Replay/export | `claim_shadow_replay_export` reads raw core, occurrence and decision rows; `claim_shadow_replay_assembler` calls the typed M4C assembler | HIGH: bounded read and evidence re-binding; no write path |

The current direct execution paths are intentionally separate:

```text
Canonical worker path
  GraphExtractionPayload
    -> stage_unit_candidate_occurrences
    -> recompute_job_candidate_aggregates
    -> validate_job_candidates
    -> apply_job_candidate_routes
    -> materialize_graph_extraction_job
    -> auto_publish_graph_extraction_job
    -> plan_graph_publication / activate_graph_publication
```

```text
Phase 2 shadow path
  canonical candidate transaction commits and the unit succeeds
    -> run_shadow_after_canonical
    -> build_raw_claim_from_shadow
    -> create_or_get_raw_claim (new transaction)
    -> _build_decision_projections
    -> create_or_get_claim_decision (separate transaction)
    -> job.statistics.claim_shadow (best effort)
```

`GraphRawClaim` and `GraphClaimDecision` are not currently consumed by
`stage_unit_candidate_occurrences`, candidate validation, materialization, or
publication. The existing relation candidate stores `relation_type_key`; it
does not provide a safe reconstruction of `RawClaimV1.raw_predicate`. Existing
candidate purge can clear candidate payload/evidence while immutable raw claim
rows remain available for replay.

The sanitized M5H fixture confirms this boundary. The v2 artifact and report
were read from:

```text
.run_logs/m5h_canary_20260809_0125/claim-shadow-replay-8cc8012951d2f0ff.json
.run_logs/m5h_canary_20260809_0125/claim-shadow-replay-8cc8012951d2f0ff-report.json
```

It contains three unique raw cores, three occurrences, six pending decisions,
and successful shadow telemetry. Its raw-gold section is explicitly
`raw_gold_not_provided`; it is an operational fixture, not canonical truth.

### 1.3 Safest insertion point

The mapper must run only after a validated immutable raw claim, its occurrence,
its verified evidence references, and the applicable frozen ontology binding
are available. The safe future shape is:

```text
raw claim + occurrence + decision projection
  -> pure scope/evidence/ontology adapter
  -> CanonicalMappingV1 result (append-only sidecar)
  -> optional explicit candidate bridge
  -> existing candidate validation and routing
  -> existing materializer
  -> separately approved publication path
```

The adapter must not be called inline before the canonical extraction
transaction commits. An accepted mapping is not an approval, a materialized
relation, or a publication item. If a candidate bridge is eventually built,
it must re-enter the existing entity/relation candidate validation and
evidence gates. A direct call to materialization or publication is a design
failure.

### 1.4 High-risk warnings

* The worker and provider boundary is HIGH because an accidental inline call
  can change latency, retries, token cost, or canonical unit behavior.
* The candidate and frozen-ontology boundary is HIGH because a mapping can
  silently turn a raw predicate into a relation type or bypass endpoint rules.
* The materializer and publication boundary is CRITICAL because a bad bridge
  can create or activate entities/relations outside the existing gates.
* The persistence boundary is HIGH because mapping results must remain
  auditable after candidate/job purge and must not overwrite earlier decisions.
* Any future edit to an existing symbol requires a fresh GitNexus attempt or
  the AGENTS.md fallback caller audit. This planning document does not replace
  that per-symbol gate.

## 2. CanonicalMappingV1 Contract

The contract is domain-neutral, frozen, `extra=forbid`, and immutable. M0/M1
must implement it as a DB-free model and pure validators before any worker or
ORM integration.

### 2.1 Input

`CanonicalMappingInputV1` contains:

```text
schema_version = canonical_mapping_input_v1
claim: validated RawClaimV1
claim_id: claim.claim_id
extraction_occurrence_id: claim.extraction_occurrence_id
scope: library_id, document_id, document_revision_id, revision_no
surface_raw_predicate: claim.raw_predicate
surface_direction: claim.surface_direction
source_mention: claim.source_mention
target_mention: claim.target_mention
endpoint_type_binding: source and target frozen type keys or explicit null
verified_evidence_ref_ids: non-empty subset of claim.evidence_refs
frozen_ontology: ontology_version_id, ontology_snapshot_hash,
                  ontology_contract_version, bounded relation/type snapshot
```

The repeated scope and surface fields are checked against `claim`; they are
not alternative authority fields. `endpoint_type_binding` must be supplied
by an explicit typed binding. A missing type is an unknown endpoint condition,
not a request to infer `Entity`, `concept`, or another generic type.

The frozen ontology adapter must use the existing frozen snapshot semantics
(`load_ontology_rule_set_v1` in the DB-backed path). A DB-free fixture may
provide a small explicit snapshot, but it may not import a production domain
allowlist or synonym table.

### 2.2 Output

`CanonicalMappingV1` contains at least:

```text
schema_version = canonical_mapping_v1
mapping_attempt_id
mapping_attempt_fingerprint
mapping_result_id
mapping_result_fingerprint
mapping_version >= 1
library_id, document_id, document_revision_id, revision_no
claim_id
extraction_occurrence_id (required)
source_evidence_ref_ids
target_evidence_ref_ids
mapping_evidence_ref_ids
typed evidence_validation_v1 bindings with stable identity/hash and full scope
status = mapped | ambiguous | blocked | rejected
canonical_relation_key (required only for mapped)
canonical_direction (required only for mapped)
canonical_source_endpoint (required only for mapped)
canonical_target_endpoint (required only for mapped)
endpoint_transform and predicate_transform (independent closed enums)
mapping_confidence (bounded 0..1; required for mapped, optional for ambiguous,
                     null for blocked/rejected)
reason_code (closed stable enum, required for non-mapped states)
semantic_projection and semantic_projection_fingerprint
ontology_version_id
ontology_snapshot_hash
ontology_contract_version
mapping_schema_hash
canonical_schema_hash
mapper_key
mapper_version
mapper_version_hash
model_provider (bounded enum/token)
model_version_hash
prompt_version
prompt_content_hash
config_version
config_hash
source/actor/remap provenance
created_at
```

`CanonicalEndpointV1` is a role-specific endpoint reference, not an entity
creation command. It contains the role (`source` or `target`), an explicit
validated entity/entity-candidate reference when available, and the frozen
entity type key. It never falls back to a generic entity or a copied raw
surface as a canonical identity. If an explicit endpoint reference is not
available, the result is non-mapped with a stable reason.

The output stores references, hashes, enums, and bounded identifiers. It does
not duplicate raw quote/text, prompt, response, storage path, DSN, secret, or
unbounded error text. The full input claim remains the immutable raw-claim
authority; artifact serializers must apply the existing M4A2/M4C redaction
rules.

### 2.3 Semantic rules

1. The raw predicate is copied from `RawClaimV1.raw_predicate` and is never
   reconstructed from `GraphRelationCandidate.relation_type_key`, a gold
   fixture, a domain vocabulary, or a synonym table.
2. A mapped result requires the relation key to exist in the supplied frozen
   ontology and the source/target endpoint types and direction to satisfy its
   frozen constraint. Otherwise the result is rejected, ambiguous, or blocked
   with a stable reason.
3. `surface_direction == unknown` preserves the ambiguity. It cannot produce a
   canonical direction, swap source and target, or manufacture a reversed
   relation. Endpoint swap and predicate inverse are recorded independently.
4. Every mapping evidence ref must belong to the claim, be active and verified,
   match the same library/document/revision/job/unit scope, and retain the
   source and target mention evidence refs when those mentions are non-empty.
   Missing, legacy-unverified, stale, conflicting, or cross-revision evidence
   fails closed.
5. `related_to` and any other generic fallback relation are forbidden unless
   explicitly present as a real key in the frozen ontology and independently
   justified by the evidence and endpoint constraint. The mapper may not
   invent such a key as a fallback.
6. A `schema_extension_candidate` remains an unknown endpoint/type proposal;
   it cannot create an ontology row, canonical relation, or generic Entity.
7. A mapping result never mutates `RawClaimV1`, an occurrence, or an existing
   M3A decision projection.
8. Negation, modality, qualifier, valid-time, and effective-time semantics are
   projected and fingerprinted. If the canonical relation shape cannot express
   one losslessly, a closed `unsupported_*` reason blocks or preserves the
   ambiguity; it never becomes an unconditional positive relation.

### 2.4 Stable reason codes

M1 freezes a closed enum. The minimum set is:

```text
unknown_predicate
unknown_source_type
unknown_target_type
unknown_direction
ambiguous_mapping
ambiguous_endpoint
ontology_relation_not_allowed
ontology_snapshot_mismatch
evidence_missing
evidence_invalid
scope_mismatch
no_explicit_mapping
unsupported_negation
unsupported_modality
unsupported_qualifier
unsupported_valid_time
unsupported_effective_time
mapper_error
```

The implementation may add a code only through a contract version change and
focused tests. Model prose and exception text are never reason codes.

## 3. Append-only Identity and Decision Semantics

### 3.1 Fingerprint and UUID

M1 freezes a dedicated canonical-mapping UUID5 namespace; it must not reuse
the M2D2 raw-claim namespace or the M3A decision namespace. The mapping attempt
fingerprint is the SHA-256 of canonical JSON containing:

```text
scope + claim content fingerprint + required occurrence identity/fingerprint
decision identity when present + mapping_version
mapping/canonical schema hashes
frozen ontology version/hash/contract version
explicit endpoint type binding and typed evidence bindings
mapper/model/prompt/config version hashes
```

`created_at` is excluded. The UUID5 is derived from the attempt or result
fingerprint and the dedicated namespace. The result fingerprint additionally
contains outcome/reason/confidence, independent endpoint/predicate transforms,
semantic projection, evidence subset, and remap/source/actor provenance.
Evidence attestation `validated_at` is audit metadata only and is excluded from
binding, attempt, and result identity, so validation retries with a different
observation time reuse the same immutable event.
Identical retries therefore reuse the same event and first `created_at`; a
different occurrence, proposal, ontology snapshot, mapper version, or mapping
version creates a new event. No event is updated in place.

### 3.2 Decision state transitions

The existing M3A/M3B decision rows remain `pending` and append-only:

```text
mapping_candidate (pending)
  -> zero or more versioned mapping result events

schema_extension_candidate (pending)
  -> retained as an unresolved extension event
  -> explicit future ontology/human workflow only
```

The mapper may link a result to a decision ID/fingerprint, but it does not
change the decision status. Mapping results can be `mapped`, `ambiguous`,
`blocked`, or `rejected`; all states are retained. A remapping is a new event
with a new fingerprint/version and an optional `supersedes_mapping_id` link,
never an overwrite. Rollback disables the mapping execution or read/bridge
path and preserves raw claims, occurrences, decisions, and mapping events.

## 4. M0-M5 Execution Plan

### M0 - Boundary and DB-free contract freeze

**Scope**

* Add only a new DB-free contract module and synthetic fixtures/tests.
* Validate `CanonicalMappingInputV1` and `CanonicalMappingV1` against
  `RawClaimV1`, an explicit decision projection identity binding, typed
  verified evidence bindings, and a frozen ontology fixture.
* Keep `build_canonical_mapping` explicitly named and documented as the M0
  pure validated contract factory. It does not infer mappings, call a model,
  or act as the M1 mapper.
* Bind the contract to generated schema hashes, a deep text-free RawClaim
  snapshot, typed evidence attestations, the complete M3A decision projection,
  a content-bound `MappingAuthorizationRegistrySnapshotV1`, complete endpoint
  resolution attestations, explicit registry membership, and separate
  mapper/remap provenance. The registry snapshot is a pure value projection of
  an immutable authority that a future repository must load and prove; M0
  recomputes its manifest hash and checks exact entry membership but does not
  claim external authenticity or perform a database lookup.
  Registry manifests are sorted by complete entry content and reject duplicate
  or conflicting policy identities before snapshot hashing.
  The trusted input factory accepts only bounded raw JSON bytes/string/mapping,
  preserves the canonical pre-coercion payload and its hash, rejects
  coercible semantic booleans and mapping-relevant numeric types before
  `RawClaimV1` validation, and rechecks that authority at every trusted
  serializer/factory boundary; an already parsed/coerced `RawClaimV1` is not
  an authoritative ingress.
  Endpoint-resolution evidence must be present in both the selected verified
  evidence IDs and the corresponding typed validation bindings; claim-level
  membership alone is insufficient. Strict raw JSON parsing rejects duplicate
  object keys at every nesting level, and numeric checks traverse only the
  actual RawClaim evidence/locator schema, never dynamic qualifier JSON.
  Serializer boundaries require the complete `CanonicalMappingInputV1` as an
  authority; a mapping/object payload is never treated as proof, and a bare
  `CanonicalMappingV1.model_validate` is structural parsing, not proof. The
  trusted serializer rebuilds both input and result from
  canonical JSON and rechecks claim/evidence/decision/ontology/authorization
  and every scope field before emitting bytes. Nested provenance, evidence,
  endpoint, semantic, snapshot, and remap models carry self-fingerprints so a
  `model_copy(update=...)` cannot bypass the boundary.
  The factory and trusted result serializer share one input-aware validator for
  mapped and non-mapped outcomes, including unknown-reason facts and the
  closed outcome/semantic-status matrix; an unsupported reason cannot be
  marked as preserved.
* Freeze one outcome enum (`mapped`, `ambiguous`, `blocked`, `rejected`) and
  its reason matrix. A `schema_extension_candidate` decision can only remain
  unresolved; it can never produce `mapped`. `related_to` is allowed only
  when present in the supplied frozen ontology and explicitly authorized for
  the exact surface predicate; there is no fallback relation.
* Keep `remap_generation=0` with empty predecessor fields in M0/M1/M2. Positive
  remaps are fail-closed until M3 can transactionally look up predecessor by
  id, result fingerprint, and complete scope, then verify continuous
  generation, lineage root, and source/actor precedence. A self-consistent
  UUID5 is not evidence that a predecessor exists.
* Record the current separate worker/candidate/materializer/publication flows
  in the implementation notes without changing them.

**Symbols/modules**

* New `app/schemas/canonical_mapping.py` and focused tests.
* Read-only reuse of `app/schemas/raw_claim.py`,
  `app/schemas/claim_decision.py`, `app/services/claim_decision_builder.py`,
  and the evidence locator contract.
* No imports from worker, provider, ORM, migration, materializer, or
  publication code in the pure contract module.

**Tests**

* frozen/extra-forbid/field-bound tests;
* scope, revision, claim, occurrence, ontology hash, and evidence binding
  mismatch rejection;
* forged result/model-copy input, nested RawClaim JSON mutation, strict
  boolean semantics, decision identity, authorization, and evidence
  attestation re-validation; content-bound registry entry membership and
  endpoint/entity-link authority binding; positive/dangling remap rejection;
  and `validated_at` identity exclusion;
* asset/legal/medical synthetic fixtures with no domain allowlist;
* unknown predicate, unknown endpoint type, unknown direction, and explicit
  ambiguity preservation;
* no `related_to` fallback, no endpoint swap, no RawClaim mutation, and no
  raw prompt/response/path/secret field.

**Risk and gate**

HIGH because this is a shared contract boundary. The gate is DB-free tests,
existing RawClaim/decision tests, Ruff, diff check, and an import check proving
the module has no worker/provider/ORM/publication dependency. No existing
production symbol is edited without a new impact/fallback audit.

**Explicit exclusions**

No mapper, model call, persistence, worker hook, candidate write, migration,
ontology mutation, materialization, publication, gold/scorer change, or UI/API.

### M1 - Pure mapping builder and provenance validation

**Scope**

* Build a pure function that accepts a validated raw claim plus an explicit
  mapping proposal and frozen ontology binding.
* Implement deterministic mapping fingerprint/UUID and the stable status/reason
  matrix.
* Require endpoint/evidence references and preserve source/target roles.

**Symbols/modules**

* New `app/services/canonical_mapping_builder.py` and DB-free tests.
* Reuse `stable_evidence_identity`, RawClaim fingerprints, and structured
  Pydantic validation; do not copy the persistence hashing rules.

**Tests**

* explicit known mapping succeeds only when relation key, direction, endpoint
  types, frozen constraint, and evidence all match;
* missing suggested mapping, unknown predicate/type/direction, ambiguous
  endpoint, invalid locator, and evidence scope mismatch fail closed;
* same input retry has identical fingerprint/UUID and first-created-at
  semantics; changed proposal/version/ontology/occurrence produces a new event;
* producer/model/prompt/config hash changes alter identity without leaking
  values;
* all synthetic domains use fixture data only and no allowlist.

**Risk and gate**

HIGH for semantic correctness, but DB-free. The gate is deterministic contract
tests plus M0 and M2D2/M3A focused regression. The builder must never call a
provider or mutate a RawClaim.

**Explicit exclusions**

No automatic mapping inference, domain synonyms, `related_to`, candidate ORM,
decision status mutation, materialization, or publication.

### M2 - Shadow comparison and quality measurement

**Scope**

* Add an eval-only adapter that consumes typed M1 inputs and explicit mapper
  outputs (fake mapper first), then compares raw extraction and canonical
  mapping as separate result families.
* Reuse M4A/M4B artifact/report orchestration only through typed APIs. Do not
  alter v1/v2 artifact contracts or the existing raw scorer.

**Symbols/modules**

* New eval-only mapping comparison module and synthetic fixtures.
* Read-only use of `claim_shadow_replay_assembler`, M4B1 runtime metrics, and
  M4B2 raw-gold scorer; canonical metrics get a separate schema/section.

**Tests**

* perfect, missing, extra, wrong predicate, swapped endpoint/direction,
  unknown retained/dropped, invalid locator, duplicate occurrence, and
  cross-revision cases for asset/legal/medical fixtures;
* order-independent aggregate results and explicit unavailable reasons;
* raw relation recall/precision remains based on unique content claims, while
  canonical mapping accuracy/coverage uses mapped canonical outputs and never
  treats a decision candidate as a canonical relation;
* sanitized M5H artifact loads without being used as gold truth.

**Risk and gate**

MEDIUM/HIGH due to metric contamination risk. The gate requires separate raw
and canonical sections, no changes to `claim_shadow_raw_scorer`, no gold or
semantic-alignment changes, and exact Phase 2 baseline preservation.

**Explicit exclusions**

No production mapper, real model, DB write, worker integration, candidate
bridge, publication, or canonical metric merge.

### M3 - Append-only mapping result persistence

**Scope**

* After M1/M2 acceptance, add an additive mapping-result table and repository.
  Proposed table name: `graph_claim_mappings`.
* Persist the complete versioned result projection, including scope, claim,
  optional occurrence/decision links, ontology snapshot/hash, bounded proposal,
  provenance hashes, status, reason, and first `created_at`.
* Before accepting a positive remap, look up the predecessor transactionally by
  `id + result_fingerprint + complete scope`; verify continuous generation,
  lineage root, and source/actor precedence, then enable only the typed remap
  shape that M0 currently keeps fail-closed. Also verify registry snapshot
  provenance/existence from immutable persistence; M0's pure snapshot hash is
  not an external authenticity proof.
* M3 deliberately rejects the typed `ontology_refresh` remap reason. The
  frozen authority root is the job's immutable ontology snapshot; changing it
  requires a separately versioned authority/source milestone and is not made
  reachable through this persistence API.
* Keep raw claim/occurrence/decision foreign keys `RESTRICT`; mapping rows must
  survive job/candidate purge.

Security note: a plain SHA-256 digest or sidecar can detect corruption, but it
does not authenticate the producer. Producer authentication is deferred until
a separately versioned MAC/signature or immutable external authority exists;
it is intentionally not smuggled into M3.

**Symbols/modules**

* New ORM model, migration after the current head, immutable UPDATE/DELETE
  triggers, repository `create_or_get_canonical_mapping` and scoped read/list.
* Existing `GraphRawClaim`, `GraphRawClaimOccurrence`, and
  `GraphClaimDecision` persistence contracts are read-only dependencies.

**Tests**

* DB-free fake repository: retry reuse, created-at first wins, identity
  conflict, version/proposal remap, scope/occurrence/decision validation;
* ORM checks: bounded JSON, status/reason compatibility, hash fields,
  required endpoint/evidence fields, and no generic fallback;
* disposable PostgreSQL: insert/read/list, duplicate retry, different version
  and proposal, cross-scope rejection, dangling references, immutable
  UPDATE/DELETE rejection, and RESTRICT retention after job/candidate purge;
* no trigger disabling and no cleanup deletion of immutable rows.

**Risk and gate**

CRITICAL database change. Require a single-head migration review, upgrade/
downgrade cycle, unique no-volume PostgreSQL acceptance, M2A/M3B regression,
and a no-side-write assertion. Do not proceed if the table would cascade from
job/candidate deletion.

**Explicit exclusions**

No worker call, automatic decision generation, candidate writes, ontology rows,
materializer/publication changes, API exposure, product database, or model.

### M4 - Quarantine/sidecar bridge behind existing validation

**Scope**

* Design and test an opt-in quarantine/sidecar projection from a `mapped` M3
  result to a candidate-shaped input. It is not a formal materializer or
  publication path in this phase.
* Require explicit entity/entity-candidate endpoint references and frozen
  relation/endpoint validation before creating any canonical candidate.
* Route only through existing candidate evidence validation and quarantine
  checks. If the existing `GraphExtractionPayload` boundary cannot express the
  typed mapping without losing raw/evidence identity, keep the mapping
  sidecar-only and stop. Formal materializer/publication integration requires a
  separately approved milestone.

**Symbols/modules**

* New audited adapter around `stage_unit_candidate_occurrences` or a minimal
  additive candidate-projection helper; any existing-symbol edit needs a new
  impact/fallback audit.
* Existing candidate validation symbols remain read-only boundary references;
  `materialize_graph_extraction_job` and publication symbols are not invoked
  by this quarantine/sidecar milestone.

**Tests**

* mapped/ambiguous/blocked/rejected results produce only their
  permitted candidate effect;
* unknown direction never changes endpoint order;
* invalid/missing evidence, endpoint scope, ontology mismatch, and low
  confidence do not reach materializer;
* raw core, occurrence, decision, canonical job status, existing candidate
  inputs, publication rows, and Phase 2 shadow statistics are unchanged when
  the bridge is disabled or a mapping fails;
* quarantine tests prove no materializer/publication call and no
  entity/relation write before a separately approved bridge passes all gates.

**Risk and gate**

CRITICAL because this is the only proposed boundary toward shared canonical
code. The default must be disabled. Acceptance requires focused quarantine,
candidate-validation, and Phase 0 regression with exact Automatic 3/2 and
Required 91/27 baselines. This milestone must not write through the formal
materializer or create `KnowledgeRelation`, entity, or publication rows.

**Explicit exclusions**

No direct `GraphPublication`/`GraphPublicationItem` creation, no activation,
no automatic ontology/schema extension approval, no raw claim mutation, and
no fallback relation, no formal materializer execution, and no
`KnowledgeRelation`/entity/publication write. M5 is the earliest rollout
handoff gate and does not itself authorize those writes.

### M5 - Controlled rollout and publication handoff gate

**Scope**

* Run a single isolated synthetic canary only after M0-M4 approval. Mapping
  execution must have an independent rollout authorization; Phase 2 shadow
  enablement, Schema confirmation, or an existing candidate status must not
  silently enable it.
* Compare raw and canonical metrics, candidate/quarantine outcomes, evidence
  traceability, latency/cost, and failure/blocked rates. Formal materializer,
  `KnowledgeRelation`, entity, and publication writes remain separately gated.
* Keep publication input unchanged until a separate approval explicitly
  accepts the mapping-to-canonical handoff.

**Tests and operational checks**

* global/library mapping off: zero mapping calls/writes and unchanged old path;
* isolated mapping on: one bounded synthetic run per asset/legal/medical
  fixture, with provider calls, timeout, malformed, evidence, and persistence
  failures isolated from canonical outcome;
* READ ONLY repeatable-read export and M4C artifact/report checks;
* raw relation recall/precision, canonical mapping accuracy/coverage,
  endpoint/direction/evidence validity, unknown retention, ambiguity and
  rejection counts are separate;
* before/after Phase 2 baseline, Required 91/27 and Automatic 3/2, health,
  Graph API/page readability, and publication row counts;
* rollback by disabling mapping execution/read/bridge paths; retain immutable
  data and do not delete historical events.

**Risk and gate**

CRITICAL operational/model/database risk. Stop on scope leakage, evidence
failure, unbounded calls/cost, artifact leakage, canonical status change, or
any Phase 0 regression. Real model use, if ever approved, requires a separate
runbook and explicit authorization; this M0 plan does not authorize it.

**Explicit exclusions**

No backfill, full-library scan, production-wide enablement, direct publication,
gold/scorer/alignment modification, domain allowlist, UI/API, Qdrant, or Dify.

## 5. Cross-Milestone Acceptance Gates

Every milestone must preserve the following invariants:

1. Raw relation recall/precision is measured on unique content-scoped claims;
   occurrence count is reported separately. It is never substituted with
   canonical mapping accuracy.
2. Canonical mapping accuracy/coverage is calculated only from explicit
   mapped results and a compatible canonical gold/fixture. Unavailable
   denominators are `null` plus a stable reason, never zero.
3. Evidence validity is a prerequisite for mapping, not a post-hoc score that
   can be bypassed. Scope includes library, document, revision, job/unit, and
   verified locator/hash/span identity as applicable.
4. Unknown predicate, endpoint type, and direction are retained as
   blocked/ambiguity or rejection diagnostics. They are never silently
   discarded or canonicalized.
5. Phase 2 shadow behavior, raw claim/occurrence/decision fingerprints, job
   status, candidate rows, materialization, publication, and statistics remain
   unchanged when mapping is disabled or fails.
6. Frozen ontology version and snapshot hash are explicit inputs to every
   mapped result. A changed snapshot creates a new result event.
7. All persistence is append-only and idempotent. Retry reuses the same event;
   remapping creates a new versioned event; rollback disables visibility or
   execution and never deletes immutable history.

## 6. First Implementation Instruction

After coordinator approval, implement only M0: add a new DB-free
`CanonicalMappingV1` contract and synthetic tests in a new module, with a
fresh GitNexus impact attempt (or the documented fallback) for every imported
shared symbol. Do not touch `graph_claim_shadow_worker`, canonical provider or
parser, candidate aggregation/validation, materializer, publication, ORM,
migration, gold, scorer, semantic alignment, or any production database.
