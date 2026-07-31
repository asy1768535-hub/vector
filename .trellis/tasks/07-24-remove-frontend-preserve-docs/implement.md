# Implementation Plan

## 1. Capture The Retired Product Surface

- [x] Inventory v0.9 routes, menus, permissions, view actions, states, and API
      calls from `admin-ui/`.
- [x] Rewrite `docs/12-admin-ui.md` as the complete functional specification
      before deleting source.
- [x] Mark `.trellis/spec/frontend/index.md` contracts as retired references.

## 2. Remove The Runtime Surface

- [x] Delete every tracked file under `admin-ui/`.
- [x] Remove static frontend serving and root redirect from `app/main.py`.
- [x] Remove `console_ui_dir` from `app/config.py`.
- [x] Replace console-selection tests with API-only route assertions.

## 3. Remove Operational Coupling

- [x] Remove frontend-only release-safety logic.
- [x] Remove frontend launch configuration, ignore rules, and startup/status
      console output.
- [x] Update current README and operational docs to use API/health endpoints.
- [x] Keep historical plans/specs intact.

## 4. Verify

- [x] Confirm `git ls-files admin-ui` is empty.
- [x] Search active code/config/tests for stale frontend runtime references.
- [x] Run `ruff check app tests scripts`.
- [x] Run targeted app-routing tests.
- [x] Run the CI-equivalent non-frozen pytest suite.
- [x] Run `scripts/check_release_safety.py`.
- [x] Run `git diff --check` and inspect `git diff --stat`.

## Risky Files And Rollback Points

- `app/main.py`: verify API router registration is unchanged outside the
  frontend mount block.
- `app/config.py`: verify only the obsolete console setting is removed.
- `scripts/check_release_safety.py`: preserve all non-frontend safety checks.
- `docs/12-admin-ui.md`: complete this before source deletion.
- Rollback baseline: `origin/main@3849df8`.
- Full local backup: `d636984` on
  `origin/codex/backup-before-frontend-removal-20260724`.
