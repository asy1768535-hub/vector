# Public API Operations

## Scenario: Content-Free Hosted Operations

### 1. Scope / Trigger

Use this contract when changing operational recording, request admission,
concurrent answer capacity, retention, or cleanup for the official `/api/v1`
facade. It extends the M6 public-read contract and does not authorize scope,
store customer content, add a frontend, or govern non-public routes.

### 2. Signatures

Environment defaults:

```text
PUBLIC_API_OPERATIONS_ENABLED=false
PUBLIC_API_LIMITS_ENABLED=false
PUBLIC_API_LIMIT_PRIVATE_ORGANIZATIONS=false
PUBLIC_API_ORGANIZATION_REQUESTS_PER_MINUTE=600
PUBLIC_API_API_KEY_REQUESTS_PER_MINUTE=120
PUBLIC_API_ORGANIZATION_CONCURRENT_ANSWERS=20
PUBLIC_API_API_KEY_CONCURRENT_ANSWERS=4
PUBLIC_API_ANSWER_MAX_SECONDS=300
PUBLIC_API_ANSWER_LEASE_SECONDS=330
PUBLIC_API_REQUEST_RETENTION_DAYS=30
PUBLIC_API_CLEANUP_BATCH_SIZE=1000
PUBLIC_API_CLEANUP_INTERVAL_SECONDS=3600
```

Migration `0040` adds exactly:

```text
public_api_request_records(request_id UNIQUE, identity UUIDs, endpoint/timing,
                           terminal status, bounded counts/model/tokens)
public_api_rate_windows(scope_kind, scope_id, window_started_at, request_count)
public_api_answer_leases(request_id UNIQUE, organization_id, api_key_id,
                         endpoint_key, acquired_at, expires_at)
```

Core service boundaries:

```python
admit_public_request(scope, session_factory, config, at=None) -> PublicRateAdmission
acquire_public_answer_lease(request_id, endpoint_key, organization_id,
                            api_key_id, session_factory, config, at=None)
release_public_answer_lease(request_id, session_factory) -> bool
record_public_operation(record, session_factory, config) -> bool
run_public_api_operations_maintenance(db, at=None, batch_size=None, config=settings)
    -> PublicOperationsMaintenanceResult
```

### 3. Contracts

- Operations require public v1 and Organization authorization. Limits require
  operations. Invalid bounds fail API and cleanup-worker startup even when the
  feature flags are off.
- An authenticated official route creates one request-local
  `PublicOperationContext`. Unknown catch-all, disabled-v1, and unauthenticated
  requests never create a context or row.
- Scope authorization remains owned by the existing Organization, compatibility,
  Catalog, and Graph Catalog services. Admission occurs only after authoritative
  scope resolution and before Catalog, federation, graph search, or provider work.
- Hosted Organizations are enforced when limits are enabled. Private
  Organizations are unlimited unless explicitly opted in. Cookie requests have
  no API-Key counter.
- Fixed one-minute Organization and API-Key increments occur in one independent
  PostgreSQL transaction. A denial rolls back every increment. Never implement
  this with process memory or separate per-key commits.
- Answer admission locks the active Organization row first and the active API-Key
  row second, counts only unexpired leases, and inserts one request-ID lease.
  Empty-evidence and pre-provider failures acquire no lease. The answer timeout
  is at least 30 seconds shorter than the lease.
- Sync and SSE success, failure, timeout, cancellation, and disconnect release
  independently and finalize once. Process crashes recover through lease expiry.
- Terminal records use `INSERT ... ON CONFLICT (request_id) DO NOTHING` in an
  independent session. Record/release failure cannot replace a completed response.
- Stored fields are allowlisted identifiers, endpoint/method, Library UUIDs,
  timing/status/outcome, bounded counts, model identity, and provider token counts
  when supplied. Never store or log questions, answers, prompts, partial output,
  Chunk/Evidence text, titles, graph properties, URLs, object locators,
  credentials, headers, network identity, provider payloads, or raw exceptions.
- The existing cleanup worker deletes old terminal rows, obsolete windows, and
  expired leases in separate bounded `FOR UPDATE SKIP LOCKED` batches. One-shot
  runs once; watch mode respects the configured interval.
- When operations are disabled, API call signatures omit the optional operations
  keyword entirely. This preserves M6 function and test-double compatibility.

### 4. Validation & Error Matrix

| Condition | Stable result |
|---|---|
| Operations enabled without public v1 or Organization authorization | startup failure |
| Limits enabled without operations | startup failure |
| Invalid quota, timeout/lease, retention, batch, or interval | startup failure |
| Authenticated request fails before Organization resolution | record, no quota |
| Any applicable fixed window is full | `429 rate_limited`, integer `Retry-After`, no increments |
| Any applicable answer scope is full | `429 rate_limited` before SSE starts, no lease |
| Admission database/invariant failure | `503 service_unavailable`, fail closed |
| Terminal record or lease-release failure | preserve response; fixed content-free warning |
| Stream provider failure after HTTP 200 | one `error` event, release, terminal `failed` row |
| Client cancellation or disconnect | close provider, release, `cancelled` or `disconnected` row |
| API process crash | capacity reusable after lease expiry |

### 5. Good/Base/Bad Cases

- Good: a hosted API-Key request atomically increments its Organization and Key
  windows, calls retrieval, records UUID/count metadata, and stores no query text.
- Good: a limited stream receives ordinary HTTP 429 with `Retry-After`; no SSE
  event, provider call, counter increment, or answer lease is produced.
- Good: two cleanup workers lock different expired rows and preserve records
  inside retention.
- Base: operations off omits the context keyword and reproduces M6 responses,
  SSE events, calls, and errors.
- Bad: creating a context before authentication permits unauthenticated storage
  amplification and is forbidden.
- Bad: incrementing Organization and API-Key counters in separate transactions,
  locking API Key before Organization, or renewing leases in a process-local loop
  breaks multi-process correctness.
- Bad: parsing response bodies to derive metrics or logging caught SQL/provider
  exceptions violates the content-free boundary.

### 6. Tests Required

- Contract tests assert all flags default off, dependency/bound validation, exact
  allowlisted record fields, strict UUID/order bounds, 429 message/header, all 11
  unique endpoint keys, ORM/migration parity, one Alembic head, and offline
  upgrade/downgrade.
- API tests assert authenticated validation failures record, unknown and
  unauthenticated requests do not, API-Key UUID is nullable/correct, M6 disabled
  calls omit the new keyword, and 429 occurs before SSE.
- Lifecycle tests assert sync/SSE success, upstream failure, timeout, no-evidence,
  cancellation, and disconnect release/finalize exactly once without content.
- A real disposable PostgreSQL gate is mandatory: independent sessions must not
  exceed Organization/API-Key request or answer limits; partial denial must roll
  back; lease expiry must recover capacity; concurrent cleanup must be bounded and
  idempotent; `0040 -> 0039 -> 0040` must preserve non-M7 tables. A skipped gate
  cannot declare this milestone accepted.
- Run inherited public v1, auth, Organization, cleanup, Catalog/graph, federation,
  and chat suites; full non-frozen pytest; Ruff; compileall/import/OpenAPI smoke;
  release safety; Alembic heads/history/offline SQL; and `git diff --check`.

### 7. Wrong vs Correct

Wrong:

```python
if organization_count >= limit:
    raise HTTPException(429, detail=str(provider_error))
organization_count += 1
await db.commit()
api_key_count += 1
await db.commit()
```

Correct:

```python
scope = await authorize_and_resolve_scope(db, user, selection)
await admit_public_request(
    PublicAdmissionScope(
        organization_ids=(scope.organization_id,),
        api_key_id=operation_context.api_key_id,
    )
)  # all counters commit or roll back together in an independent transaction
result = await run_authoritative_public_service(scope)
```

The correct boundary preserves authorization ownership, multi-process atomicity,
content-free errors, and M6 behavior when operations are disabled.
