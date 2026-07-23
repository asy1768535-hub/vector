# Public Read API v1

## Scenario: Stable Evidence-Grounded Read Facade

### 1. Scope / Trigger

Use this contract when adding or changing the additive `/api/v1` product API for
Library discovery, document/graph/Evidence reads, cross-Library retrieval, or
stateless grounded answers. The facade composes existing authorization,
compatibility, Catalog, Graph Catalog, federated retrieval, and chat services. It
does not own another knowledge store, write API, frontend, conversation history,
or usage-control persistence.

### 2. Signatures

Environment:

```text
PUBLIC_API_V1_ENABLED=false
```

Enabling requires Organization authorization, cross-Library compatibility,
personal Library scopes, federated retrieval, Knowledge Catalog, and Graph
Catalog.

HTTP routes:

```text
GET  /api/v1/libraries
POST /api/v1/scopes/validate
GET  /api/v1/libraries/{slug}/documents/{document_id}
GET  /api/v1/libraries/{slug}/entities/{entity_id}
GET  /api/v1/libraries/{slug}/relations/{relation_id}
GET  /api/v1/libraries/{slug}/evidence/{evidence_id}
POST /api/v1/entities/search
POST /api/v1/relations/search
POST /api/v1/retrieval
POST /api/v1/answers
POST /api/v1/answers/stream
```

Core service boundaries:

```python
resolve_public_scope(db, user, selection, channels) -> ResolvedPublicScope
resolve_public_graph_scope(db, user, selection) -> ResolvedPublicScope
prepare_public_retrieval(db, user, body) -> PreparedPublicRetrieval
recheck_public_scope(db, user, prepared) -> None
build_public_retrieval_response(request_id, prepared) -> PublicRetrievalResponse
build_public_answer_response(request_id, answer, prepared, used_records) -> PublicAnswerResponse
```

### 3. Contracts

- The router uses `PublicV1Route`. A server-generated 32-lowercase-hex request
  ID is present in every success/error body or SSE event and in
  `X-Request-Id`. An unlisted public path or wrong HTTP method reaches a hidden
  catch-all and returns the same `not_found` envelope.
- The default-off dependency executes before `current_active_user`, database,
  or customer-data authorization. JWT Cookie and Bearer API Key use the same
  existing active-user dependency; the API Key retains current Organization
  binding, membership, expiry, revocation, and last-used behavior.
- Aggregate bodies contain exactly one `scope.library_slugs` or
  `scope.scope_id`. Selection is ordered, unique, bounded to `1..20`, and
  all-or-nothing. A saved scope is an owned preference and never grants access.
- Text operations use the compatibility service. Graph searches use the Graph
  Catalog scope resolver so its authoritative semantics remain intact: one
  Library may expose staged facts without graph compatibility; two or more
  Libraries require exact graph compatibility. Do not pre-resolve a graph
  scope through saved-scope text restore.
- Document/Evidence reads call Knowledge Catalog services. Entity/relation
  reads and searches call Graph Catalog services. Public handlers do not query
  customer knowledge tables or proxy legacy routes over HTTP.
- Retrieval runs existing compatible federation once, keeps Library order and
  Library-scoped identity, returns at most 50 matched source/chunk rows, and
  never invokes the answer provider. Source/chunk ranks are contiguous and
  their Library/Document/Revision/Chunk identities match.
- Graph context examines at most five current retrieved documents and returns
  at most 50 entities plus 50 relations. Facts are keyed by `(library_id,
  fact_id)` and retain Publication, Ontology, Revision, and Evidence identity.
- Synchronous and streaming answers recheck exact current text scope before
  model execution. SSE performs a pre-response check and another check at the
  actual provider boundary. Empty retrieval returns the fixed no-evidence
  answer without provider use.
- SSE event order is `meta`, bounded `delta` (`1..2048` characters), then one
  terminal `result`; failure emits one content-free `error` and no `result`.
  Provider iterators are closed in `finally`, including client cancellation.
- M6 persists no question, answer, partial output, usage row, rate window, or
  lease. Responses exclude object locators, credentials, prompts, raw provider
  responses, arbitrary metadata, and raw exceptions.

### 4. Validation & Error Matrix

| Condition | Stable result |
|---|---|
| Public v1 disabled, unknown path, or wrong method | `404 not_found` |
| Missing/invalid JWT Cookie or Bearer API Key | `401 authentication_required` |
| Missing, unauthorized, removed, cross-Organization, or API-Key-out-of-scope member | `403 scope_forbidden` |
| Authorized text/graph selection is incompatible | `409 scope_incompatible` with bounded allowlisted details |
| Strict body/path validation fails | `422 request_invalid` |
| Authorized resource is absent or no longer current/published | `404 resource_not_found` |
| Catalog/graph invariant or required runtime is unavailable | `503 service_unavailable` |
| Answer provider is not configured | `503 answer_unavailable` |
| Retrieval/model upstream fails | `502 upstream_failed` |
| Unexpected public boundary failure | `500 internal_error` without raw detail |
| Stream fails after response start | one content-free `error` SSE event |

Authentication, authorization, resource-hidden, upstream, and internal errors
must not contain query, answer, Chunk/Evidence text, secret, title, storage
locator, provider response, or exception text.

### 5. Good/Base/Bad Cases

- Good: a user selects two readable, text-compatible Libraries; retrieval keeps
  both Library identities and answer sources map exactly to the records admitted
  to model context.
- Good: a single Library without a current graph Publication remains searchable
  through Graph Catalog as staged knowledge.
- Base: with `PUBLIC_API_V1_ENABLED=false`, the exact OpenAPI paths remain
  published while all requests fail before authentication/data access.
- Bad: dropping an unavailable saved-scope member, merging same-named facts
  across Libraries, or using a stored scope as authority is forbidden.
- Bad: calling text compatibility before single-Library graph search changes
  Graph Catalog semantics and is forbidden.
- Bad: allowing an exception to escape after SSE starts, or failing to close the
  provider iterator on cancellation, is forbidden.

### 6. Tests Required

- Contract tests assert strict extra-field rejection, exact scope XOR, bounds,
  source/chunk identity, request IDs, fixed errors, OpenAPI examples, and the
  exact 11-route inventory.
- Service tests assert explicit/saved scope ownership, Organization/API-Key
  fencing, all-or-nothing incompatibility, single-Library Graph Catalog
  allowance, unchanged search filters/cursors, Catalog delegation, retrieval
  projection, no-provider retrieval, and pre-model scope recheck.
- API tests assert default-off dependency ordering, validation privacy, unknown
  method/path handling, stable headers/envelopes, sync result, bounded SSE,
  terminal result, provider failure, scope revocation, cancellation cleanup,
  and no provider use for empty retrieval.
- PostgreSQL coverage asserts saved-scope owner isolation and real JWT/API-Key
  principal parity when `VECTOR_KB_PG_TEST_DSN` points to a disposable database.
- Run inherited Organization authorization, compatibility, personal scope,
  federation, Knowledge Catalog, Graph Catalog, retrieval/chat suites, full
  non-frozen pytest, Ruff, compileall/import/OpenAPI smoke, release safety, and
  `git diff --check`.

### 7. Wrong vs Correct

Wrong:

```python
libraries = await load_saved_scope(scope_id)
hits = await retrieve(libraries)  # stored preference treated as authority
return {"answer": await model(query, hits), "sources": hits}
```

Correct:

```python
prepared = await prepare_public_retrieval(db, user=user, body=body)
await recheck_public_scope(db, user=user, prepared=prepared)
result = await generate_answer(body.query, public_answer_records(prepared), ...)
return build_public_answer_response(
    request_id,
    result.answer,
    prepared,
    result.used_records,
)
```

The correct boundary re-authorizes current scope, reuses the existing retrieval
and answer engines, and derives returned grounding from the exact records used.
