# Public API Operations

M7 adds optional operational controls to the accepted `/api/v1` read facade. It
does not change response bodies, SSE event payloads, authorization, or knowledge
storage. All controls are disabled by default.

## Enablement

Operations require the public API and Organization authorization. Limits require
operations. Enable in this order:

```dotenv
PUBLIC_API_V1_ENABLED=true
ORGANIZATION_AUTHORIZATION_ENABLED=true
PUBLIC_API_OPERATIONS_ENABLED=true
PUBLIC_API_LIMITS_ENABLED=true
```

The remaining public v1 dependencies must also be enabled as described in
[Public Read API v1](./public-api-v1.md). Invalid dependencies or numeric bounds
stop API and cleanup-worker startup.

## Defaults

```dotenv
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

Hosted Organizations are limited when limits are enabled. Private Organizations
remain unlimited unless `PUBLIC_API_LIMIT_PRIVATE_ORGANIZATIONS=true`. Cookie
requests use the Organization limits only. API-Key requests use both the
Organization and API-Key limits in one atomic transaction.

Request limits use PostgreSQL fixed one-minute windows. Answer limits use
PostgreSQL leases. The Organization row is locked before the API-Key row. The
answer wall-clock timeout is always at least 30 seconds shorter than the lease,
so a crashed API process recovers capacity through lease expiry without a
renewal task.

## Limit Response

A rejected request returns an ordinary HTTP response before retrieval, model
work, or SSE starts:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 17
X-Request-Id: 0123456789abcdef0123456789abcdef
Content-Type: application/json
```

```json
{
  "error": {
    "code": "rate_limited",
    "request_id": "0123456789abcdef0123456789abcdef",
    "message": "The public API request limit was reached.",
    "details": []
  }
}
```

`Retry-After` is an integer derived from the current minute boundary or the
earliest blocking answer-lease expiry. A denied multi-scope request consumes no
counter and creates no lease. Operational database or invariant failure is
fail-closed as the existing content-free `service_unavailable` response.

## Stored Operational Data

One idempotent terminal row is recorded for an authenticated official v1
request. It may contain only UUID identities, endpoint and method, selected
Library UUIDs, timestamps and duration, HTTP status, stable error and outcome,
bounded result counts, stream state, answer model identity, and provider token
counts when the provider supplies them.

The operational schema, writes, logs, errors, tests, and fixtures must never
contain questions, answers, prompts, partial output, Chunk or Evidence text,
graph properties, document titles, aliases, URLs, object locators, credentials,
headers, IP addresses, user agents, provider payloads, or raw exceptions.

Unknown routes, disabled public API requests, and requests that do not
authenticate create no operational row. Requests that authenticate but fail
before Organization resolution are recorded but do not consume Organization
quota. Ordinary reads still do not create administrator audit rows or
sensitive-file-read events.

## Retention And Cleanup

The existing cleanup worker owns maintenance; no new service is required.

```powershell
python -m app.workers.cleanup
python -m app.workers.cleanup --watch
```

One-shot mode runs operational maintenance once. Watch mode runs it no more
often than `PUBLIC_API_CLEANUP_INTERVAL_SECONDS`. Each transaction deletes at
most `PUBLIC_API_CLEANUP_BATCH_SIZE` expired request records, obsolete rate
windows, and expired answer leases per category. Row locks with `SKIP LOCKED`
make multiple cleanup workers safe. Logs contain aggregate counts only.

## Failure And Rollback

Terminal-record or lease-release failure never replaces a response already
produced. Lease expiry and scheduled cleanup recover abandoned rows. Admission
failure while limits are active remains fail-closed.

For runtime rollback, first set:

```dotenv
PUBLIC_API_LIMITS_ENABLED=false
```

This keeps content-free recording but removes M7 rejections. If operational
writes are unhealthy, also set:

```dotenv
PUBLIC_API_OPERATIONS_ENABLED=false
```

That restores exact M6 runtime behavior. Existing records remain until normal
retention. A database rollback requires both flags disabled, then downgrades
`0040` to `0039`; it removes only the three `public_api_*` operational tables.
