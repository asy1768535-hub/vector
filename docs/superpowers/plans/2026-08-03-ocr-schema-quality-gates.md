# OCR and Graph Schema Quality Gates Implementation Plan

> Status: In progress. This document records the approved implementation
> boundaries; it does not authorize changes to existing Publication data.
>
> Authority: the current graph governance and schema lifecycle contracts in
> `.trellis/spec/`, the existing Publication implementation, and the current
> extraction test suite. External research is used for design input, not as a
> replacement for the repository contracts.

## Implementation Amendments (2026-08-03)

- Page-level routing is required, but a mixed page with sufficient native text
  must not be full-page OCR'd and then have duplicate native text appended.
  The first local implementation may OCR extracted embedded image bytes only;
  if the page cannot expose image regions, it records
  `native_visual_unparsed` for a later layout/vision parser.
- A closed Schema and endpoint validation are mandatory. Entity and relation
  extraction may remain one provider call when that is the existing contract;
  the non-negotiable boundary is that relation endpoints are validated against
  accepted, typed candidates before materialization/publication.
- The no-orphan rule applies to the formal business graph. Asset records and
  explicitly allowlisted standalone types may remain publishable without a
  relation.
- Hard-coded noise examples are defense-in-depth only. They must not replace
  Schema namespaces, type-specific validation, evidence checks, or the review
  queue.
- The current enterprise ontology snapshot has no persisted asset/business
  namespace or standalone-asset flag. Until a versioned Schema adds one, the
  materializer treats the standalone-asset allowlist as empty: extracted
  entities must be valid endpoints of an evidence-backed relation to be
  staged for the formal graph.

### Current implementation checkpoint

- Phase 3 is being implemented incrementally. The materializer no-orphan gate
  is complete. The Publication planner no-orphan gate is the current step;
  static-panorama work remains a separate follow-up step in this plan.

## 1. Problem Statement

The current pipeline has two independent-looking failures that share the same
root cause: intermediate results can be treated as final facts without enough
quality evidence.

### 1.1 PDF/OCR

`app/services/pdf_extract.py` uses a single native-text threshold. A page with
enough selectable text is accepted without checking whether it also contains a
screenshot, chart, scanned region, table, or vector diagram. This loses content
from mixed pages. `app/services/ocr.py` returns only joined text and discards
bounding boxes, confidence scores, and the parser identity needed to diagnose a
bad extraction.

The immediate risk is therefore not only inaccurate OCR. It is silent omission
of visual content in otherwise ordinary PDFs, followed by loss of page-level
provenance.

### 1.2 Entity and Relation Schema

The current enterprise seed contains broad types such as `policy`, `process`,
`document`, and `term`. A general-purpose extractor can therefore classify
chapter headings, dates, URLs, API paths, and action phrases as valid entities.

The current Publication planner can include draft facts when `include_drafts`
is true, and the extraction auto-publication path explicitly passes
`include_drafts=True`. The materializer also stages every qualifying entity
candidate before it knows whether that candidate participates in a valid
relation. These behaviors make it possible for low-value or unreviewed facts to
reach the graph consumed by retrieval or visualization.

## 2. Goals

1. Route PDF parsing per page instead of deciding OCR once for the whole file.
2. Preserve extraction evidence sufficient to locate a page, region, parser,
   confidence, and warning after an answer or graph fact is challenged.
3. Use a closed, versioned extraction Schema with explicit asset/business
   boundaries and allowlisted Relation Types.
4. Keep candidate extraction, validation, normalization, materialization, and
   Publication activation as separate gates.
5. Ensure formal active Publications contain only validated, eligible facts;
   drafts and pending-review facts remain outside the formal graph.
6. Make the future fixed static graph panorama read one active Publication
   snapshot, with no live-fact scan or frontend relationship synthesis.

## 3. Non-Goals

- Do not redesign or implement the advanced exploration/static panorama UI in
  this work package. Its separate UI plan must consume the active Publication
  contract defined here.
- Do not replace all OCR with a larger model or run multiple OCR engines on
  every page.
- Do not silently rewrite or delete existing entities, relations, evidence, or
  Publication history.
- Do not make a zero-relation extraction produce an active business graph.
- Do not add a new graph database or a second traversal backend.
- Do not treat a model confidence score as proof of correctness without schema,
  evidence, and structural validation.

## 4. Design Decisions From Research

The implementation should follow the common pattern used by current document
and GraphRAG systems:

- Use native text as the cheap first pass, then route only suspicious pages to
  OCR or layout-aware parsing. This is consistent with Unstructured's element
  based PDF strategy and Docling's OCR/layout concepts.
- Use layout-aware parsing for tables and complex pages. PaddleOCR
  PP-Structure is the preferred local Chinese parser for the first structured
  parsing integration; Docling can be evaluated as a compatible alternative.
- Separate entity discovery, entity type validation/normalization, and relation
  extraction. Neo4j GraphRAG and Microsoft GraphRAG both make Schema and
  structured extraction boundaries explicit rather than relying on unconstrained
  entity discovery.
- Keep source evidence and failed candidates. Dropping them after vector or
  graph insertion makes correction and rollback impractical.
- Start with deterministic rules and a small allowlist. Add model-based review
  only for cases that rules cannot decide.

## 5. Target Data Flow

```text
PDF page
  -> native text and visual/layout signals
  -> page route: native | OCR | mixed | layout | vision-review
  -> text/structure blocks with page and region evidence
  -> candidate entity extraction
  -> Schema/type/shape/quality validation
  -> normalization and entity linking
  -> candidate relation extraction from accepted entities
  -> relation/evidence/endpoint validation
  -> draft staging or pending_review
  -> Publication planner with drafts excluded
  -> explicit activation of an active snapshot
  -> static graph panorama / RAG retrieval
```

The asset catalog and business graph are separate projections of the same
source material:

```text
Knowledge assets: Document, Section, Page, Table, Image, URL, API, evidence
Business graph:   System, Project, Organization, Person, Device, Process,
                  Policy, and other explicitly approved business facts
```

Chapter numbers/titles, dates, page headers/footers, URLs, and API paths remain
useful assets or attributes. They are not business entities merely because a
model can name them.

## 6. Implementation Phases

### Phase 0 - Freeze the Contract and Build a Corpus (P0)

Before editing production symbols:

- Run GitNexus impact analysis for every function, class, or method that will be
  changed. A HIGH or CRITICAL result must be reported and reviewed before
  implementation.
- Preserve unrelated changes from other windows. Establish a file-level change
  allowlist before coding.
- Create a small evaluation corpus containing native PDFs, scan-only PDFs,
  mixed text-plus-screenshot pages, tables, multi-column pages, vector diagrams,
  headers/footers, API-heavy documents, and deliberately bad OCR samples.
- Record page-level expected signals and a small gold set of entities,
  relations, and rejected noise examples.

Deliverable: a versioned evaluation dataset and a contract test matrix. No
production data is modified.

### Phase 1 - Page-Level PDF Parsing Router (P1)

Target modules:

- `app/services/pdf_extract.py`
- `app/services/ocr.py`
- `app/config.py`
- the PDF/OCR focused tests and the revision parsing integration tests

Implementation:

1. Keep native `pypdf` extraction as the fast path.
2. Collect per-page signals before choosing a route: meaningful character
   count, replacement/garbled-character ratio, image count and coverage,
   text-block density, table/layout indicators, and vector drawing presence.
3. Use explicit routes:
   - `native`: text is sufficient and no visual signal is present.
   - `ocr`: scan-like page with no usable text layer.
   - `mixed`: retain native text and OCR/parse visual regions. The first
     adapter OCRs embedded image regions only; it does not OCR the whole page
     when native text is already sufficient.
   - `layout`: table, multi-column, or complex layout needs structure.
   - `vision_review`: diagram/chart or low-confidence region requires a bounded
     visual fallback.
4. Make thresholds configuration-backed and record the route decision. The
   current 20-character threshold remains a fallback signal, not the sole
   decision.
5. Use one primary OCR implementation for the first release. Preserve the
   current RapidOCR behavior for simple image OCR while defining the adapter
   boundary for PP-StructureV3 on routed complex pages.
6. Bound rendered DPI, page count, memory, timeout, and output size. A missing
   optional parser produces a fixed unavailable/review state rather than
   silently pretending the page was fully parsed.

Required output shape per page/block:

```json
{
  "page": 12,
  "block": "b-12-03",
  "extraction_mode": "native|ocr|mixed|layout|vision_review",
  "parser": "pypdf|rapidocr|paddleocr",
  "parser_version": "...",
  "text": "...",
  "confidence": 0.91,
  "bbox": [120, 80, 840, 620],
  "source_image": "page-12.png",
  "warnings": []
}
```

The exact persistence location should reuse the existing document/revision
evidence model where possible. A new table or migration is justified only if
the current evidence model cannot represent page, block, parser, and hash
identity without storing raw provider payloads in the graph.

Acceptance gates:

- Mixed pages do not skip visual parsing merely because native text exceeds the
  old threshold.
- Native-only pages do not invoke OCR unnecessarily.
- Page and block evidence is stable and can be linked back to the source.
- Missing OCR/layout dependencies are observable and do not create a false
  successful parse.
- Existing scan-PDF behavior and OCR page limits remain compatible.

### Phase 2 - Closed Schema and Candidate Quality Gates (P0)

Target modules:

- `app/services/graph_seed.py`
- `app/services/graph_extraction_prompt.py`
- `app/services/graph_candidate_routing.py`
- `app/services/graph_schema_validator.py`
- `app/services/graph_extraction_batch_eval.py`
- candidate and extraction tests

Implementation:

1. Version the extraction Schema independently from the provider prompt. Each
   extraction run records the Schema version, allowed entity types, allowed
   relation types, endpoint constraints, required attributes, and quality policy
   snapshot.
2. Classify types into explicit namespaces:
   - `asset`: document structure, page, table, image, URL, API, and source
     artifacts.
   - `business`: person, organization, system, project, process, policy,
     product, device, and other approved domain facts.
   - `attribute`: date, number, percentage, identifier, status, and scalar
     values that must not become nodes.
3. Keep extraction output closed-world: unknown types, unknown relation keys,
   invalid endpoints, missing required attributes, malformed names, and
   unsupported properties are rejected or sent to `pending_review`.
4. Add deterministic noise filters before normalization:
   - pure dates, numbers, percentages, and common page labels;
   - chapter/section number plus title patterns;
   - URLs, API paths, file paths, and code fragments unless explicitly routed
     to the asset/technical namespace;
   - generic action phrases such as "operation arrangement" or equivalent
     unanchored verbs;
   - repeated headers, footers, watermarks, and navigation labels.
5. Do not use fuzzy name matching as the only merge decision. Preserve the
   source evidence, type, Library, Ontology version, and candidate identity for
   every normalization decision.
6. Separate the validation contracts even when the provider uses one model
   pass: relation candidates must be checked against accepted, typed,
   normalized entity candidates and may not invent endpoints. A future
   two-pass extractor is optional, not a prerequisite for this release.
7. Add a review policy for high-risk values: dates/numbers/identifiers,
   legal/policy statements, table rows, and low-confidence OCR regions require
   stronger evidence or `pending_review`.

Acceptance gates:

- The examples `10.1 Detailed Design...`, dates, URLs, and API paths are
  rejected as business entities or routed to the correct asset/attribute class.
- Every accepted relation uses an allowlisted type and two accepted endpoints
  from the same Library and Ontology version.
- Candidate decisions are deterministic for the same input and Schema version.
- Batch evaluation reports precision/recall for accepted facts and rejection
  precision for known noise.

### Phase 3 - Materialization and Publication Safety (P0)

Target modules:

- `app/services/graph_extraction_materializer.py`
- `app/services/graph_publication_planner.py`
- `app/services/graph_extraction_auto_publication.py`
- `app/services/graph_publication_read.py`
- Publication and materializer tests

Implementation:

1. Preserve materialization as a staging step. A validated entity candidate may
   be materialized so a valid relation can reference it, but that does not make
   it publishable.
2. Define formal Publication eligibility as all of:
   - status is active and review policy is satisfied;
   - Schema type and shape validation pass;
   - evidence is active, current, Library-scoped, and revision-scoped;
   - the entity is a valid relation endpoint, or is explicitly allowlisted as a
     standalone asset type;
   - the relation type and endpoint constraint pass;
   - the item is not a draft, pending-review, stale, or degraded fact.
3. Remove the automatic extraction path's ability to publish drafts. The
   extraction auto-publication path must plan with drafts excluded. The
   `include_drafts` option, if retained for an administrative preview, must be
   impossible to activate as a formal retrieval Publication.
4. Keep the existing no-valid-relation fail-closed behavior and extend it to
   reject snapshots that contain only orphan business entities.
5. Record blocked counts by stable reason codes, including draft exclusion,
   schema rejection, noise rejection, missing evidence, invalid endpoint, and
   orphan entity. Do not put raw OCR text or provider payloads in audit records.
6. Revalidate the exact parent Publication, Schema version, evidence identity,
   and manifest hash during activation. Other-window changes are handled by
   parent/hash fencing; they must not be merged into a static view implicitly.

Required invariant:

```text
candidate -> validated staging -> eligible snapshot item -> planned Publication
          -> explicit activation -> active snapshot -> retrieval/visualization
```

Acceptance gates:

- A draft or pending-review fact cannot appear in an active Publication even if
  an extraction job auto-publishes.
- A relation with a missing, stale, cross-Library, or wrong-revision evidence
  row is excluded.
- An orphan business entity is excluded; an approved standalone asset follows
  its explicit asset policy.
- A concurrent Publication change causes a safe retry or a fixed failure, never
  a mixed snapshot.
- Historical Publications remain readable and are not silently rewritten.

### Phase 4 - Fixed Static Panorama Contract (Separate UI Work)

This phase is intentionally separate from the data-quality implementation. It
is the only place to change the advanced exploration presentation when that UI
work is resumed.

The panorama must:

- load one exact active Publication snapshot for the selected Library;
- render the snapshot's frozen Entity and Relation items, not live ORM rows;
- preserve Library, Ontology version, Publication ID, item hashes, and Evidence
  identities;
- show a fixed loading/empty/degraded/malformed state instead of falling back
  to drafts or synthesizing edges;
- handle a Publication change by discarding the stale response and requiring a
  new explicit load;
- leave the existing graph governance, catalog, and evidence drawers as the
  authority for inspection.

No frontend files are in scope for Phases 0-3. The separate UI change must
follow `.trellis/spec/frontend/graph-exploration-console.md` and its required
browser tests.

## 7. Existing Data and Migration Strategy

1. Do not delete or rewrite current facts, candidates, Evidence, or Publication
   history as part of the first rollout.
2. Change future Publication eligibility first. Existing active Publications
   remain immutable historical snapshots until an explicit replan is requested.
3. Add a dry-run audit command/report that evaluates the current active
   Publication against the new rules and reports:
   - draft/pending items;
   - noise-pattern matches;
   - orphan business entities;
   - invalid or stale Evidence;
   - type/relation constraint violations;
   - pages that would need re-parsing under the new OCR router.
4. Remediation is explicit and versioned: reparse the source revision, rerun
   extraction under the new Schema, review high-risk candidates, plan a new
   Publication, and activate only after the normal parent/hash checks.
5. If a migration is required for parser metadata or Schema namespaces, make it
   additive, backfillable, idempotent, and reversible at the data-model level.
   Never use a migration to infer corrected business facts from old OCR text.

## 8. Test and Measurement Plan

### Focused tests

- PDF route tests for native, scan, mixed, table, multi-column, vector, missing
  dependency, page limit, and parser timeout cases.
- OCR adapter tests for text, bbox, confidence, parser version, and failure
  metadata.
- Schema tests for allowlists, endpoint constraints, required properties,
  namespace separation, noise rejection, normalization, and idempotency.
- Materializer tests proving entity staging does not imply publication and that
  relation endpoints/evidence are exact.
- Publication tests proving `draft` and `pending_review` are never active
  snapshot items, including the automatic extraction path.
- Static panorama contract tests proving it reads one exact Publication and
  rejects stale or mismatched snapshot responses.

### Regression and release checks

- Existing OCR/PDF, candidate routing, materializer, auto-publication,
  Publication activation, retrieval, and Graph Catalog tests.
- Full non-frozen backend regression when backend truth changes.
- All required `admin-ui/*.test.mjs` tests only when the separate UI phase is
  implemented.
- Ruff, compile/import smoke, release safety, and `git diff --check`.
- Before any commit, run `gitnexus_detect_changes()` and verify the affected
  symbols and execution flows are within the approved allowlist.

### Initial metrics

Measure on the gold corpus and a sampled production-like corpus:

- native-text pages that contain missed visual regions;
- OCR/layout route rate and unnecessary OCR rate;
- page/block evidence completeness;
- accepted business-entity precision;
- known-noise rejection precision;
- valid relation endpoint rate;
- orphan business-entity rate;
- active Publication draft/pending count, which must be zero;
- active Publication items with invalid or missing Evidence, which must be zero.

Suggested rollout gates are to establish the baseline first, then require no
regression in native-text recall, zero draft/pending active items, zero invalid
active Evidence links, and a documented review queue for unresolved complex
pages. Thresholds for precision/recall should be set after the gold corpus is
measured rather than guessed globally.

## 9. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| OCR cost and latency increase | Route per page, cap rendering, use one primary engine, cache page fingerprints |
| False rejection of useful short entities | Require type-specific rules and retain pending review with evidence |
| Asset/business boundary is too rigid | Allow explicit asset namespace and versioned Schema changes |
| Existing active data conflicts with new rules | Keep historical Publications; remediate through a new Publication |
| Another window changes facts during planning | Lock and revalidate parent Publication, Schema, Evidence, and manifest hashes |
| Provider output changes unexpectedly | Persist parser/model/Schema versions and evaluate against the gold corpus |
| New metadata leaks sensitive source content | Store bounded identifiers, hashes, coordinates, and fixed warning codes in audit; keep raw content in governed Evidence storage |

## 10. Definition of Done

- The evaluation corpus and baseline metrics exist.
- Page-level route decisions and parser evidence are persisted or explicitly
  represented by the governed evidence model.
- The closed Schema, asset/business split, and noise rules are versioned and
  covered by tests.
- Automatic extraction cannot activate draft or pending-review facts.
- Active Publications contain only eligible, evidence-backed facts and valid
  relation endpoints.
- Existing Publications and other windows' uncommitted changes are preserved.
- The separate fixed static panorama plan points only to the active Publication
  snapshot contract and does not query live facts.
- GitNexus impact analysis was run before every symbol edit and
  `gitnexus_detect_changes()` passed before any commit.
