# Chat Citation Source Location Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let chat users inspect recalled chunks and exact normalized source locations in-page without leaving the intelligent chat page.

**Architecture:** Persist one `document_sources` row per managed document revision, store absolute source offsets and structured location metadata in each new chunk, expose a read-only source-location endpoint under the existing library document API, and update chat rendering to convert valid answer citations into accessible source badges. Existing documents remain legacy until re-imported; no fuzzy positioning or original-file persistence is introduced.

**Tech Stack:** FastAPI, SQLAlchemy async ORM, Alembic handwritten migrations, PostgreSQL JSONB, existing Python extractors/splitter, Vue 3 composition API, Element Plus, marked, DOMPurify, Node built-in test runner, pytest.

## Global Constraints

- Do not save original uploaded files.
- Do not reconstruct native Word layout.
- Do not add download, online editing, annotation, or comment features.
- Do not change chat send behavior, streaming behavior, or permission semantics.
- Do not use fuzzy global string search to fake exact positioning.
- Do not navigate away from the intelligent chat page, open the document-management route, or open the knowledge-base sidebar.
- Do not push remote branches or tags as part of this work.
- Keep the existing source-list button text exactly `文档详情`.
- Keep the current DOMPurify explicit allow-lists and do not weaken sanitization.
- Do not use inline `onclick` attributes.
- The source endpoint is `GET /libraries/{slug}/documents/{document_id}/source?chunk_id={chunk_id}`.
- The source endpoint requires existing library `read` permission and must reject cross-library, wrong-document, wrong-chunk, deleted-document, and no-permission access.
- Missing `DocumentSource`, missing offsets, or revision mismatch is legacy/stale and must not fabricate offsets.
- Source windows are bounded to roughly 3000 characters total around the highlighted range.

---

## File Structure

- Create `app/models/document_source.py`: `DocumentSource` ORM model with one row per document and revision.
- Modify `app/models/__init__.py`: import/export `DocumentSource` for Alembic/test discovery.
- Create `alembic/versions/0016_document_sources.py`: handwritten migration for `document_sources` with cascade delete.
- Modify `app/schemas/documents.py`: add `DocumentSourceLocationResponse` response model.
- Modify `app/api/documents.py`: persist `DocumentSource` during imports/replacements and add the source-location endpoint.
- Modify `app/services/splitter.py`: add structured chunk helpers that preserve text and exact offsets.
- Modify `app/services/ingest.py`: accept string chunks or structured chunks; write offset metadata only when provided.
- Modify `app/services/pdf_extract.py`, `app/services/docx_extract.py`, `app/services/xlsx_extract.py`: add normalized-source builders with location metadata while keeping existing text extraction APIs.
- Modify tests under `tests/`: add focused tests for structured chunks, source persistence, source API legacy/precise behavior, and extractor location metadata.
- Create `admin-ui/src/chat_citations.js`: safe citation rendering and text-window highlighting helpers.
- Modify `admin-ui/src/api.js`: add `getDocumentSource(slug, documentId, chunkId)`.
- Modify `admin-ui/src/views/Chat.js`: wire citation badges, recalled-chunk modal, source-location modal, and in-page `文档详情` behavior.
- Modify `admin-ui/style.css`: badge, modal, highlight, and responsive styles.
- Add/modify `admin-ui/*.test.mjs`: citation parsing/rendering, source modal tokens, DOMPurify/XSS, highlight math, responsive CSS checks.

---

### Task 1: Document Source Model And Migration

**Files:**
- Create: `app/models/document_source.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/0016_document_sources.py`
- Modify: `tests/test_document_schema.py`

**Interfaces:**
- Produces: `DocumentSource(document_id, revision, file_name, file_type, normalized_text, created_at, updated_at)`.
- Produces: DB table `document_sources` with `document_id` primary key and `FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE`.
- Consumes: existing `Document.id`, `Document.current_revision`.

- [ ] **Step 1: Write failing ORM/migration discovery tests**

Add tests to `tests/test_document_schema.py`:

```python
def test_document_source_model_is_exported():
    from app.models import DocumentSource

    assert DocumentSource.__tablename__ == "document_sources"
    assert DocumentSource.document_id.property.columns[0].primary_key
    assert DocumentSource.normalized_text.property.columns[0].nullable is False


def test_document_source_migration_file_exists():
    from pathlib import Path

    migration = Path("alembic/versions/0016_document_sources.py").read_text(encoding="utf-8")
    assert 'revision: str = "0016"' in migration
    assert 'down_revision: Union[str, None] = "0015"' in migration
    assert 'op.create_table("document_sources"' in migration
    assert 'ondelete="CASCADE"' in migration
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_document_schema.py -q`
Expected: FAIL because `DocumentSource` and migration do not exist.

- [ ] **Step 3: Create `DocumentSource` model**

Implement `app/models/document_source.py`:

```python
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentSource(Base):
    __tablename__ = "document_sources"

    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

- [ ] **Step 4: Export the model**

Modify `app/models/__init__.py` to import `DocumentSource` and include it in `__all__`.

- [ ] **Step 5: Create migration `0016_document_sources.py`**

Use handwritten Alembic style matching `0015`:

```python
"""document source normalized text

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-06
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_sources",
        sa.Column("document_id", sa.UUID(), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=512), nullable=True),
        sa.Column("file_type", sa.String(length=32), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("document_sources")
```

- [ ] **Step 6: Run tests and commit**

Run: `python -m pytest tests/test_document_schema.py -q`
Expected: PASS.

Commit:

```bash
git add app/models/document_source.py app/models/__init__.py alembic/versions/0016_document_sources.py tests/test_document_schema.py
git commit -m "feat: add document source model"
```

---

### Task 2: Structured Chunks And Source Persistence

**Files:**
- Modify: `app/services/splitter.py`
- Modify: `app/services/ingest.py`
- Modify: `app/api/documents.py`
- Modify: `tests/test_splitter.py`
- Modify: `tests/test_query_import_api.py`

**Interfaces:**
- Produces: structured chunk dict `{"text": str, "source_start": int, "source_end": int, "location": dict}`.
- Produces: `split_structured_text(text, *, chunk_size, chunk_overlap, splitter="text", base_location=None) -> list[dict]`.
- Produces: `doc_data["source"] = {"normalized_text": str, "file_name": str | None, "file_type": str | None}` for import paths that can provide source text.
- Consumes: `DocumentSource` from Task 1.

- [ ] **Step 1: Write failing splitter tests**

Add to `tests/test_splitter.py`:

```python
from app.services.splitter import split_structured_text


def test_split_structured_text_offsets_match_source_slices():
    text = "第一行\n第二行很长" * 30
    chunks = split_structured_text(text, chunk_size=80, chunk_overlap=10, splitter="text")
    assert chunks
    for chunk in chunks:
        assert text[chunk["source_start"]:chunk["source_end"]] == chunk["text"]
        assert chunk["location"]["type"] == "line"
        assert chunk["location"]["start_line"] >= 1


def test_split_structured_text_none_returns_full_span():
    text = "alpha\nbeta"
    chunks = split_structured_text(text, chunk_size=100, chunk_overlap=0, splitter="none")
    assert chunks == [{"text": text, "source_start": 0, "source_end": len(text), "location": {"type": "line", "start_line": 1, "end_line": 2}}]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_splitter.py -q`
Expected: FAIL because `split_structured_text` does not exist.

- [ ] **Step 3: Implement structured splitter**

Add helper functions in `app/services/splitter.py` without changing `split_text` output:

```python
def _line_location(text: str, start: int, end: int) -> dict:
    return {
        "type": "line",
        "start_line": text.count("\n", 0, start) + 1,
        "end_line": text.count("\n", 0, max(start, end - 1)) + 1,
    }


def _find_from(text: str, piece: str, cursor: int) -> tuple[int, int]:
    idx = text.find(piece, cursor)
    if idx < 0:
        idx = text.find(piece)
    if idx < 0:
        raise ValueError("structured splitter could not map chunk to source text")
    return idx, idx + len(piece)


def split_structured_text(
    text: str, *, chunk_size: int, chunk_overlap: int, splitter: str = "text", base_location: dict | None = None
) -> list[dict]:
    pieces = split_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, splitter=splitter)
    out: list[dict] = []
    cursor = 0
    for piece in pieces:
        start, end = _find_from(text, piece, cursor)
        cursor = max(start + 1, end - chunk_overlap)
        location = dict(base_location or _line_location(text, start, end))
        if not base_location:
            location = _line_location(text, start, end)
        out.append({"text": piece, "source_start": start, "source_end": end, "location": location})
    return out
```

- [ ] **Step 4: Write failing ingest metadata tests**

Add focused service tests to `tests/test_query_import_api.py` that exercise `ingest_service._chunk_text_and_metadata` directly before it exists:

```python
from app.services import ingest as ingest_service


def test_structured_chunk_metadata_includes_source_offsets():
    text, metadata = ingest_service._chunk_text_and_metadata(
        {"text": "第二行", "source_start": 4, "source_end": 7, "location": {"type": "line", "start_line": 2, "end_line": 2}},
        title="demo.txt",
        external_id="ext-1",
        revision=3,
    )

    assert text == "第二行"
    assert metadata == {
        "title": "demo.txt",
        "external_id": "ext-1",
        "source_start": 4,
        "source_end": 7,
        "location": {"type": "line", "start_line": 2, "end_line": 2},
        "source_revision": 3,
    }


def test_string_chunk_metadata_does_not_fabricate_offsets():
    text, metadata = ingest_service._chunk_text_and_metadata(
        "legacy chunk",
        title=None,
        external_id=None,
        revision=1,
    )

    assert text == "legacy chunk"
    assert metadata is None
```

- [ ] **Step 5: Extend ingest service to accept structured chunks**

In `app/services/ingest.py`, define a small normalization helper:

```python
def _chunk_text_and_metadata(chunk: str | dict, *, title: str | None, external_id: str | None, revision: int) -> tuple[str, dict | None]:
    base = {"title": title, "external_id": external_id} if (title or external_id) else {}
    if isinstance(chunk, str):
        return chunk, (base or None)
    text = str(chunk.get("text") or "")
    metadata = dict(base)
    metadata.update({
        "source_start": int(chunk["source_start"]),
        "source_end": int(chunk["source_end"]),
        "location": chunk.get("location") or {},
        "source_revision": revision,
    })
    return text, metadata
```

Use it in both chunk creation loops with `revision=doc.current_revision` or `revision=document.current_revision` after `_new_generation` has advanced the revision. Keep string chunks backward compatible and do not fabricate metadata.

- [ ] **Step 6: Persist `DocumentSource` in import/replacement paths**

In `app/api/documents.py`, import `DocumentSource` and add `_upsert_document_source(db, document_id, revision, source)`:

```python
async def _upsert_document_source(db, document_id, revision: int, source: dict | None) -> None:
    if not source:
        return
    existing = await db.get(DocumentSource, document_id)
    values = {
        "revision": revision,
        "file_name": source.get("file_name"),
        "file_type": source.get("file_type"),
        "normalized_text": source["normalized_text"],
    }
    if existing is None:
        db.add(DocumentSource(document_id=document_id, **values))
    else:
        for key, value in values.items():
            setattr(existing, key, value)
```

Call it after new/updated document and chunks are created, before `commit`, for non-dedup created/updated documents. Do not change dedup hits where no new document is created.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/test_splitter.py tests/test_query_import_api.py -q`
Expected: PASS.

Commit:

```bash
git add app/services/splitter.py app/services/ingest.py app/api/documents.py tests/test_splitter.py tests/test_query_import_api.py
git commit -m "feat: persist source offsets for chunks"
```

---

### Task 3: Extractor Location Metadata

**Files:**
- Modify: `app/services/pdf_extract.py`
- Modify: `app/services/docx_extract.py`
- Modify: `app/services/xlsx_extract.py`
- Modify: `app/api/documents.py`
- Modify: `tests/test_pdf_extract.py`
- Modify: `tests/test_docx_extract.py`
- Modify: `tests/test_xlsx_extract.py`

**Interfaces:**
- Produces: `build_pdf_source(data, ...) -> {"normalized_text": str, "chunks": list[dict]}` with PDF page locations.
- Produces: `build_docx_source(data, *, ocr=None, chunk_size, chunk_overlap, table_aware=False) -> {"normalized_text": str, "chunks": list[dict]}`.
- Produces: `build_xlsx_source(data, *, chunk_size, chunk_overlap) -> {"normalized_text": str, "chunks": list[dict]}`.
- Consumes: `split_structured_text` from Task 2.

- [ ] **Step 1: Write failing extractor tests**

Add tests asserting:

```python
def assert_structured_chunks_match_source(source):
    text = source["normalized_text"]
    assert text.strip()
    assert source["chunks"]
    for chunk in source["chunks"]:
        assert text[chunk["source_start"]:chunk["source_end"]] == chunk["text"]
        assert isinstance(chunk["location"], dict)
```

PDF tests must assert `location["type"] == "page"` and `location["page"] == 1` for a one-page fixture. DOCX tests must assert paragraph/table locations are present. XLSX tests must assert `location["type"] == "sheet_row"`, `sheet` is the worksheet name, and row range is present.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_pdf_extract.py tests/test_docx_extract.py tests/test_xlsx_extract.py -q`
Expected: FAIL because source builder functions do not exist.

- [ ] **Step 3: Implement source builders**

Keep existing extraction functions unchanged for callers. Add new builders that normalize text in the same visible order, calculate offsets while building the normalized text, and return structured chunks directly. Do not compute offsets through later global fuzzy search.

For text-like and page/segment sources, use this construction pattern:

```python
parts: list[str] = []
chunks: list[dict] = []
cursor = 0
for item_text, location in items:
    if parts:
        parts.append("\n\n")
        cursor += 2
    start = cursor
    parts.append(item_text)
    cursor += len(item_text)
    end = cursor
    chunks.extend(_offset_chunks(item_text, start, location))
normalized_text = "".join(parts)
```

Where `_offset_chunks` calls `split_structured_text(item_text, ...)` and adds `base_offset` to each `source_start`/`source_end`.

- [ ] **Step 4: Wire import paths to builders**

In `app/api/documents.py`, for `.pdf`, `.docx`, `.xlsx`, `.txt`, `.md`, `.markdown`, `.json` single-document imports, set both:

```python
"source": {"normalized_text": normalized_text, "file_name": filename, "file_type": suffix.lstrip(".")},
"chunks": structured_chunks,
```

For CSV, keep one row as one document and use `location = {"type": "csv_row", "row": row_number}` for that row-document. Do not merge all CSV rows into one source document.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_pdf_extract.py tests/test_docx_extract.py tests/test_xlsx_extract.py tests/test_query_import_api.py -q`
Expected: PASS.

Commit:

```bash
git add app/services/pdf_extract.py app/services/docx_extract.py app/services/xlsx_extract.py app/api/documents.py tests/test_pdf_extract.py tests/test_docx_extract.py tests/test_xlsx_extract.py tests/test_query_import_api.py
git commit -m "feat: build structured import sources"
```

---

### Task 4: Source Location API

**Files:**
- Modify: `app/schemas/documents.py`
- Modify: `app/api/documents.py`
- Modify: `tests/test_query_import_api.py`
- Modify: `tests/test_chat_api.py` if chat source shape needs API fixtures updated.

**Interfaces:**
- Produces: `DocumentSourceLocationResponse` fields `document_title`, `file_type`, `text_window`, `window_start`, `window_end`, `source_start`, `source_end`, `location`, `chunk_id`, `chunk_seq`, `legacy`, `fallback_chunk`.
- Consumes: `DocumentSource` and `Chunk.chunk_metadata` source fields.

- [ ] **Step 1: Write failing API tests**

Add tests covering precise and legacy responses. The precise test must arrange a document, chunk, and document source where `normalized_text[source_start:source_end] == chunk.text` and assert:

```python
assert response.status_code == 200
data = response.json()
assert data["legacy"] is False
assert data["text_window"][data["source_start"] - data["window_start"]:data["source_end"] - data["window_start"]] == chunk.text
assert data["chunk_seq"] == chunk.seq
```

The legacy test must omit `DocumentSource` or source offsets and assert `legacy is True`, `fallback_chunk == chunk.text`, and the endpoint does not fabricate `source_start`/`source_end`.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_query_import_api.py -q`
Expected: FAIL because the endpoint/schema does not exist.

- [ ] **Step 3: Add schema**

Add to `app/schemas/documents.py`:

```python
class DocumentSourceLocationResponse(BaseModel):
    document_title: Optional[str] = None
    file_type: Optional[str] = None
    text_window: str = ""
    window_start: Optional[int] = None
    window_end: Optional[int] = None
    source_start: Optional[int] = None
    source_end: Optional[int] = None
    location: Optional[dict[str, Any]] = None
    chunk_id: str
    chunk_seq: int
    legacy: bool
    fallback_chunk: str = ""
```

- [ ] **Step 4: Implement endpoint**

In `app/api/documents.py`, add constant `SOURCE_CONTEXT_CHARS = 3000` and route:

```python
@router.get("/documents/{document_id}/source", response_model=DocumentSourceLocationResponse)
async def get_document_source(document_id: uuid.UUID, chunk_id: uuid.UUID = Query(...), lib: Library = Depends(require_lib("read")), db: AsyncSession = Depends(get_db)):
    ...
```

Validation order:
1. Load `Document`; 404 if absent, cross-library, or deleted.
2. Load `Chunk`; 404 if absent, wrong document, or wrong library.
3. Load `DocumentSource`; return legacy if absent.
4. Read `chunk.chunk_metadata or {}`; return legacy if offsets/revision/location are missing.
5. Require `metadata.source_revision == source.revision == document.current_revision`; otherwise legacy.
6. Validate integer bounds; otherwise legacy.
7. Return bounded text window around the span.

Use this legacy helper:

```python
def _legacy_source_response(doc: Document, chunk: Chunk, file_type: str | None = None) -> DocumentSourceLocationResponse:
    return DocumentSourceLocationResponse(
        document_title=doc.title,
        file_type=file_type,
        chunk_id=str(chunk.id),
        chunk_seq=chunk.seq,
        legacy=True,
        fallback_chunk=chunk.text,
    )
```

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_query_import_api.py tests/test_chat_api.py -q`
Expected: PASS.

Commit:

```bash
git add app/schemas/documents.py app/api/documents.py tests/test_query_import_api.py tests/test_chat_api.py
git commit -m "feat: expose document source location"
```

---

### Task 5: Citation Rendering Helpers

**Files:**
- Create: `admin-ui/src/chat_citations.js`
- Create: `admin-ui/chat_citations.test.mjs`
- Modify: `admin-ui/markdown.test.mjs` if sanitizer allow-list assertions need to include narrow data attributes.

**Interfaces:**
- Produces: `renderAssistantMarkdown(text, sources, { marked, DOMPurify }) -> string`.
- Produces: `extractCitationIndex(el) -> number | null`.
- Produces: `highlightSourceWindow(source) -> { before: string, match: string, after: string }`.
- Consumes: existing marked and DOMPurify packages.

- [ ] **Step 1: Write failing Node tests**

Create `admin-ui/chat_citations.test.mjs` with tests for single citations, consecutive citations, code/link/URL exclusions, out-of-range citations, XSS sanitization, and highlight math. Include assertions such as:

```javascript
assert.match(html, /class="chat-citation-badge"/);
assert.match(html, /data-citation-index="0"/);
assert.ok(!renderedCode.includes('chat-citation-badge'));
assert.deepEqual(highlightSourceWindow({ text_window: 'abcdef', window_start: 10, source_start: 12, source_end: 14 }), { before: 'ab', match: 'cd', after: 'ef' });
```

- [ ] **Step 2: Run tests to verify failure**

Run: `node --test admin-ui/chat_citations.test.mjs`
Expected: FAIL because helper file does not exist.

- [ ] **Step 3: Implement helpers**

Implement DOM traversal after sanitization. Use a detached `<template>` when `document` exists; for Node tests, expose pure helpers that can be tested without browser DOM. Do not add inline event handlers. Keep DOMPurify allow-list equivalent to current tags/attrs plus only `class`, `role`, `tabindex`, `aria-label`, and `data-citation-index` if markup is sanitized with those attributes.

Badge markup must be:

```html
<sup><button type="button" class="chat-citation-badge" data-citation-index="0" aria-label="查看引用 1">1</button></sup>
```

- [ ] **Step 4: Run tests and commit**

Run:

```powershell
node --test admin-ui/chat_citations.test.mjs admin-ui/markdown.test.mjs
node --check admin-ui/src/chat_citations.js
```

Expected: PASS.

Commit:

```bash
git add admin-ui/src/chat_citations.js admin-ui/chat_citations.test.mjs admin-ui/markdown.test.mjs
git commit -m "feat: render clickable chat citations"
```

---

### Task 6: Chat UI Modals And Source API Wiring

**Files:**
- Modify: `admin-ui/src/api.js`
- Modify: `admin-ui/src/views/Chat.js`
- Modify: `admin-ui/style.css`
- Modify: `admin-ui/chat_redesign.test.mjs`
- Create or modify: `admin-ui/chat_source_location.test.mjs`

**Interfaces:**
- Consumes: `renderAssistantMarkdown`, `extractCitationIndex`, `highlightSourceWindow` from Task 5.
- Consumes: `GET /libraries/{slug}/documents/{document_id}/source?chunk_id={chunk_id}` from Task 4 via `api.getDocumentSource(slug, documentId, chunkId)`.

- [ ] **Step 1: Write failing frontend tests**

Add `admin-ui/chat_source_location.test.mjs` asserting `Chat.js`, `api.js`, and CSS contain the required wiring:

```javascript
assert.ok(api.includes('getDocumentSource'));
assert.ok(chat.includes('openCitationChunk'));
assert.ok(chat.includes('sourceLocationDialog'));
assert.ok(chat.includes('recalledChunkDialog'));
assert.ok(chat.includes('@click="openDocDetail(s)"'));
assert.ok(!chat.includes('window.open(resolved.href'));
assert.match(css, /\.chat-source-location-dialog/);
assert.match(css, /width:\s*900px/);
assert.match(css, /calc\(100vw - 24px\)/);
```

- [ ] **Step 2: Run tests to verify failure**

Run: `node --test admin-ui/chat_source_location.test.mjs`
Expected: FAIL because wiring does not exist.

- [ ] **Step 3: Add API client**

In `admin-ui/src/api.js` add:

```javascript
export const getDocumentSource = (slug, documentId, chunkId) =>
    request(`/libraries/${slug}/documents/${documentId}/source?` + new URLSearchParams({ chunk_id: chunkId }).toString());
```

- [ ] **Step 4: Wire `Chat.js` state and handlers**

Import helpers, replace `renderMarkdown` calls with `renderAssistantMarkdown(m.text, m.sources, { marked, DOMPurify })`, add delegated click/keydown handlers on `.chat-markdown`, and add two Element Plus dialogs:

1. Recalled chunk modal: title, score, chunk sequence, full content, copy button.
2. Source-location modal: large in-page dialog, header metadata, bounded pre-wrapped text window with highlight, legacy warning `该文档需重新导入后才能精确定位`, and fallback chunk on API failure/legacy.

`openDocDetail(source)` must call `api.getDocumentSource(currentSlug.value, source.document_id, source.chunk_id)` and must not use `router.resolve` or `window.open`.

- [ ] **Step 5: Add CSS**

In `admin-ui/style.css`, add classes for `.chat-citation-badge`, `.chat-source-location-dialog`, `.chat-source-window`, `.chat-source-highlight`, `.chat-recalled-dialog`, with responsive rules at `1199px` and `899px`. Desktop dialog width about `900px`; small-screen width `calc(100vw - 24px)`. Ensure no horizontal overflow.

- [ ] **Step 6: Run frontend tests and commit**

Run:

```powershell
node --test admin-ui/chat_citations.test.mjs admin-ui/chat_source_location.test.mjs admin-ui/chat_redesign.test.mjs admin-ui/markdown.test.mjs
node --check admin-ui/src/chat_citations.js
node --check admin-ui/src/views/Chat.js
```

Expected: PASS.

Commit:

```bash
git add admin-ui/src/api.js admin-ui/src/views/Chat.js admin-ui/style.css admin-ui/chat_redesign.test.mjs admin-ui/chat_source_location.test.mjs
git commit -m "feat: show chat source locations in page"
```

---

### Task 7: Final Verification And Release Safety

**Files:**
- Modify only files required by failing verification.

**Interfaces:**
- Consumes all previous task outputs.

- [ ] **Step 1: Run required frontend verification**

Run:

```powershell
node --test admin-ui/*.test.mjs admin-ui/src/**/*.test.mjs
node --check admin-ui/src/chat_citations.js
node --check admin-ui/src/views/Chat.js
```

Expected: PASS.

- [ ] **Step 2: Run required backend verification**

Run:

```powershell
python -m pytest tests/test_pdf_extract.py tests/test_docx_extract.py tests/test_xlsx_extract.py tests/test_splitter.py tests/test_query_import_api.py tests/test_chat_api.py
```

Expected: PASS. If the environment lacks dependencies, document the exact missing modules and the failed install command output.

- [ ] **Step 3: Run release safety and diff checks**

Run:

```powershell
python scripts/check_release_safety.py
git diff --check
git status --short
```

Expected: release safety passes, diff has no whitespace errors, status only contains intended files or is clean after commits.

- [ ] **Step 4: Commit verification fixes if needed**

If any verification fix was required:

```bash
git add <changed-files>
git commit -m "fix: stabilize chat source location verification"
```

---

## Baseline Notes

- Worktree path: `.worktrees/chat-citation-source-location` under the repository root.
- Branch: `feature/chat-citation-source-location`.
- Dependency setup attempted with `py -m pip install -r requirements-dev.txt`; it failed on PyPI SSL while fetching Alembic metadata.
- Existing Node baseline passed: `node --test admin-ui/chat_redesign.test.mjs admin-ui/markdown.test.mjs admin-ui/copy_answer.test.mjs`.
- Existing Python baseline with default `python -m pytest tests/test_pdf_extract.py tests/test_docx_extract.py tests/test_query_import_api.py tests/test_chat_api.py` failed during collection because this interpreter lacks `pypdf` and `sqlalchemy`.
