# Remove bundled frontend and preserve functional documentation

## Goal

Turn the v0.9 repository into an API-only service by removing the bundled
administration frontend and every active runtime/build/test dependency on it,
while preserving a complete, implementation-neutral functional specification
for a future frontend rebuild.

## Confirmed Facts

- The accepted v0.9 baseline is `origin/main` at `3849df8`.
- The current local work was backed up before this task on remote branch
  `codex/backup-before-frontend-removal-20260724`, commit `d636984`.
- The implementation branch is `codex/remove-bundled-frontend`, based directly
  on `origin/main`.
- The bundled frontend is the no-build Vue application under `admin-ui/`.
- FastAPI currently mounts that directory at `/console` and redirects `/` to
  `/console/`.
- v0.9 exposes 19 frontend routes: login, chat, documents, catalog, knowledge
  graph, schema lifecycle, classification review, search, import, API keys,
  retrieval diagnostics, dashboard, users, libraries, permissions, jobs,
  runtime status, audit, and chat logs. Login plus the 18 authenticated routes
  must remain documented.
- Backend APIs and business capabilities are independent of the bundled UI and
  remain in scope as supported product surfaces.

## Requirements

- Delete all tracked files under `admin-ui/`, including source, static assets,
  vendored browser dependencies, and frontend-only tests.
- Remove the FastAPI static-file mount, no-cache static-file wrapper, console
  directory resolver, `/console` route, and root redirect.
- Remove `console_ui_dir` / `CONSOLE_UI_DIR` as a supported setting.
- Remove frontend-only tests, release checks, local-launch entries, ignore
  rules, and status output that assume a bundled console exists.
- Update current user, architecture, deployment, API, operations, and
  troubleshooting documentation so it does not direct users to a removed UI.
- Rewrite `docs/12-admin-ui.md` as the authoritative retired-console functional
  specification. It must document each page's purpose, users/permissions,
  primary functions, important states, and backend API capability mapping.
- Preserve historical design documents, implementation plans, and Trellis
  frontend specifications as historical/reference documentation. Mark active
  frontend contracts as retired where needed; do not delete documentation that
  captures product behavior.
- Preserve every backend route, schema, service, worker, migration, and API
  authorization rule.
- Treat removal of `/` redirect and `/console/*` as an intentional breaking
  change: those paths return normal API 404 responses after this change.
- Do not modify the backup branch or the user's original local scratch files.

## Acceptance Criteria

- [ ] `git ls-files admin-ui` returns no files.
- [ ] Repository-wide active-code search finds no runtime/config/test/CI
      dependency on `admin-ui`, `/console`, `CONSOLE_UI_DIR`,
      `console_ui_dir`, or `resolve_console_ui_dir`.
- [ ] Creating the FastAPI application succeeds without a frontend directory,
      `GET /` returns 404, and `GET /console/` returns 404.
- [ ] The backend lint and CI-equivalent pytest boundary pass.
- [ ] Release-safety checks pass after removing frontend-only assertions.
- [ ] `docs/12-admin-ui.md` documents all 19 retired routes and clearly states
      that no frontend is shipped or served.
- [ ] Active README, quickstart, architecture, project-structure, API,
      deployment, worker, observability, backup/restore, usage, and FAQ
      documentation no longer claims the console is available.
- [ ] Historical planning/specification documents remain available as
      reference and are not rewritten as current operational instructions.
- [ ] `git diff --check` passes on the implementation branch.

## Out Of Scope

- Removing or consolidating backend APIs that appeared redundant in the UI.
- Designing or implementing a replacement frontend.
- Changing authentication, authorization, Organization, Library, graph,
  retrieval, ingestion, observability, or audit behavior.
- Rewriting historical release plans solely to remove old frontend references.
