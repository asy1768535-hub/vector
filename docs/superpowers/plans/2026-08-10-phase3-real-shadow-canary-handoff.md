# Phase 3 Real Shadow Canary and Production Handoff Gate

Status: implementation in progress. This is a Phase 3 shadow/read-only
acceptance milestone. It does not authorize mapping-to-publication, entity or
relation writes, candidate materialization, or Phase 4/5 work.

## Scope and authority

The canary uses fresh libraries, fresh batches, and the existing upload,
schema-discovery, extraction, raw-claim, evidence, decision, and canonical
mapping boundaries. It may dispatch the existing approved provider only for
the explicitly authorized canary library and batch. Canonical mapping is
read-only/shadow-only and must consume frozen raw/evidence/ontology authority.

The existing `claim_graph_shadow_policy` remains the Phase 2 raw-shadow flag.
This milestone adds an independent canonical-mapping shadow authorization with
both a global setting and an explicit library-level setting. Both gates are
off by default. Schema confirmation, graph extraction enablement, existing
candidate state, and Phase 2 shadow enablement never imply canonical mapping
authorization.

## Rollout contract

The resolved authorization must contain a library id, batch/job scope, policy
version, provider/model identity, and bounded limits. A request is dispatched
only when the global gate, library gate, fresh-library scope, frozen authority,
and evidence validation all pass. Unknown scope, missing evidence, invalid or
truncated model output, timeout, cleanup failure, persistence failure, and
canonical validation failure are isolated as stable failed/unavailable
outcomes. They never change raw claims, occurrences, decisions, job status,
statistics, candidates, materialization, or publication input.

The canary has finite limits for documents, extraction units, mapper/provider
calls, output tokens, retries, bytes, and wall-clock lifetime. It uses the
M5 bounded cancellation contract: a no-write synthetic or approved provider
adapter may respond to a bounded cleanup cancellation; an untrusted coroutine
that suppresses all cancellation requires a separate process boundary and is
unsupported here.

## Artifact and metrics contract

Every attempt emits a sanitized, auditable artifact with stable schema/version,
scope, authorization facts, provider/model identity, dispatch count, latency,
token/finish metadata, outcome, failure code, and canonical mapping identity.
Prompts, model responses, raw text, source paths, credentials, DSNs, and gold
are excluded. Failed attempts retain failed status and `metrics=null` plus a
stable unavailable reason; they never become zero or perfect scores.

Raw metrics and canonical metrics remain separate. Raw recall/precision is
computed from unique content claims. Canonical accuracy/coverage is computed
only with compatible offline gold and explicit mapped results. Endpoint,
direction, evidence validity, unknown retention, ambiguity/rejection,
latency/cost, blocked, and failure metrics are independent sections.

`gold_standard.json` is never uploaded, inserted, sent to a provider, included
in a prompt/request, or used while producing the frozen production artifact.
It is read only by a separate offline scorer after the artifact is finalized.
Missing/incompatible gold produces null canonical metrics with a stable reason.

## Real acceptance order

1. Markdown Stage 1: create a fresh required/explore library, upload only
   `01-06` Markdown files, complete schema discovery/frozen schema and raw
   extraction, run canonical shadow, freeze sanitized artifacts, then read the
   separately held gold for offline scoring. Gold is 20 entities and 18
   relations. The threshold gate is entity recall >= 0.90, alias precision >=
   0.95, raw relation recall >= 0.80, endpoint/direction >= 0.90, canonical
   predicate accuracy >= 0.70, evidence validity = 1.00, semantic relation
   recall >= 0.60, and unsupported high-risk claim = 0.
2. DOCX Stage 2: only after Stage 1 structural closure and no gold leak, use a
   fresh library and upload the five small DOCX files. Report parser,
   section/chunk, evidence, raw claim, and canonical shadow results. No gold
   recall is claimed where no compatible gold exists.
3. DOCX Stage 3: only after Stage 2 passes, use another fresh library for
   `4.测试报告.docx`. Validate bounded parsing, memory/task stability, timeout,
   and evidence traceability. It remains shadow-only.

The README and `gold_standard.json` in the Markdown source directory are never
uploaded. Existing Required 91/27 and Automatic 3/2 graph data is not reused,
overwritten, or used as a canary target.

## Administrator configuration contract

The administrator UI is a separate, minimal task. This backend exposes the
following independent fields on the existing library create/update/read
contract; none aliases `graph_assisted_chat_mode` or
`claim_graph_shadow_policy`:

* create/update: `canonical_mapping_shadow_policy`, one of `inherit`,
  `disabled`, or `enabled`; the create default is `inherit` and PATCH omits the
  field to leave it unchanged;
* read-only: `canonical_mapping_shadow_global_enabled`, copied from the
  repository setting, and `canonical_mapping_shadow_resolved`, computed by the
  backend from the global gate, library policy, and schema eligibility;
* paths: `POST /admin/libraries` and `PATCH /admin/libraries/{slug}` accept the
  policy; `GET /admin/libraries` and `GET /admin/libraries/{slug}` return all
  three fields;
* rollback: PATCH the library with
  `{"canonical_mapping_shadow_policy":"disabled"}`, then GET the same
  library. Rollback is successful only when the response is 2xx and
  `canonical_mapping_shadow_resolved` is exactly `false`; a failed GET or any
  other value is a failed rollback, not a success;
* no scoped latest-run/status projection API exists in this milestone. The UI
  must not manufacture one or expose raw claims; the missing status projection
  is an explicit later boundary.

`inherit` is fail-closed until the library is explicitly set to `enabled`;
the global gate remains required. A global-off response therefore reports
`canonical_mapping_shadow_resolved=false` even when the stored policy is
`enabled`. The setting is independently default `false`, and schema-disabled
libraries are never eligible.

## Rollback and stop conditions

Rollback is performed through the formal library API by setting the canonical
mapping library flag to disabled, then read-only verifying `resolved=false`.
It is idempotent and is mandatory after every stage, including failure. No
immutable raw, occurrence, decision, evidence, mapping, or artifact history
is deleted.

Stop immediately on scope leakage, a call/timeout/byte budget breach, gold
leakage, missing or cross-scope evidence, non-frozen authority, canonical
write/publication call, raw-path regression, unsupported high-risk claim,
threshold failure, or any existing baseline regression. A failed quality gate
may receive one evidence-based minimal fix and one rerun; gold, scorer,
semantic alignment, and fixture expectations cannot be changed.

## Verification

Before real execution, run DB-free tests for default-off and dual-gate
authorization, scope and gold-leak guards, timeout/truncation isolation,
rollback, no-write/import/caller guards, and the existing M0-M5/candidate/
evidence/routing/publication regression sets. Then use only a newly approved
isolated PostgreSQL/API/provider environment for the three stages. Retain
sanitized ids, artifact hashes, resolved non-secret policy facts, metrics, and
rollback proof. Do not connect to a product database and do not enter
mapping-to-publication or Phase 4/5.
