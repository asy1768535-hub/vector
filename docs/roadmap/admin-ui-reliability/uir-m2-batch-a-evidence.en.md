# UIR-M2 Batch A Implementation Evidence

> Project: `vectorDatabase`
>
> Date: 2026-07-29
>
> Scope: read-state reliability pilots only; mutation behavior remains frozen.

## Implemented Surfaces

### Simple List Pilot: `Users.js`

- distinguishes initial loading, successful empty, fatal failure, refresh, and refresh failure;
- keeps the last successful user list when refresh fails;
- provides a durable fatal error with a real `listUsers` retry;
- prevents stale responses from replacing the latest request;
- does not change create, edit, reset-password, role, status, or disable behavior.

### Multi-Request Pilot: `Dashboard.js`

- tracks operations, Libraries, audits, and health reads independently;
- treats operations, Libraries, and audits as primary reads;
- renders a fatal state only when all primary reads fail before any primary data resolves;
- preserves successful cards and labels unresolved metrics with `—`;
- retries only failed reads from partial and fatal states;
- keeps health failure auxiliary to valid primary data;
- prevents stale responses and duplicate UI retries.

## Shared Contract

`admin-ui/src/read_state_ui.js` provides:

- `readProjection` for explicit read-state projection;
- `createRequestFence` for latest-request ownership.

## Verification

- focused read-state, Users, Dashboard, and frontend audit tests passed;
- full `admin-ui` suite passed: `621/621`;
- PC browser verification passed at `1280x720`;
- Users loaded one preview row, refresh preserved the row, and no fatal state appeared;
- Dashboard rendered five stat cards and four content cards; refresh completed without console errors;
- neither pilot produced horizontal page overflow;
- no mutation control was exercised or changed.

## Deferred

- Batch B: `Search.js`, `Libraries.js`, `ApiKeys.js`;
- Batch C: `Chat.js`, `Documents.js`, `Permissions.js`, `Jobs.js`;
- Graph Governance remains assigned to `UIR-M3`;
- mobile layout and retained API-wrapper cleanup remain out of scope.
