# Raw Claim Candidate Boundary Decision

Status: Phase 2 M1 completed. This is an offline boundary decision only.
No migration, ORM model, worker shadow write, production database, or model
call was added or run.

## Scope And Audit Method

The replay fixture follows one relation through the existing DB-free portions
of the pipeline:

```text
raw JSON
  -> parse_graph_extraction_output
  -> parse_batched_graph_extraction_output
  -> GraphExtractionPayload relation.model_dump()
  -> claim endpoint-local-id validation
  -> relation_candidate_key_v1
  -> aggregate_relation_occurrence_payloads
  -> candidate validation boundary classification
  -> existing candidate purge projection
```

The historical helper was
`app/services/raw_claim_candidate_replay.py`. It called the real parser,
batch parser, candidate-key helper, and relation aggregation function. It does
not call ORM staging because `stage_unit_candidate_occurrences` requires a
Postgres session and a complete job/unit/snapshot graph. The replay therefore
marks candidate validation as a boundary classification, not as a substitute
for the DB-backed `validate_job_candidates` call. Before occurrence staging, it
also checks that the parsed relation source/target local IDs exactly match the
RawClaim mention local IDs; a declared-but-mismatched endpoint is rejected and
is never assigned a candidate key. Existing PG/purge tests were used to verify
the persistence facts separately.

The audited high-risk symbols and flows are:

| Area | Reused or audited symbols | Risk |
|---|---|---|
| Parser | `parse_graph_extraction_output`, `parse_batched_graph_extraction_output`, `GraphExtractionPayload` | HIGH |
| Staging | `stage_unit_candidate_occurrences`, `_upsert_relation_candidate`, `_upsert_relation_occurrence` | HIGH |
| Aggregation | `relation_candidate_key_v1`, `aggregate_relation_occurrence_payloads`, `recompute_job_candidate_aggregates` | HIGH |
| Validation | `validate_job_candidates` | HIGH |
| Worker | `_persist_candidate_result` | HIGH |
| Purge | `_purge_candidate_payloads`, `purge_revision_graph_extraction_payloads` | HIGH |

GitNexus was attempted with `npx gitnexus analyze`, but failed before indexing:
`tree-sitter-kotlin` could not load `node-gyp-build` under Node.js
v24.18.0. The fallback audit used `git grep`, targeted source reads, and the
existing focused tests. No existing high-risk symbol was changed.

## Replay Counts

The normal known-relation fixture contains one relation and two declared
entities.

| Stage | Input relations | Output relations | Result |
|---|---:|---:|---|
| Raw JSON | 1 | 1 | accepted |
| Direct parser | 1 | 1 | accepted |
| Batch parser | 1 | 1 | accepted |
| Claim endpoint validation | 1 | 1 | source/target local IDs matched |
| Occurrence `raw_payload` | 1 | 1 | exact relation model dump |
| Candidate key/staging boundary | 1 | 1 | candidate key generated |
| Relation aggregation | 1 | 1 | properties/confidence aggregate |
| Candidate validation boundary | 1 | 1 | `aggregated` for known relation/endpoints |
| Existing candidate purge | 1 | 0 retained payload | raw/properties/evidence quote cleared |

The following variants locate loss precisely:

- A surface predicate such as `is alleged to support` is not present in the
  provider/parser relation contract. The parser receives only the canonical
  `relation_type_key` `supports`, so the surface raw predicate is already
  unavailable before parser input; neither occurrence nor candidate can
  recover it. The helper reports this as `raw_predicate: unavailable`, not as a
  match against `relation_type_key`.
- An unknown relation key is only an unknown canonical relation key. With no
  routed allowlist, parser, batch parser, occurrence, and aggregation retain
  that key; validation classifies it as `rejected / schema_extension_candidate`.
- An unknown relation key with `allowed_relation_type_keys={"supports"}` is
  rejected by the batch parser before occurrence creation.
- Unknown endpoint type: the relation reaches occurrence and validation, then
  is classified as `schema_extension_candidate`.
- Undeclared endpoint local ID: direct parser rejects the payload; the batch
  parser's `_without_undeclared_endpoint_relations` removes the relation and
  emits zero relations. This is the earliest loss point for this case.
- Declared endpoint local IDs that do not match the RawClaim source/target
  mention IDs are rejected by the replay boundary before occurrence/candidate
  association with `endpoint_local_id_mismatch`; this prevents a claim
  fingerprint from being silently attached to an unrelated relation.
- Failed/no-persist batch: no occurrence or candidate payload exists to retain.
  A previously staged rejected candidate is subject to the same existing purge
  as a successful candidate.

## Field Fidelity Matrix

`preserved` means the value is available at that boundary; `transformed` means
it is represented by a different existing field; `unavailable` means the
boundary has no field for it; `removed` means the existing purge clears it.
The opaque relation properties in the fixture intentionally contain the raw
claim semantic fields to measure the best case for the current JSON boundary.

| RawClaim field | Parser | Occurrence raw payload | Candidate/aggregate | Purge |
|---|---|---|---|---|
| raw predicate | unavailable: absent from provider/parser input | unavailable | unavailable; cannot be reconstructed from a canonical key | removed/unavailable |
| canonical relation type key | preserved as `relation_type_key` | preserved | transformed into the candidate key/type-key boundary; aggregate does not carry a typed raw predicate | removed with candidate/occurrence |
| source/target local ID | preserved | preserved | transformed to source/target candidate IDs | removed with occurrence |
| source/target surface | available in entity payload | unavailable in relation payload | unavailable in relation candidate/aggregate | removed with entity occurrence |
| surface direction | opaque `properties` only | preserved as opaque JSON if supplied | preserved as opaque `proposed_properties`; not typed | removed |
| negation | opaque `properties` only | preserved as opaque JSON if supplied | preserved as opaque `proposed_properties`; not typed | removed |
| modality | opaque `properties` only | preserved as opaque JSON if supplied | preserved as opaque `proposed_properties`; not typed | removed |
| qualifiers | opaque `properties` only | preserved as opaque JSON if supplied | preserved as opaque `proposed_properties`; not typed | removed |
| valid/effective time | opaque `properties` only | preserved as opaque JSON if supplied | preserved as opaque `proposed_properties`; not typed | removed |
| evidence refs | relation evidence becomes `context_ref`/quote; typed RawClaim refs are only opaque properties | relation evidence list preserved; no typed ref identity | candidate evidence is separate context/quote resolution, not RawClaim refs; multiple RawClaim refs cannot be represented as typed edges | quote and matches cleared |
| library/document/revision scope | not in payload | job/unit are external columns; library/revision are not in raw relation JSON | job/library/ontology are columns; revision/unit claim scope is not a claim core | payload is cleared |
| job/unit scope | not in payload | `GraphRelationOccurrence.job_id` and `extraction_unit_id` columns | candidate has job ID; unit occurrence is separate | row remains only as purged audit shell |
| extractor/model/prompt/parser/config/ontology provenance | unavailable in relation payload | unavailable except external job/unit columns | ontology version is present; model/prompt/parser/config are unavailable | payload is cleared |

The opaque-property result is deliberately not treated as lossless RawClaim
support: it is untyped, may be interpreted as canonical relation properties,
and is erased by candidate purge.

The multi-evidence fixture binds source mention to `e1`, target mention and a
qualifier to `e2`, and carries both refs only inside generic relation
properties. The candidate evidence boundary still exposes only the ordinary
relation `context_ref`/quote shape, and the existing purge clears both opaque
properties and evidence payload. This is a carrier observation, not durable
RawClaim evidence retention.

## Idempotency, Scope, And Retention Facts

- The existing occurrence identity is `(extraction_unit_id, ordinal)`.
- The existing candidate identity is `(job_id, candidate_key)`.
- `relation_candidate_key_v1` uses ontology version, source candidate key,
  relation type key, target candidate key, and properties. It does not include
  RawClaim content fingerprint, revision identity, occurrence ID, model, or
  prompt provenance.
- Replaying the same relation with different job/unit/model provenance produces
  the same candidate key when the ordinary relation properties are unchanged.
  The RawClaim content fingerprint remains stable and the occurrence
  fingerprint changes, but the current candidate boundary does not store those
  fingerprints.
- Different document revisions remain separated in the job/unit scope during
  staging, but can produce the same candidate key. Revision identity is not a
  durable candidate-content key.
- Existing purge keeps row identities and timestamps but sets
  `GraphRelationOccurrence.raw_payload` to NULL,
  `GraphRelationCandidate.proposed_properties` to NULL, and candidate evidence
  quote/matches/source span payloads to NULL/empty values. The SQL check
  constraints explicitly model this purged state.
- Existing purge is covered by `tests/test_v04_m5_purge.py` and PG integration
  coverage; this M1 replay does not execute a database purge.

## Decision

### Short-lived candidate carrier: sufficient with limits

The current candidate boundary can carry a known relation's ordinary payload,
and even arbitrary raw-claim semantic fields when a producer explicitly puts
them into generic relation `properties`. The existing parser, occurrence JSON,
candidate `proposed_properties`, and aggregation preserve that opaque JSON for
the short lifetime of the job.

### Phase 2 immutable raw claim retention: insufficient

The existing boundary is not sufficient for the Phase 2 objective of retaining
every legal evidence-backed RawClaimV1. The minimum missing capabilities are:

1. An immutable claim core keyed by library/revision/content fingerprint.
2. Separate extraction occurrence identity and full extractor/model/prompt/
   parser/config provenance.
3. Typed preservation of surface direction, negation, modality, qualifiers,
   time intervals, evidence ref identity, locator/hash, and claim schema
   version rather than an opaque relation-properties convention.
4. Retention independent from job/candidate purge, including rejected,
   unknown-predicate, unknown-endpoint, and unknown-direction claims.
5. A durable distinction between raw claim status and canonical candidate status;
   current candidate status is canonical-pipeline state and can become
   `rejected`, `materialized`, `superseded`, or purged.

The first loss of `raw_predicate` is therefore before or at the provider
contract/parser input, not in candidate aggregation. M2 shadow capture must
capture the original model field at the boundary where it still exists; it
must not infer a surface predicate from `relation_type_key`.

Therefore M2 may design an additive claim core plus occurrence persistence and
independent versioned decision projection. It must not promote
`raw_payload`/`proposed_properties` into the long-term contract and must not
change the current candidate, materializer, publication, or purge success
conditions.

## Test And Performance Evidence

New offline replay tests: `15 passed`, including endpoint mismatch rejection,
natural-language raw predicate versus canonical key, multi-evidence opaque
retention, and asset/legal/medical/ordinary domain-neutral fixtures.

Related existing tests:

- candidate aggregation, batch parser, and purge: `36 passed`;
- the replay helper's 20-sample wall-time measurement: p50 `0.165 ms`,
  p95 `0.207 ms` on the local test process.

These timings cover JSON parsing, existing Pydantic parsing, candidate-key
hashing, and one in-memory relation aggregation only. They are not DB, worker,
provider, or production latency measurements.

## M1 Gate

M1 decision: **candidate reuse is insufficient for immutable RawClaimV1
retention; M2 additive persistence design is permitted.** No M2 code, ORM
model, migration, shadow flag, worker write, real model call, evaluator change,
or product database operation was performed in this milestone.
