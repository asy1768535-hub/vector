# Permission Matrix Tools Redesign

## Goal

Reduce visual noise in the permission matrix tools area by replacing the colorful read/write/delete legend with a quiet management hint in the user configuration box and moving the knowledge-base search control to the front of the matrix tools row.

## Current Problem

The current legend uses blue, green, and red checked boxes for `读取`, `写入`, and `删除`. These colors are visually heavier than the surrounding toolbar and make the row look like status or warning content. The table already has clear column headers for read, write, and delete permissions, so the legend repeats information instead of helping the administrator complete the task.

## Scope

- Update only the tools area in `admin-ui/src/views/Permissions.js`.
- Update only related `permissions-matrix-tools`, `permissions-action-legend`, `permissions-search-wrap`, and supporting styles in `admin-ui/style.css`.
- Preserve existing permission data, save/reset behavior, filtering behavior, and table columns.
- Do not change API calls or permission semantics.

## Design

Replace the color legend with one subdued hint in the top user configuration box:

`可为该用户配置各知识库的读取、写入、删除权限`

Layout:
- The hint sits in the same top configuration box as the selected user, role, status, save hint, unsaved changes, and save/reset actions.
- The matrix tools row starts with the `搜索知识库名称` input.
- If filtered results are active, the match count remains near the search input.
- On narrow screens, the user configuration box and matrix search row continue stacking naturally with the existing responsive rules.

Visual treatment:
- Use neutral text color and a small inline icon or bullet only if existing patterns support it.
- Remove the colored legend boxes and checkmarks.
- Keep the hint lower priority than the primary `保存权限` button.
- Keep the matrix tools row focused on filtering, not explanatory text.

## Testing

Update `admin-ui/permissions_redesign.test.mjs` to cover:
- The old `permissions-legend-box` markup is removed.
- The new management hint text is present.
- The search input and filtered match count remain present.
- The matrix tools row no longer contains the management hint.
- No inline style attributes are introduced.

Run:
- `cd admin-ui && node permissions_redesign.test.mjs`
- `cd admin-ui && node --test`
