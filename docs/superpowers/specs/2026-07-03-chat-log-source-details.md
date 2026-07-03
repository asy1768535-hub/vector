# Chat Log Source Details Redesign

## Goal

Make the chat log expanded row easier to scan by replacing long inline citation excerpts with compact source summary rows and a detail dialog for the full source text.

## Current Problem

The expanded row currently renders every `row.sources` item with title, document ID, score, and full `s.content`. Long source excerpts make the row very tall and push important log context out of view.

## Scope

- Update only `admin-ui/src/views/ChatLogs.js` rendering and local state needed for source details.
- Update only related `.logs-source-*` styles in `admin-ui/style.css`.
- Update existing chat log regression tests.
- Do not change API calls, filters, table columns, pagination, CSV export, or backend code.

## Design

The expanded row will show compact source summary items instead of full content:

- File type icon when available.
- Source title, truncated in the row.
- Score, using the existing `fmtSourceScore` helper and visual tone classes for high, medium, and low similarity.
- No document ID, page, or location metadata in the compact row.
- `查看详情` action for each source, styled as a small button rather than a link.

The source summary list should use available horizontal space. On desktop it should render as a two-column compact grid so five citations fit in roughly three rows instead of a tall single-column stack. On narrow screens it can collapse to one column.

Clicking `查看详情` opens a dialog with:

- Source title.
- Document ID when available.
- Page or location metadata when available.
- Score.
- Full source excerpt from `s.content`.

The implementation must not invent metadata. If a page/location field is absent, the dialog simply omits that line.

## Data Handling

Use only fields already present on source objects. Candidate metadata keys may include `page`, `page_number`, `loc`, `location`, and `document_id`. These are displayed only when present.

## Testing

Update `admin-ui/chat_logs_redesign.test.mjs` to cover:

- Expanded row keeps source list rendering.
- Source cards no longer render `{{ s.content }}` inline.
- Source cards no longer render document ID or page/location metadata inline.
- Each source has a `查看详情` action.
- Source detail actions are button-style, not link-style.
- Source score has tone classes for color grading.
- A source detail dialog exists.
- Dialog renders title, document ID, score, optional page/location metadata, and full content.
- No inline style attributes are introduced.

Run:
- `cd admin-ui && node chat_logs_redesign.test.mjs`
- `cd admin-ui && node --test`
