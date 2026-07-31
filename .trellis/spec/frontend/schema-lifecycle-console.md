# Schema Lifecycle Console Contracts

## Scenario: Permission-Aware Schema Administration

### 1. Scope / Trigger

Use this contract when changing `/knowledge-governance/schema` (legacy
`/schema-lifecycle`), its menu access, strict API
clients, version routing, draft forms, validation, impact, clone, activation,
draft deletion, or active-version disable.
This is a management workspace; Documents and Knowledge Catalog remain primary
product entry points.

### 2. Signatures

Route and permission:

```text
/knowledge-governance/schema?library=<slug>&version=<uuid>&tab=<known-tab>
access: Organization administrator or exact Library admin permission
```

Known tabs:

```text
overview | entities | relations | attributes | constraints | impact
```

The view consumes only the allowlisted Schema lifecycle clients in `api.js`.
Pure permission, route, identity, status, issue, impact, and error projections
live in `schema_lifecycle_ui.js`.

### 3. Contracts

- Library choices come only from effective manageable Libraries. Hide the menu
  and reject the direct route when none exist; never treat platform superuser
  as customer Library authority.
- Canonical routing retains only a manageable Library, a version returned for
  that Library, and a known tab. Library/version changes invalidate detail,
  validation, impact, mutation, and dialog sequences before route replacement.
- Version list, detail, validation, impact, and mutation flows use independent
  monotonic request tokens. Responses must repeat exact Library/version
  identity, child scope, count, and state hash before display.
- Clone refreshes the version list before replacing the URL with the returned
  draft ID. This prevents route normalization from selecting the old version
  while the new draft is not yet present in the client list.
- Entity type, relation type, attribute, and constraint dialogs submit only
  type-specific allowlisted fields plus the current state hash and one
  idempotency key per intent. There is no automatic `409` replay.
- Validation invoked from any tab navigates to `overview` after completion so
  success, fixed error, or actionable issues are visible immediately.
- Impact is draft-only and renders Schema diff, reference counts, compatibility
  status, and the explicit historical-data statement. It never changes
  retrieval scope.
- Activation requires a confirmation naming the exact version and stating that
  historical graph knowledge and Publications remain on their old versions.
- Draft versions expose a delete command with exact-version confirmation. After
  success the hidden version is removed from the list and the next version is
  selected. Active versions expose a disable command that states historical
  graph knowledge and Publications remain bound to that version.
- Do not render arbitrary metadata, raw errors, Schema provider payloads,
  prompts, source content, credentials, storage locators, or `v-html`.
- At `<=899px` the workspace stacks. At `<=520px` controls wrap, tables scroll
  only inside their shells, dialogs stay inside the viewport, and the page has
  no horizontal overflow.

### 4. Validation & Error Matrix

| Condition | Console behavior |
|---|---|
| No manageable Library | Hide menu and reject route |
| Runtime disabled / `404` | Fixed unavailable state; no alternate API probe |
| `403` | Fixed scoped forbidden state |
| `409` | Close unsafe UI, clear previews, reload server truth, never resubmit |
| `422` | Fixed invalid-command state |
| Malformed or mismatched response identity | Discard payload and show fixed malformed state |
| Active version selected | Inspection plus clone and disable; no draft edit controls |
| Disabled version selected | Inspection only; no mutation controls |
| Request completes after scope change | Ignore response without changing current UI |

### 5. Good / Base / Bad Cases

- Good: an administrator deep-links a draft, edits all four item kinds, runs
  validation from the constraints tab and lands on a visible result, previews
  impact, confirms activation, and sees the old version disabled.
- Good: after clone, the URL and detail both point to the returned draft even
  when the prior active version was selected.
- Good: deleting a draft selects the next visible version; disabling an active
  version leaves it visible with the disabled status.
- Base: an active version is readable and offers clone; impact explains that a
  draft is required.
- Bad: changing the URL before refreshing versions, accepting children from
  another version, silently replaying a stale command, or showing a successful
  validation only on a hidden tab is forbidden.

### 6. Tests Required

- Pure tests cover manageable Library projection, route normalization, fixed
  labels/errors, response identity, child/count validation, and impact summary.
- API tests assert exact route families, methods, and per-item request
  allowlists; caller extras must be discarded.
- View tests assert every tab and command, independent request sequences,
  clone-list refresh ordering, validation result navigation, explicit
  activation confirmation, privacy exclusions, and imported helper boundaries.
- Browser QA covers clone, four draft forms, validation, impact, activation,
  desktop and 390px layouts, internal table scrolling, dialog bounds, page
  overflow, and console errors.
- Run every `admin-ui/*.test.mjs` test before commit.

### 7. Wrong vs Correct

Wrong:

```javascript
scope.versionId = response.version.id;
await canonicalRoute();
await loadVersions();
```

Correct:

```javascript
detail.data = response.version;
scope.versionId = String(response.version.id);
await loadVersions({ chooseVersion: false });
await canonicalRoute();
```

The correct order makes route normalization aware of the new draft and prevents
an older list response from replacing the selected version.
