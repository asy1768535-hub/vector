# Phase 2 M2D2 Shadow Worker Boundary

M2D2 is a best-effort worker hook. The canonical graph extraction transaction
commits candidate rows and marks the extraction unit succeeded before any
shadow request is built. Shadow claim writes use a new session and transaction;
failure rolls back only that transaction and never changes canonical unit,
candidate, materialization, or publication state.

## Rollout

The effective decision is the M2D1 global setting plus the Library
`inherit|enabled|disabled` policy, with `graph_extraction_enabled` and
`external_llm_enabled` as safety gates. The global default is off. A
shadow-enabled Library is excluded from the production batch claimant so it is
processed through the single-unit worker hook. Batch processing does not have
a second shadow implementation.

## Provenance And IDs

Shadow provenance is fixed to `graph-claim-shadow-v1`,
`shadow-raw-claim-v1`, `shadow-response-parser-v1`, and
`surface-text-normalization-v1`. Shadow does not use the canonical ontology;
its ontology snapshot hash is null. Deterministic claim and occurrence UUIDs
use the namespace `f0a4d99e-1e78-5a9e-8d26-cd90a3f3d5e7`, through the existing
RawClaimV1 fingerprint helpers.

## Evidence And Metrics

Only active evidence in the unpurged extraction context snapshot is eligible.
The locator must be verified and must match revision, quote, unit text, and
scope. Missing, legacy-unverified, stale, or conflicting evidence is a stable
skip/failure, never an inferred locator. `job.statistics.claim_shadow` stores
bounded counters, stable reason codes, latency totals/counts, and token
counts. It does not store prompt, response, quote, file hash, storage path,
object key, secret, or exception text. Metrics are updated in a locked short
transaction and a metrics failure is ignored.

## Rollback

Disable the global flag or set the Library policy to `disabled`. This stops
new shadow calls and reads/writes but does not delete immutable claims or
evidence. Existing canonical publication remains the only authoritative graph
path.

## Verified Acceptance Boundary

The DB-free worker suite covers cache-hit and canonical-failure skips, dispatch
re-authorization, bounded shadow output budget, provider/parser failures, and
canonical-result preservation when the shadow hook raises. The disposable PG
acceptance covers the real context snapshot to verified locator/evidence
adapter, two context refs (`c0` and `p1`), RawClaim insert/read, batch claimant
routing for global/policy and safety-gate combinations, and concurrent locked
statistics updates. The combined M2A plus M2D2 PG invocation passed with four
tests. No real model, product database, canary, or production backfill was
used; those remain outside this milestone.
