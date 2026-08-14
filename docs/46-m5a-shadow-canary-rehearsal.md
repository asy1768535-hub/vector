# Phase 2 M5A Shadow Canary Rehearsal

Status: **rehearsal complete; M5B real-model canary not authorized**

This runbook records the preflight boundary for a future shadow canary. M5A
uses fake providers only. It must not connect to a product database, Dify,
Qdrant, a historical job, or a real model endpoint.

## Isolation

- Create a unique library name such as `m5b-shadow-<run-id>` in a disposable
  organization. Use one new document, revision, job, and extraction unit.
- Use a PostgreSQL 16 container with a unique name, localhost-only high port,
  no host volume, a dedicated temporary database, and a temporary credential.
  Inject the DSN only into the test process. Never print or persist the DSN.
- Do not use `docker compose down`, `docker-db_postgres-1`, Dify containers,
  product Qdrant, historical tasks, or existing library data.
- Write artifacts only below a run-specific output directory. Do not write
  prompts, responses, quotes, source text, paths, credentials, or DSNs.

## Authorization And Budget

Before M5B, record the resolved authorization for the isolated Library:

| Setting | Required preflight value |
|---|---|
| `graph_claim_shadow_enabled` | Explicitly reviewed; default remains `false` |
| `claim_graph_shadow_policy` | `enabled` for the canary library, or `inherit` with global `true` |
| `graph_extraction_enabled` | `true` |
| `external_llm_enabled` | `true` only for the isolated canary library |
| model endpoint | Explicitly supplied and allowlisted for this run |
| maximum calls | A written finite cap, starting at one unit per run |
| timeout/retries | Written before execution; retries must not exceed the cost cap |

The global flag and Library policy must be independently auditable. Automatic
or required canonical policies do not authorize shadow extraction. Global off,
Library disabled, graph extraction disabled, or external LLM disabled must
resolve to shadow off.

## Phase 0 Gate

Run the Phase 0 Required and Automatic baseline checks against the same code
tree and record their exact commands and results. Required baseline failures
stop the run. Automatic baseline failures stop the run unless an explicitly
reviewed waiver records the failure, owner, and expiry. Do not use a shadow
artifact or a synthetic gold score to waive a canonical baseline failure.

## Rehearsal Matrix

M5A must exercise fake-provider success and each best-effort failure without
changing canonical job status, candidate rows, materialization inputs, or
publication readiness:

- global off with `inherit`;
- global off with `enabled`;
- global on with `inherit`, `disabled`, and `enabled`;
- provider truncation, timeout, malformed response, evidence/build failure,
  decision persistence failure, and export failure;
- rollback after each run by setting the global flag off or the Library policy
  to `disabled`.

The success path must read real immutable PostgreSQL rows through M4D, assemble
the v2 artifact through M4C, and keep M4B1 structural metrics separate from
M4B2 synthetic raw-gold metrics. A failed artifact must retain its actual
failed status, completed stages, and stable error code with `metrics = null`.

## Stop Conditions

Stop immediately and turn the flag off if any of these occur:

- an unexpected provider call, call count or latency/cost budget breach;
- any product DSN, non-isolated Library, Dify, Qdrant, or historical job is
  selected;
- canonical status, candidate/materialization/publication state, or canonical
  request identity changes;
- evidence scope, revision, locator, or hash validation fails unexpectedly;
- an artifact contains raw text, prompt/response content, paths, credentials,
  file hashes, or other unredacted identifiers;
- a failed shadow run is reported as a successful empty extraction.

## Rollback And Evidence

Rollback is configuration-only: set `graph_claim_shadow_enabled = false` and,
for the canary Library, set `claim_graph_shadow_policy = disabled`. Verify new
shadow calls and reads/writes stop while existing immutable raw claims,
occurrences, decisions, and evidence remain retained. Do not delete immutable
rows and do not roll back canonical publication.

For every rehearsal, retain only the sanitized v2 artifact/report, command
hashes, resolved non-secret policy facts, pass/fail status, call counts, and
cleanup proof. Remove the disposable container by its exact unique name and
verify that the name no longer exists. M5A does not authorize real-model
execution; M5B requires a separate approval after this checklist passes.
