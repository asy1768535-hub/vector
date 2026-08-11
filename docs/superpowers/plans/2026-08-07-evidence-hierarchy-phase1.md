# Evidence Hierarchy Phase 1: EvidenceLocatorV1

## 1. Scope and frozen baseline

This plan implements only Phase 1 of the evidence-centered GraphRAG plan:
normalize file-derived evidence into a stable hierarchy that can be read by
embedding, retrieval, citation, and later graph work.

This plan does not implement Claim Graph, canonical mapping, rule candidates,
hierarchical RAG, long-context packing, publication changes, or new graph
ontology behavior. Claim Graph tables belong to Phase 2 and are deliberately
absent here.

Phase 0 is a frozen baseline. Do not modify its publication repair or rerun its
product tasks as part of this work. The Automatic import projection mismatch
(`processing/embedding` in the raw row versus `succeeded/completed` in the
projection) is a separate debt item and must not be fixed opportunistically in
Phase 1.

## 2. Audit result

### 2.1 GitNexus gate

The required GitNexus attempt was made before this plan was written:

```text
npx gitnexus status
```

reported a stale index (indexed 2026-07-31; current commit `b37c051`). The
required refresh was attempted:

```text
npx gitnexus analyze
```

It failed before indexing because `tree-sitter-kotlin` could not load its
`node-gyp-build` module under Node `v24.18.0`.

Per `AGENTS.md`, the rest of this plan uses the fallback: definitions and
callers were inspected with `rg`, and API, worker, parser, storage, embedding,
retrieval, and evidence-read paths were checked manually. No business symbol
was edited in this planning task. Future implementation edits must repeat this
fallback impact check for each changed symbol. Import, parsing, revision,
embedding, retrieval, and evidence APIs are shared paths and are HIGH risk;
changes to revision immutability or storage ownership are HIGH risk as well.

### 2.2 Existing data flow

```text
upload API / legacy ingest
  -> DocumentImportJob staging file
  -> importer._process_claimed_job
  -> parse_import_file
       -> PDF/DOCX/XLSX parser or text/CSV/JSON parser
       -> ParsedImport(normalized_text, chunks, splitter_name)
  -> ingest.ingest_text / reingest_document
  -> Document + DocumentRevision
  -> evidence_write_path.create_evidence_generation
       -> DocumentBlock
       -> EvidenceUnit
       -> Chunk
       -> ChunkBlock + ChunkEvidence
  -> EmbeddingJob
  -> embedder._build_payload
  -> embedding provider / Qdrant
  -> retrieval.run_retrieval / federated retrieval
  -> evidence_read.get_chunk_source / get_evidence_detail
```

The revision-file path runs beside parsing: the importer persists the original
file as `DocumentRevisionFile` after a revision is created. The file identity
therefore comes from the raw upload, while `DocumentRevision.content_hash` is
currently the SHA-256 of normalized imported text (`_text_hash`), not a raw
file digest. These hashes must remain distinct in EvidenceLocatorV1.

### 2.3 Current identity and locator inventory

| Layer | Current stable identity | Current locator/parent data | Gap for Phase 1 |
|---|---|---|---|
| Document | `Document.id`, library, source path, external id | `content_hash`, metadata, current/latest revision pointers | source path is not a complete immutable source locator |
| Revision | `DocumentRevision.id`, document id, `revision_no` | normalized text, parser/chunking name/version/config, content hash | no typed hierarchy locator; immutability is application convention, not an explicit locator contract |
| Original file | `DocumentRevisionFile.id`, revision id | raw `sha256`, size, storage provider/path/object key/version/etag, `source_locator` | parser provenance is not attached to the file record |
| Block/section | `DocumentBlock.id`, revision id, `seq` | `parent_block_id`, `block_kind`, title path, source offsets, page range, `content`, `position` | writer currently creates one paragraph block per prepared chunk; no stable Section/structured-unit vocabulary |
| Chunk | `Chunk.id`, revision id, seq | block/evidence references, text, offsets, page range, title path, position, JSON metadata | structured coordinates are parser-specific metadata and not validated by one contract |
| Evidence | `EvidenceUnit.id`, revision id | block reference, quote/hash, offsets, pages, title path, position, JSON metadata | normally generated from chunk; no first-class section or row/cell evidence contract |
| Vector point | deterministic revision/chunk point id | Qdrant payload includes chunk, revision, evidence and source metadata | locator fields are a payload projection, not the source of truth |

Existing `EvidenceUnit` and `Chunk` reads enforce library/document/revision
scope and require an explicit revision for historical evidence. Phase 1 must
preserve this behavior.

### 2.4 Real format support today

The following is an audit of current code, not a claim that the target contract
already exists.

| Format | Current behavior | Current locator fidelity | Phase 1 target |
|---|---|---|---|
| Native PDF | `pypdf` extracts page text; page chunks retain page and extraction mode | page number and normalized-text offsets; no text-run coordinates | page unit plus optional region/line metadata; preserve native versus OCR mode |
| Scanned PDF | OCR path renders pages through `pypdfium2`/Pillow when enabled and available; limits and failure hints exist | page and extraction mode; OCR confidence and word/line boxes are not persisted by the parser contract | persist OCR engine/version, confidence, and optional bounding boxes without claiming missing values |
| DOCX prose/headings | `python-docx`; paragraph order and heading paths are available | title path; no real page number or layout coordinate | section/paragraph unit with document-order identity and optional table/image provenance |
| DOCX tables | table segments retain table index, header, and rows; merged cells are deduplicated | mostly flattened text; no cell-level stable locator or page/box | table, row, and cell locators where the parser can prove them; otherwise mark unavailable |
| XLSX/XLS | XLSX through `openpyxl`; XLS through optional `xlrd`; worksheets become table segments | sheet name, row numbers, headers and rows are retained; column letters/cells/formula provenance are not | sheet/table/row/cell locator; distinguish formula, cached value, and source value when available |
| CSV | `csv.reader` joins each row with `" | "` and sends it through text splitting | source type only; row/column/cell coordinates are lost | row and cell coordinates from the parser, with stable header mapping where unambiguous |
| Image upload | no direct image branch in `parse_import_file`; direct image files are not currently real supported imports | not applicable as a standalone upload | explicitly unsupported until a bounded image import path is implemented |
| Embedded DOCX image | optional OCR callback can contribute text in document order | OCR text only in current flattened segment; no persisted confidence/box | image region unit and OCR quality metadata when available |
| PDF embedded image | PDF extraction can OCR image regions/pages if enabled | mode/page is retained; region confidence/box is not a stable persisted contract | same OCR quality contract as scanned pages |
| JSON | `json.load` then pretty-prints JSON and text-splits it | source type and text offsets only; no JSON Pointer, record key, or field path | JSON document/record/field structured units with JSON Pointer where deterministic |

The current tests use fake PDF readers/renderers, fake OCR callbacks, generated
DOCX/XLSX files, and CSV import assertions. They do not prove production OCR
model availability, direct image upload, cell-level locators, JSON Pointer
locators, or durable locator round trips through a live database.

## 3. EvidenceLocatorV1 contract

### 3.1 Hierarchy

The minimum hierarchy is:

```text
Document (logical identity)
  -> DocumentRevision (immutable parsed snapshot)
     -> Section (ordered structural block; may have parent Section)
        -> Chunk | Structured Unit (retrieval/evidence unit)
```

`DocumentBlock` is the compatible storage representation of Section and
structured parent units. `Chunk` remains the embedding unit. `EvidenceUnit`
remains the citation/evidence projection. No new Claim Graph object is
introduced.

### 3.2 Canonical envelope

The first implementation should store this envelope in existing JSONB fields:

```json
{
  "locator_version": "v1",
  "document_id": "uuid",
  "document_revision_id": "uuid",
  "revision_no": 3,
  "document_revision_file_id": "uuid",
  "raw_file_sha256": "64 lowercase hex chars",
  "normalized_content_hash": "64 lowercase hex chars",
  "unit_id": "uuid",
  "parent_unit_id": "uuid or null",
  "unit_kind": "section|chunk|structured_unit|table|row|cell|image_region",
  "ordinal": 12,
  "parser": {"name": "...", "version": "...", "config_hash": "..."},
  "source": {
    "kind": "text|pdf|docx|xlsx|xls|csv|json|image",
    "file_name": "...",
    "page": {"start": 2, "end": 2},
    "text": {"start": 100, "end": 140, "ranges": []},
    "heading_path": [],
    "table": {"index": 0, "name": null},
    "sheet": {"name": "Sheet1"},
    "row": {"start": 4, "end": 4},
    "column": {"start": "A", "end": "C"},
    "cell": {"start": "A4", "end": "C4"},
    "json_pointer": "/records/0/name",
    "bbox": [0, 0, 100, 20]
  },
  "quality": {
    "extraction_mode": "native|ocr|mixed|unparsed|not_applicable",
    "ocr_engine": null,
    "ocr_engine_version": null,
    "ocr_confidence": null
  },
  "unit_text_sha256": "64 lowercase hex chars",
  "quote_sha256": "64 lowercase hex chars"
}
```

The envelope is a contract shape, not a requirement that every source populate
every member. Absent or unprovable coordinates must be `null` or omitted; they
must never be guessed. `source.text` is relative to the immutable normalized
revision text. `unit_text_sha256` hashes the exact unit text, and
`quote_sha256` hashes the exact evidence quote. `raw_file_sha256` is the
uploaded object identity and must not be replaced by normalized text hash.

### 3.3 Storage mapping

* `DocumentRevisionFile.source_locator` stores the file-level source identity.
* `DocumentBlock.content` stores the canonical envelope plus parser-specific
  structural data; `DocumentBlock.position` remains a compact compatibility
  projection of `source`.
* `Chunk.chunk_metadata` stores the same envelope with `unit_kind=chunk` or
  `structured_unit`; scalar page/offset/title fields remain populated for
  existing SQL/API consumers.
* `EvidenceUnit.evidence_metadata` stores the same envelope and is the source
  used by citation/evidence reads when a structured locator is needed.
* `Chunk.block_id` and `Chunk.evidence_id`, plus `EvidenceUnit.document_block_id`,
  are authoritative links. JSON IDs must agree with these columns or the write
  must fail.
* Qdrant payload receives a bounded projection: revision id, chunk id, evidence
  id, unit kind, page/title/source ranges, and a locator hash. Full source
  files, full document text, OCR output arrays, and unbounded metadata do not
  enter the vector payload.

### 3.4 Invariants

1. A locator is valid only within its `document_revision_id`; a revision ID and
   revision number mismatch is an error.
2. A ready revision is immutable. Re-parsing, changing parser/config, or
   replacing the file creates a new revision and new block/chunk/evidence IDs.
3. `parser_name`, `parser_version`, parser config hash, chunking strategy and
   chunking version are recorded on the revision and copied into locator
   metadata. Parser changes do not silently mutate old evidence.
4. `content_hash` for normalized text and `DocumentRevisionFile.sha256` for the
   raw object are both required for a complete file-backed locator.
5. Duplicate import with the same logical source and normalized content keeps
   the existing revision and reports `unchanged`; it must not duplicate
   evidence. A replacement or changed content creates a new revision while
   keeping old revisions readable by explicit revision ID.
6. `parent_unit_id` must point to a unit in the same revision and precede the
   child in document order. Cycles and cross-revision parents are rejected.
7. `source_start/end` are half-open offsets into normalized text. Page, row,
   column, cell, JSON Pointer, and bounding-box values are source-specific and
   must pass shape/range validation.
8. OCR confidence is a nullable numeric value in `[0, 1]` per OCR unit. The
   engine, engine version, and extraction mode are recorded. No confidence is
   synthesized from a parser that does not provide it.
9. Bounding boxes use `[x_min, y_min, x_max, y_max]` in the source image/page
   coordinate system, with a coordinate-system label and page/image dimensions
   when known. Coordinates are optional, but an emitted box must be finite and
   ordered.
10. A quote is accepted only when it can be checked against the revision text
    or a source-specific structured unit. Invalid quote/hash/locator links fail
    the unit rather than silently producing untraceable evidence.
11. Locator metadata is bounded and JSON serializable. Large OCR arrays and
    parser internals stay in the revision/source artifact or object storage,
    not in every chunk/evidence row.

## 4. Compatibility and migration strategy

The default Phase 1 design is additive at the application contract level and
uses the existing tables/JSONB columns. Do not add a migration merely to
rename `DocumentBlock` to Section or to duplicate IDs already present.

If measured locator queries cannot be served acceptably from JSONB, use one
small additive migration only after the contract and backfill are validated.
Candidate columns are `locator_version`, `unit_kind`, and a bounded locator
hash or typed source-kind discriminator; the full envelope remains JSONB. No
Claim Graph tables belong in this migration.

Rollout order:

1. Add contract code and validators with dual-read: read new envelope first,
   fall back to existing scalar fields/metadata for legacy rows.
2. Add dual-write for new imports. Keep scalar page/offset/title fields and
   legacy `position` populated until all consumers are migrated.
3. Backfill only immutable ready revisions in bounded batches. Each backfill
   records parser/version uncertainty instead of inventing provenance; legacy
   rows with insufficient evidence get `locator_version=v1` with explicit
   `quality/provenance_status=legacy_unverified` or remain legacy-readable.
4. Validate counts, parent links, revision scope, quote hashes, and read parity
   before enabling locator-dependent retrieval or citation behavior.
5. Build any optional indexes concurrently, measure query latency, and roll out
   per library/feature flag.
6. Roll back by disabling new reads and retaining dual-write; never delete old
   evidence or rewrite historical revisions. A failed backfill is reverted by
   marking its batch incomplete, not by destructive table cleanup.

API compatibility requirements:

* Existing document, chunk, evidence, source-window, and retrieval response
  fields remain unchanged.
* Add an optional `locator` object only to versioned/read models or behind an
  additive response field; old clients continue to receive scalar fields.
* Historical reads keep requiring explicit `revision_id`.
* Retrieval filters continue using library, visibility, security, and current
  revision rules. Locator enrichment happens after candidate selection unless
  a measured indexed locator query is explicitly enabled.
* Qdrant payload changes are additive. Existing points remain readable during
  dual-read; re-embedding is not required solely to backfill PostgreSQL
  evidence metadata.

## 5. Executable milestones

### M0 - Contract and audit fixtures

**Files:** new contract/validation module under `app/schemas` or
`app/services`, parser fixtures under `tests/`, this plan. No migration.

**Risk:** HIGH because the contract is shared by import, evidence, and
retrieval paths.

**Work:** define typed locator shapes, source-kind rules, hash rules, parent
checks, OCR confidence and bbox validation; make absence explicit; create
representative fixtures for all currently supported formats.

**Tests:** pure contract tests for valid/invalid offsets, hashes, parent scope,
confidence, bbox, JSON Pointer, and legacy metadata fallback.

**Gate:** no production write path changes until the contract rejects malformed
locators deterministically and accepts current legacy scalar metadata through
dual-read.

### M1 - Parser provenance and structured units

**Files:** `app/services/import_parsing.py`, `pdf_extract.py`, `docx_extract.py`,
`xlsx_extract.py`, `splitter.py`, `ocr.py`; parser tests.

**Risk:** HIGH; parser changes alter normalized text, offsets, chunk counts, and
embedding inputs.

**Work:** emit a common intermediate segment/unit shape with source kind,
ordinal, parent/section information, parser version, exact source ranges, and
only proven page/table/sheet/row/column/cell/JSON Pointer data. Preserve native,
OCR, mixed, and unparsed PDF modes. Make OCR line confidence/bbox available
without requiring it when the OCR engine is absent. Keep direct image upload
explicitly unsupported unless a separate bounded parser is delivered.

**Tests:** native/scanned/mixed PDF; DOCX headings, paragraphs, tables and
embedded image OCR; XLSX sheets/rows/cells and formulas; CSV rows/cells; JSON
records and pointers; missing OCR dependency and malformed input.

**Gate:** parser fixtures round-trip source locations and never claim a
coordinate that the source parser did not provide.

### M2 - Revision-scoped write path

**Files:** `app/services/evidence_write_path.py`, `app/services/ingest.py`,
`app/workers/importer.py`, models only if an additive typed column is proven
necessary; import/evidence tests.

**Risk:** HIGH/CRITICAL for revision immutability, duplicate import, and
replacement behavior.

**Work:** create Section/structured parent blocks and child Chunk/Evidence
records from the common intermediate shape; populate the canonical envelope
and retain existing scalar projections. Enforce same-revision links, exact
quote hashes, raw-file versus normalized-content hashes, and parser/config
provenance. Preserve unchanged-import detection and explicit replacement
revision semantics.

**Tests:** new import, same-content duplicate, changed-content import, forced
replacement, historical evidence read, parent-child ordering, rollback on
invalid unit, and no orphan block/chunk/evidence rows.

**Gate:** one source file produces a complete revision-scoped hierarchy; a
second import cannot mutate the first revision's locators or evidence.

### M3 - Embedding and retrieval dual-read

**Files:** `app/workers/embedder.py`, `app/services/retrieval.py`,
`app/services/federated_retrieval.py`, `app/services/evidence_read.py`, relevant
schemas/APIs.

**Risk:** HIGH because Qdrant payload and citation behavior are shared.

**Work:** add bounded locator projection to new embedding payloads; make source
reads use the canonical envelope with scalar fallback; preserve current
revision filtering, visibility/security checks, and explicit historical reads.
Do not make retrieval depend on unindexed JSON locator fields in the first
release.

**Tests:** legacy and new payload retrieval, source-window parity, evidence
detail parity, revision replacement visibility, federated source projection,
payload size bounds, and missing/invalid locator fallback.

**Gate:** old points and new points return equivalent existing API fields, and
new evidence can be traced from hit to chunk to revision file.

### M4 - Backfill and optional index rollout

**Files:** backfill/maintenance command, migration only if M0-M3 measurements
prove it necessary, operational docs and tests.

**Risk:** HIGH for data consistency and database load.

**Work:** implement resumable, bounded backfill for ready revisions; record
legacy provenance uncertainty; verify parent/hash/revision invariants. Add only
measured indexes, preferably concurrently, and expose progress and failure
counts. Keep dual-read enabled throughout.

**Tests:** idempotent restart, partial failure, rollback by feature flag,
large-revision batching, index explain/latency checks, and old-client API
compatibility.

**Gate:** backfill reaches an auditable completion count without changing
normalized text, revision IDs, or historical evidence content.

### M5 - Acceptance and operational handoff

**Files:** import/evidence/retrieval acceptance tests, docs, metrics/health
checks. Do not touch publication code or Phase 0 artifacts.

**Risk:** MEDIUM after M2/M3, still HIGH for production rollout.

**Work:** run the format matrix against fresh isolated fixtures and verify API
and citation locator round trips. Publish parser/contract versions and rollout
flags in operational logs without logging file contents, secrets, or full OCR
payloads.

**Gate:** all tests below pass; legacy and v1 reads agree; rollback is tested;
and the Phase 0 required/automatic publication baseline remains unchanged.

## 6. Test matrix and performance boundaries

| Area | Required assertions |
|---|---|
| Identity/revision | raw SHA and normalized hash are distinct; duplicate is unchanged; replacement creates an immutable new revision |
| Parent-child | same-revision parents, ordered children, no cycles or orphans |
| PDF | native page locators; scanned OCR mode; mixed order; missing OCR; confidence/box round trip when supplied |
| DOCX | heading path; paragraph order; table index/row/cell where available; embedded image provenance |
| XLSX/XLS | sheet, row, column, cell; formula/cached-value provenance; optional XLS dependency behavior |
| CSV | row and cell locators; quoted commas/newlines; stable header mapping only when unique |
| JSON | deterministic JSON Pointer and record/field units; arrays and duplicate-looking values remain distinguishable by pointer |
| Evidence API | current and historical revision scope, quote/hash validation, legacy fallback, locator response field compatibility |
| Retrieval | Qdrant payload projection, old/new point parity, revision/security filters, federated source projection |
| Failure/rollback | parser failure leaves no partial evidence; backfill retry is idempotent; feature flag disables v1 reads safely |

Initial operational limits:

* stream file hashing and bounded parsing; do not load an unbounded file or OCR
  page set into one JSON document;
* keep per-unit metadata bounded and cap OCR region count per page according to
  existing settings;
* backfill in small transactions with resumable cursors and no table-wide lock;
* keep Qdrant locator payloads bounded and omit full raw text/large arrays;
* measure p95 import latency, peak memory, database write amplification,
  evidence-read latency, retrieval latency, and payload size before enabling
  structured locator filtering.

## 7. Open questions before implementation

1. Is `DocumentRevisionFile` guaranteed to exist for every legacy revision, or
   must the contract support a permanently file-less text revision?
2. Which coordinate systems must be standardized for PDF and image boxes
   (PDF points, rendered pixels, or both), and are page dimensions available at
   parse time?
3. Should XLSX formulas expose both formula text and cached value, and which is
   the evidence quote used for retrieval?
4. Is `xlrd` a supported production dependency for XLS, or should XLS remain a
   best-effort optional format?
5. What is the maximum accepted file/page/row/cell/OCR-region budget for a
   single import and for one evidence response?
6. Do existing API consumers need locator data immediately, or can it ship as
   an optional versioned field after dual-read is proven?
7. Which legacy rows may be marked `legacy_unverified` during backfill without
   implying that their old citations are invalid?

## 8. Completion definition

Phase 1 is complete only when the hierarchy is revision-scoped and immutable,
all supported parser outputs use the same locator contract, unsupported image
upload is documented as unsupported, evidence and retrieval reads preserve API
compatibility, backfill/rollback are tested, and the full matrix passes.

This is an implementation plan, not an implementation report. At this point no
business code, prompt, migration, frontend, real model call, E2E task, or
publication baseline was changed.
