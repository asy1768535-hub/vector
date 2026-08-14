# Remove bundled frontend and preserve functional documentation

## Boundary

The product becomes API-only. The deletion target is the tracked `admin-ui/`
tree plus code and active documentation whose only purpose is serving or
operating that tree. Backend business APIs remain unchanged.

Historical plans and specifications are records, not runtime dependencies.
They remain in the repository. The current frontend spec index and
`docs/12-admin-ui.md` will identify the implementation as retired.

## Runtime Design

`app.main.create_app()` will register only API, health, and WebSocket routes.
The `StaticFiles` import, `NoCacheStaticFiles`, `resolve_console_ui_dir`,
`/console` mount, and root redirect will be removed. No replacement root
handler is added; `/` and `/console/*` use FastAPI's normal 404 response.

`Settings.console_ui_dir` is removed because retaining an unused switch would
imply that externally supplied UI builds remain a supported runtime surface.

## Repository Cleanup

The following active couplings are removed or updated:

- `admin-ui/`: delete the complete tracked tree.
- `tests/test_console_ui_selection.py`: delete frontend-selection tests.
- `scripts/check_release_safety.py`: remove admin-UI CDN/source scanning while
  retaining backend release checks.
- `scripts/start_local.ps1` and `scripts/status_local.ps1`: report API/health
  endpoints without a console URL.
- `.claude/launch.json`: remove the frontend HTTP server launch entry.
- `.gitignore`: remove admin-ui build-cache entries.
- README and active docs: replace console workflows with API equivalents or
  state that the retired behavior is documentation-only.

CI currently has no explicit Node/frontend job, so no workflow deletion is
expected unless a final search finds an indirect reference.

## Functional Documentation

`docs/12-admin-ui.md` becomes an implementation-neutral rebuild contract:

1. retired status and API-only product boundary;
2. role and permission model;
3. navigation and shared session behavior;
4. a route inventory for login plus all 18 authenticated pages;
5. per-page purpose, audience, functions, important states, and API families;
6. overlap notes that distinguish documents/catalog, search/retrieval
   diagnostics, and graph/schema responsibilities;
7. minimum acceptance criteria for any future frontend.

The document describes capabilities, not Vue file structure or styling.
Historical detailed designs remain available under `docs/design/`,
`docs/superpowers/`, and `.trellis/spec/frontend/`.

## Compatibility And Rollback

This intentionally removes browser compatibility and changes `/` and
`/console/*` from redirect/static responses to 404. API clients are unaffected.

Rollback is a normal Git revert or branch reset to `3849df8`. The broader local
pre-change state is additionally recoverable from remote commit `d636984`.

## Risks

- Stale operational docs could send operators to a nonexistent console.
  Mitigation: search all active docs and update every operational reference.
- Removing root routing could break tests or probes that incorrectly use `/`.
  Mitigation: add explicit API-only routing tests and document `/health`.
- Frontend code may encode behavior absent from old docs.
  Mitigation: derive the functional specification from v0.9 routes, views,
  API calls, and permission helpers before deleting the tree.
- Broad source deletion can hide accidental backend changes.
  Mitigation: inspect the final diff by path and run the backend CI boundary.
