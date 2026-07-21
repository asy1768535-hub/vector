# Library Detail Drawer Redesign

## Goal

Redesign the `admin-ui` library detail drawer so it reads like a polished detail view instead of a plain field list. The change is limited to the knowledge base detail drawer opened from `admin-ui/src/views/Libraries.js`.

## Current Problem

The current drawer uses a two-column definition list with weak hierarchy and large empty vertical space. Key information such as the library name, status, retrieval mode, embedding model, Qdrant collection, and full-text source are all presented at the same visual weight, making the page harder to scan.

## Scope

- Update only the library detail drawer template in `admin-ui/src/views/Libraries.js`.
- Update only related `libraries-detail-*` styles in `admin-ui/style.css`.
- Preserve all existing actions and data sources.
- Do not change API calls, route behavior, edit dialog behavior, FAQ dialog behavior, or backend code.

## Design

The drawer will use an information-card layout.

The top section is an identity header:
- Show library name as the primary title.
- Show unique ID below the title in monospace styling.
- Show the current status tag near the title.
- Show compact summary pills for retrieval mode, vector distance, and chunk size/overlap.

The body is grouped into three white cards on a subtle page background:
- Basic information: description, unique ID, status.
- Vector and retrieval: embedding display, vector distance, retrieval mode, chunk size/overlap.
- Storage and source: Qdrant collection, full-text source, embedding endpoint.

Long values such as endpoint URL, Qdrant collection, and full-text source must wrap cleanly and use monospace styling where useful.

The footer actions remain at the bottom of the drawer:
- `编辑配置` remains available unless the library is deleted.
- `管理常用问题` remains available unless the library is deleted.
- Layout should make the primary next action clear without adding new actions.

## Visual Constraints

- Drawer width should be slightly wider than today, around `480px`, while remaining responsive with the existing mobile rule.
- Avoid nested cards beyond the three direct information blocks.
- Use existing enterprise theme tokens: `--app-text`, `--app-text-secondary`, `--app-border`, `--app-border-light`, `--app-primary`, and `--app-page-bg`.
- Keep typography compact and appropriate for an admin tool.
- No inline `style` attributes.

## Testing

Update `admin-ui/libraries_redesign.test.mjs` to cover:
- The drawer uses the new header, summary, section, and field class names.
- Existing detail fields remain present: description, embedding endpoint, vector distance, Qdrant collection, and full-text source.
- Existing actions still call `openEdit(selectedLibrary)` and `openFaq(selectedLibrary)`.
- The template continues to have no inline style attributes.

Run:
- `cd admin-ui && node libraries_redesign.test.mjs`
- `cd admin-ui && node --test`
