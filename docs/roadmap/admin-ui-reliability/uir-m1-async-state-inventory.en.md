# UIR-M1 Admin UI Async-State Inventory

> Project: `vectorDatabase`
>
> Date: 2026-07-29
>
> Authority: `docs/roadmap/vector_knowledgebase_roadmap_v3.zh-CN.md`, Section 13
>
> Purpose: implementation input for `UIR-M2`; this file records current behavior and does not authorize mutation changes.

## 1. Audit Method

The inventory covers every leaf route declared in `admin-ui/src/app.js`, plus the guest login route.

Evidence used:

- route-to-view mapping in `admin-ui/src/app.js`;
- state, request, catch, template, retry, and refresh paths in each view;
- API wrapper calls in `admin-ui/src/api.js`;
- the existing Node test baseline;
- the UIR-M1 PC browser check.

Legend:

| Value | Meaning |
|---|---|
| Yes | The state has a persistent and distinguishable UI projection |
| Partial | Some subrequests or paths handle it, but the route is not consistent |
| No | The route lacks a durable projection or incorrectly falls through to another state |
| N/A | The state does not apply to the current read surface |

An `ElMessage` toast alone does not count as a fatal or partial-error state because it disappears and the remaining page can look successfully empty.

## 2. Route Inventory

| Route | View | Loading | Success empty | Fatal error | Partial error | Read retry/refresh | Stale-response fence | Current assessment |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `#/login` | `Login.js` | Yes | N/A | Partial | N/A | No | N/A | Submit loading exists; authentication failure is toast-only. Keep outside UIR-M2 read migration. |
| `#/knowledge-use/chat` | `Chat.js` | Partial | Yes | Partial | Partial | Partial | Partial | Conversation and graph subflows have state, but initial Library/context failures are not consistently durable. |
| `#/knowledge-use/search` | `Search.js` | Yes | Yes | No | No | No | No | Library-load and search failures use toasts; failed search can resemble a successful empty result. |
| `#/knowledge-use/retrieval-test` | `RetrievalTest.js` | Yes | Yes | Yes | Yes | Partial | Yes | Compatibility and retrieval states are separated. Retest acts as retry, but retry semantics are not presented consistently. |
| `#/knowledge-assets/documents` | `Documents.js` | Yes | Yes | Partial | Partial | Yes | No | Multiple reads and mutations expose loading, but several failures rely on toasts and request scope is not uniformly fenced. |
| `#/knowledge-assets/catalog` | `KnowledgeCatalog.js` | Yes | Yes | Yes | Yes | Yes | Yes | Best current reference for explicit list/detail/processing/evidence states. Some auxiliary file-open errors remain toast-only. |
| `#/knowledge-assets/import` | `Import.js` | Yes | Yes | Partial | Yes | Yes | Yes | Upload, batch, and extraction paths have extensive state; initial supporting-data failures still need one consistent projection. |
| `#/knowledge-governance/graph` | `GraphGovernance.js` | Yes | Partial | Partial | Partial | Partial | Yes | State objects exist, but disabled, empty, fatal, and auxiliary failure can project contradictory UI. Assigned to UIR-M3. |
| `#/knowledge-governance/schema` | `SchemaLifecycle.js` | Yes | Yes | Yes | Yes | Yes | Yes | Strong state separation across catalog, draft, validation, and activation reads; verify consistency during later decomposition. |
| `#/knowledge-governance/classification` | `ClassificationReview.js` | Yes | Yes | Yes | Partial | Partial | Yes | Main list behavior is explicit; auxiliary taxonomy/option failures and retry presentation need review. |
| `#/users-permissions/users` | `Users.js` | Yes | Yes | No | No | Refresh only | No | Initial list failure is toast-only and can leave an empty table. Recommended simple-list pilot for UIR-M2. |
| `#/users-permissions/permissions` | `Permissions.js` | Yes | Yes | Partial | Partial | Partial | No | Selected-user permission errors are visible, but bootstrap data failures and retry ownership are inconsistent. |
| `#/libraries` | `Libraries.js` | Yes | Yes | No | Partial | Refresh only | No | Main list failure is toast-only and can be mistaken for no Libraries; FAQ auxiliary failure is also toast-only. |
| `#/operations-center/overview` | `Dashboard.js` | Yes | Yes | No | Yes | Yes | No | Individual request failures are listed, but an all-primary-failed outcome has no distinct fatal state. Recommended multi-request pilot for UIR-M2. |
| `#/operations-center/status` | `RuntimeStatus.js` | Yes | Yes | Yes | Yes | Yes | No | Preserves data on refresh and has fatal reload UI. Add stale-response fencing when migrated. |
| `#/operations-center/jobs` | `Jobs.js` | Yes | Yes | Yes | Partial | Yes | No | Main list failure and reload exist; auxiliary action/read failures are not uniformly projected. |
| `#/audit-center/operations` | `Audit.js` | Yes | Yes | Yes | N/A | Yes | No | Main read has loading, empty, fatal error, and reload. Add stale-response fencing when filters become concurrent. |
| `#/audit-center/chat` | `ChatLogs.js` | Yes | Yes | Yes | Yes | Yes | No | Main log failure and Library-list partial failure are distinct. Add stale-response fencing for repeated queries. |
| `#/account/profile` | `AccountProfile.js` | N/A | N/A | N/A | N/A | N/A | N/A | Read data is store-backed. Profile/password mutations remain deferred until user page confirmation. |
| `#/account/api-keys` | `ApiKeys.js` | Yes | Yes | No | No | Refresh only | No | List failure is toast-only and can display the normal empty table. |

## 3. Cross-Route Findings

### 3.1 Error-To-Empty Ambiguity

The recurring defect is:

```text
request starts
  -> array remains []
  -> request fails
  -> transient toast
  -> table empty slot renders "no data"
```

This is present or possible on Search, Users, Libraries, API Keys, and supporting reads on other pages.

### 3.2 Refresh Semantics

Several pages expose a refresh button, but do not distinguish:

- initial load from refresh;
- retained stale data from fresh data;
- refresh failure from initial fatal failure.

UIR-M2 must preserve valid data during refresh and display a bounded refresh warning instead of replacing the page with empty state.

### 3.3 Multi-Request Ownership

Dashboard, Chat, Documents, Import, Graph Governance, Schema Lifecycle, Classification Review, and Chat Logs load more than one resource. Each needs an explicit primary/auxiliary classification.

An auxiliary failure must not:

- hide valid primary content;
- mark the whole feature disabled;
- clear unrelated data;
- reuse the primary empty state.

### 3.4 Stale Responses

The strongest existing fencing is in Knowledge Catalog, Graph Governance, Schema Lifecycle, Import, Retrieval Test, and Classification Review.

Simple list pages generally have no request sequence or scope identity. UIR-M2 must add fencing when refreshes, filters, route changes, or Library changes can overlap.

## 4. UIR-M2 Recommended Batches

### Batch A: Establish The Pattern

Simple list pilot:

```text
Users.js
```

Scope:

- durable initial fatal error;
- successful empty state only after a successful response;
- refresh state that preserves existing rows;
- real read retry;
- duplicate-request and stale-response fence.

Do not change create, update, reset-password, disable, or delete behavior.

Multi-request pilot:

```text
Dashboard.js
```

Scope:

- classify primary and auxiliary requests;
- preserve valid cards when one request fails;
- distinguish partial failure from all-primary fatal failure;
- retry only the failed or required reads;
- prevent older refresh results from replacing newer state.

### Batch B: Apply To Toast-Only Lists

```text
Search.js
Libraries.js
ApiKeys.js
```

Keep all mutation behavior frozen.

### Batch C: Resolve Mixed Read Surfaces

```text
Chat.js
Documents.js
Permissions.js
Jobs.js
```

Split initial, primary, auxiliary, and refresh errors before any component decomposition.

### Batch D: Verify Strong Existing Surfaces

```text
RetrievalTest.js
KnowledgeCatalog.js
Import.js
SchemaLifecycle.js
ClassificationReview.js
RuntimeStatus.js
Audit.js
ChatLogs.js
```

Do not rewrite these merely for uniform naming. Add only missing behavior proven by tests.

Graph Governance is excluded from UIR-M2 implementation and remains owned by `UIR-M3`.

## 5. UIR-M2 Entry Gate

Before editing a selected route:

1. run GitNexus context and upstream impact for every changed function or method;
2. identify the primary read and every auxiliary read;
3. add failing behavior tests for the exact missing states;
4. verify FastAPI response and error contracts;
5. leave all unconfirmed mutation behavior unchanged;
6. run relevant Node tests and PC browser scenarios;
7. run `gitnexus_detect_changes()` before commit.
