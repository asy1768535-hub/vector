# Graph Governance Console Contracts

## Scenario: Evidence-Grounded Graph Directory And Governance

### 1. Scope / Trigger

Use this contract when changing `/knowledge-governance/graph` (legacy
`/knowledge-graph`), its Graph Catalog reads,
governance commands, review queues, or Publication controls in `admin-ui/`.
Documents and Knowledge Catalog remain the primary product entry points. The
graph console is a secondary directory, bounded exploration, and governance
workspace, not an unbounded graph canvas and not a second source-file
experience.

### 2. Signatures

Route and authorization:

```text
/knowledge-governance/graph?tab=entities|relations|review|publications|explore
                 &organization=<uuid>&libraries=<slug,slug>
                 &entity=<uuid>|relation=<uuid>

route/menu          any effective Library read
fact submissions    selected single Library insert
review/publication  exact Organization-admin or Library admin
```

Backend projections used by the console:

```text
POST /organizations/{organization_id}/graph-catalog/entities:search
POST /organizations/{organization_id}/graph-catalog/relations:search
GET  /organizations/{organization_id}/graph-catalog/libraries/{slug}/entities/{id}
GET  /organizations/{organization_id}/graph-catalog/libraries/{slug}/relations/{id}
GET  /libraries/{slug}/graph-governance/context
GET  /libraries/{slug}/graph-governance/actions
POST /libraries/{slug}/graph-governance/publications/plan
GET  /libraries/{slug}/v05/graph-publications/
GET  /libraries/{slug}/v05/graph-publications/active
```

Pure frontend boundaries live in `graph_governance_ui.js`, including scope,
permission, response-identity, error, action-summary, and Publication helpers.

### 3. Contracts

- Build Organization and 1..20 Library scope only from `/me/permissions`.
  Platform superuser alone is not customer-content or management authority.
- Entity, Relation, detail, Evidence, context, review, and Publication flows
  own independent monotonic request/mutation sequences. Scope changes discard
  stale responses and close stale drawers/dialogs before a canonical route
  replacement can return early.
- Evidence reuses the Knowledge Catalog endpoint and verifies Library,
  Evidence, Document, Revision, Chunk, fact ID, and fact kind before display.
- Commands use allowlisted API builders, server state hashes, one idempotency
  key per user intent, fixed confirmations, and no automatic `409` replay.
- Review action payloads use action-kind allowlists. Never render arbitrary
  payload/metadata, raw errors, content, prompts, credentials, or storage URLs.
- Publication planning selects approved unbound actions from one Ontology,
  reads the exact current parent from `/active`, and uses the same intent key
  for dry-run and persistent planning. Persistent identity must repeat the
  preview Publication ID, Manifest hash, action-set hash, and parent.
- Activation carries the exact planned Manifest hash. Planned governance or
  rollback Publications may be activated/cancelled; eligible superseded
  Publications may start a dry-run/persistent rollback pair with one intent.
- Every helper called by the view must be explicitly imported. Source-text
  tests must assert the import boundary, not only that a call token exists.
- At `<=899px` columns stack. Existing directory/governance controls may wrap
  at `<=520px`, but the `explore` tab is desktop-focused and must not add a
  dedicated mobile layout. Tables scroll only inside their shell and
  page-level horizontal overflow is forbidden.

### 4. Validation & Error Matrix

| Condition | Console behavior |
|---|---|
| No effective read | Hide menu and reject direct route |
| Review/Publication without management | Normalize to one manageable Library or deny |
| Multi-Library graph incompatibility | Keep every selected Library and show bounded reason codes |
| Runtime disabled / `404` | Fixed unavailable state; do not probe alternate content APIs |
| `403` | Fixed scoped forbidden state |
| `409` | Clear unsafe selection/preview, reload server truth, never resubmit |
| `422` | Fixed invalid-command state |
| Mismatched or malformed identity | Discard payload and show fixed malformed state |
| Current Publication absent | Use `null` parent; do not infer current from recent history |
| Publication runtime disabled | History remains visible; all mutation controls are disabled |
| Scope changes during canonical URL repair | Invalidate requests and drawers before `router.replace` |

### 5. Good / Base / Bad Cases

- Good: a reader searches compatible Libraries, opens an Entity, and follows
  an exact Evidence locator back to the Catalog document.
- Good: a reader selects exact Library-scoped seeds and inspects separate,
  bounded one-hop or two-hop published graph groups.
- Good: an administrator reviews staged changes, previews one exact action set,
  persists it with the same intent, then activates the returned Manifest.
- Base: a Library has no current Publication or no pending actions; directories
  and explicit empty states remain usable.
- Bad: deriving current parent from the first recent history row, retaining an
  Entity drawer after route normalization, or retrying a conflicted command is
  forbidden.
- Bad: making a graph canvas the default, merging same-name Entities across
  Libraries, authoring Schema here, dumping action JSON, or treating
  `is_superuser` as Organization authority is forbidden.

### 6. Tests Required

- Pure tests cover exact scope/permission projection, action allowlists,
  response identity, one-Ontology selection, preview/commit/rollback identity,
  status labels, and fixed errors.
- API tests assert exact paths, query/body allowlists, state/Manifest fences,
  and discarded caller extras.
- View tests assert five tabs, independent sequences, imported helper names,
  canonical-route invalidation order, no `v-html`/logging/storage fields, and
  no per-row detail fan-out.
- Browser QA covers Entity, Relation, Review, Publication, preview, and detail
  drawer states at desktop and 390px content width, including long text,
  internal table scrolling, console errors, overlaps, and page overflow.
- Run all admin-ui tests, focused M1/M2/M3 backend tests, full non-frozen
  backend, Ruff, compileall, import smoke, release safety, Alembic head/offline
  SQL for the task-owned migration range, and `git diff --check`.

### 7. Wrong vs Correct

Wrong:

```javascript
const parentId = publications.history.find((row) => row.status === 'active')?.id;
if (error.status === 409) await api.planGraphGovernancePublication(slug, body);
```

Correct:

```javascript
const current = await api.getActiveGraphPublication(slug, ontologyVersionId);
if (!graphPublicationReadMatches(current, identity)) return discardResponse();

// On 409, invalidate preview/selection and reload. A new human command creates
// a new intent; the old mutation is never replayed automatically.
await loadPublicationWorkspace();
```

The correct flow keeps the server authoritative for current Publication and
governance state while preserving a bounded, evidence-first console.
