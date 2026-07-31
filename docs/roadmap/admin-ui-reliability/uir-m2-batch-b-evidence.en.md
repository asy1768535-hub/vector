# UIR-M2 Batch B Implementation Evidence

> Project: `vectorDatabase`
>
> Date: 2026-07-29
>
> Scope: read-state reliability for Search, Libraries, and API Keys only; mutation behavior remains frozen.

## Implemented Surfaces

### Search: `Search.js`

- separates the primary search request from Library-option and FAQ reads;
- distinguishes initial loading, successful empty, fatal search failure, and refresh failure;
- retains the last successful result set when a later search fails;
- provides real retries for the search and Library-option reads;
- fences stale Library, FAQ, and search responses;
- does not change result export or document navigation behavior.

### Library Management: `Libraries.js`

- distinguishes initial loading, successful empty, fatal list failure, and refresh failure;
- retains the last successful Library list when refresh fails;
- exposes a durable list retry and a separate FAQ-dialog retry;
- fences stale list and FAQ responses;
- does not change create, edit, delete, rebuild, or FAQ mutation behavior.

### API Keys: `ApiKeys.js`

- distinguishes initial loading, successful empty, fatal list failure, and refresh failure;
- retains the last successful key list when refresh fails;
- provides a durable retry wired to `listApiKeys`;
- fences stale list responses;
- does not change API Key creation or revocation behavior.

## Verification

- focused Batch B and related frontend tests passed: `109/109`;
- full `admin-ui` Node suite passed: `629/629`;
- `git diff --check` reported no whitespace errors;
- PC smoke checks used a `1280x720` viewport;
- Library Management loaded one preview Library, refresh completed, no console error appeared, and page width remained `1280/1280`;
- API Keys loaded the successful empty state, refresh completed, no console error appeared, and page width remained `1280/1280`;
- no mutation control was exercised or changed.

## Browser Follow-Up

The Search failure-state implementation is covered by the shared state projection and Batch B tests, but its final browser error-state check remains pending. Port `5599` had several simultaneous stale Python preview listeners, so the browser repeatedly loaded an older `Search.js` and rendered the pre-migration empty state after an expected preview `HTTP 501`. Re-run this one scenario with a single clean preview listener before closing the UIR-M2 browser gate.

## Deferred

- Batch C: `Chat.js`, `Documents.js`, `Permissions.js`, `Jobs.js`;
- Graph Governance remains assigned to `UIR-M3`;
- mobile layout, mutation verification, and retained API-wrapper cleanup remain out of scope.
