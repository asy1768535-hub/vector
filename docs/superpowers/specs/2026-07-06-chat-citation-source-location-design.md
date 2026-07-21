# Chat Citation Source Location Design

Date: 2026-07-06

## Goal

Refactor intelligent chat source references so users can inspect both the recalled chunk and the exact normalized-text source location without leaving the chat page.

The existing source-list button text remains `文档详情`, but its behavior changes from opening `/documents` in a new window to opening an in-page source-location modal.

## Current State

- `admin-ui/src/views/Chat.js` renders assistant answers with `marked.parse()` followed by `DOMPurify.sanitize()` in the same file.
- The current DOMPurify configuration uses explicit allow-lists and must not be weakened.
- `openDocDetail()` currently resolves `/documents?slug=...&open=...` and opens a new browser window.
- `ChatSource` currently contains only `title`, `document_id`, `chunk_id`, `score`, and `content`.
- `Chunk.chunk_metadata` is JSONB and currently stores only lightweight document metadata such as title and external id.
- Existing splitters return strings, not offsets.
- Current import/extract paths do not persist the original file and do not persist precise page, paragraph, sheet, row, or character-position metadata.
- PDF extraction currently injects text markers such as `【第 N 页】`, but those markers are not structured, validated location metadata.
- The project uses handwritten Alembic migration files. The latest migration at review time is `0015`.

## Non-Goals

- Do not save original uploaded files.
- Do not reconstruct native Word layout.
- Do not add download, online editing, annotation, or comment features.
- Do not change chat send behavior, streaming behavior, or permission semantics.
- Do not use fuzzy global string search to fake exact positioning.
- Do not navigate away from the intelligent chat page, open the document-management route, or open the knowledge-base sidebar.
- Do not push remote branches or tags as part of this work.

## User Experience

### Clickable Answer Citations

Assistant answers may contain citations such as `[1]` or `[2][3]`.

Valid citations are converted into clickable superscript badges:

- Citation `n` maps to `message.sources[n - 1]`.
- Clicking a badge opens a small recalled-chunk modal.
- The modal shows document title, similarity score, chunk sequence, and the complete recalled chunk content.
- The modal supports copying the chunk text.
- Badges are keyboard accessible with Enter and Space.
- Badges are blue by default and use a pale blue hover background.

Invalid citations are left as plain text:

- Citations inside fenced code, inline code, Markdown links, or URL text.
- Citations whose index is out of range for `message.sources`.

The implementation must not use inline `onclick` attributes. It must attach delegated event handlers or Vue event handlers after sanitized HTML is rendered.

### Document Detail Modal

The existing source-list button text remains `文档详情`.

When clicked, it opens a large modal inside the chat page:

- It does not use `el-drawer`.
- Desktop width is about 900px.
- Small-screen width is `calc(100vw - 24px)`.
- Closing the modal preserves the active conversation and the current chat scroll position.

The modal header shows:

- File name or document title.
- File type.
- Structured location, formatted as page, paragraph, table, Sheet and row range, or line range as applicable.
- Similarity score.
- Chunk sequence.

The modal body shows only a bounded text window around the match, not the full document by default:

- The API returns roughly 3000 characters of context around the matched span.
- The response includes `window_start` and `window_end` so the frontend can translate absolute offsets into window-local offsets.
- The matched range is highlighted with a pale yellow or pale blue background.
- The text is displayed in a simple pre-wrapped text container, not a large editor component.

If the source endpoint fails, the modal falls back to the saved recalled chunk content. If the document is legacy, the modal shows `该文档需重新导入后才能精确定位` and also displays the recalled chunk.

## Frontend Design

Create `admin-ui/src/chat_citations.js`.

Responsibilities:

- Render assistant Markdown through the existing `marked` and `DOMPurify` pipeline without weakening sanitization.
- Convert valid text citations to safe placeholder markup after Markdown token analysis or after DOM traversal.
- Avoid transforming code blocks, inline code, links, and URL text.
- Attach citation metadata through safe attributes such as `data-citation-index` only after sanitization, or include the attribute in a narrow allow-list if required by the chosen implementation.
- Export helpers that can be unit tested in Node without requiring a browser DOM where possible.

`Chat.js` changes:

- Import citation rendering helpers from `chat_citations.js`.
- Add state for the recalled-chunk modal.
- Add state for the document-detail source-location modal.
- Replace `openDocDetail()` behavior with an in-page modal opener while preserving the visible text `文档详情`.
- Add a `getDocumentSource()` API call in `admin-ui/src/api.js`.
- Keep chat send, stream handling, and conversation loading behavior unchanged.

CSS changes go in `admin-ui/style.css`:

- Citation badge styling.
- Small recalled-chunk modal content sizing.
- Large source-location modal sizing and text-window highlighting.
- Responsive rules for 1920px, 1199px, and 899px widths with no horizontal overflow.

## Backend Data Model

Add a separate `DocumentSource` ORM model and migration. It avoids bloating the document list model and keeps large normalized text out of normal document-list queries.

Suggested table: `document_sources`.

Columns:

- `document_id`: primary key or unique foreign key to `documents.id`, cascade delete.
- `revision`: integer, matching the document revision used for location metadata.
- `file_name`: text or varchar.
- `file_type`: varchar.
- `normalized_text`: text, not nullable for non-legacy rows.
- `created_at`: timestamp with timezone.
- `updated_at`: timestamp with timezone.

Update `app/models/__init__.py` so Alembic and tests discover the new model.

Each `Chunk.chunk_metadata` for newly imported or replaced documents includes:

- `source_start`: absolute character offset in `DocumentSource.normalized_text`.
- `source_end`: exclusive absolute character offset.
- `location`: structured JSON metadata.
- `source_revision`: revision of the normalized source text used when the chunk was produced.

Legacy chunks do not receive fabricated metadata. A missing `DocumentSource`, missing offsets, or revision mismatch is treated as legacy or stale and does not attempt fuzzy matching.

## Extraction And Splitting

Introduce a structured chunk representation for ingestion paths, while preserving existing chunk text, embedding, de-duplication, and replacement behavior.

The splitter must directly produce text and exact offsets. It must not compute positions later through unreliable global search.

Suggested internal type:

```python
{
    "text": str,
    "source_start": int,
    "source_end": int,
    "location": dict,
}
```

`ingest_text()` and `reingest_document()` should accept either existing string chunks or structured chunks during transition. When structured chunks are provided, they populate `Chunk.text` and `Chunk.chunk_metadata`.

File-type location capabilities:

- PDF: normalized text is built page by page; location records `page`.
- DOCX: normalized text is built from ordered paragraphs and tables; location records paragraph index or table index.
- XLSX: normalized text is built from sheets and rows; location records Sheet name and row range.
- CSV: keep the existing behavior where each row is imported as its own document; location records the original CSV row number for that row-document.
- TXT, MD, JSON: normalized text records start and end line numbers.

For replacements:

- `Document.current_revision` continues to advance through the existing generation flow.
- `DocumentSource.revision` is updated to the new current revision.
- New chunks get `source_revision` equal to that revision.
- Old chunks are removed from PostgreSQL as today; old Qdrant points remain hidden by existing revision filtering and cleanup behavior.

For de-duplication hits where no new document is created, the existing document source remains unchanged.

## Source Location API

Add a read-only endpoint under the existing library document API:

`GET /libraries/{slug}/documents/{document_id}/source?chunk_id={chunk_id}`

Authorization and validation:

- The user must have `read` permission for the library.
- The document must belong to the library and must not be deleted.
- The chunk must belong to that document.
- `chunk_metadata.source_revision` must match `DocumentSource.revision` and the document's current revision.

Response fields:

- `document_title`
- `file_type`
- `text_window`
- `window_start`
- `window_end`
- `source_start`
- `source_end`
- `location`
- `chunk_id`
- `chunk_seq`
- `legacy`
- `fallback_chunk`

If precise location data is unavailable, return `legacy=true` with enough data for the frontend to display the recalled chunk and the re-import requirement. Do not fabricate offsets.

The endpoint returns a bounded window by default. Use a server-side constant for context size, initially around 3000 characters total around the highlighted range.

## Testing Plan

Backend tests:

- PDF, DOCX, XLSX, CSV, TXT, MD, and JSON imports produce expected structured location metadata.
- For new precise chunks, `normalized_text[source_start:source_end]` exactly equals the corresponding source span used for the chunk.
- Replacing a document updates `DocumentSource.revision` and chunk `source_revision`.
- Cross-library, wrong document, wrong chunk, and no-permission access are denied.
- Legacy documents return `legacy=true` without fabricated offsets.
- Deleting a document cascades deletion of `DocumentSource`.

Frontend tests:

- Single and consecutive citations map to the correct `message.sources[n - 1]`.
- Code blocks, inline code, Markdown links, URLs, and out-of-range citations are not converted.
- Citation badges open the recalled-chunk modal.
- `文档详情` opens the source-location modal in the chat page.
- Highlight range is computed correctly from `window_start`, `source_start`, and `source_end`.
- Closing the modal does not change the active conversation.
- API failure and legacy responses display clear fallback states.
- DOMPurify/XSS tests pass.
- 1920px, 1199px, and 899px layouts have no horizontal overflow.

Verification commands:

```powershell
node --test admin-ui/*.test.mjs admin-ui/src/**/*.test.mjs
node --check admin-ui/src/chat_citations.js
node --check admin-ui/src/views/Chat.js
pytest tests/test_pdf_extract.py tests/test_docx_extract.py tests/test_query_import_api.py tests/test_chat_api.py
python scripts/check_release_safety.py
git diff --check
```

If broader backend changes require it, run additional targeted pytest files around ingestion, migrations, permissions, and document deletion.

## Rollout And Legacy Documents

Existing documents remain searchable and usable. They will not have precise source-location metadata until re-imported or replaced.

For legacy documents:

- Citation chunk viewing still works because chat history already stores recalled chunk content.
- `文档详情` shows the legacy warning and fallback chunk.
- The system explicitly says the document must be re-imported before exact positioning is available.

## Implementation Notes

- Keep changes scoped to chat citation UI, source-location modal, ingestion/source metadata, and the new endpoint.
- Preserve current content hashing and duplicate detection semantics.
- Preserve current chat streaming and saved-history semantics.
- Do not introduce a large text editor dependency.
- Do not push changes to GitHub.
