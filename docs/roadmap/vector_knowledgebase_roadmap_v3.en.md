# Multi-Department Knowledge Retrieval Infrastructure Roadmap v3

> Project: `vectorDatabase`
>
> Audience: implementation agents and engineering reviewers
>
> Chinese product roadmap: `docs/roadmap/vector_knowledgebase_roadmap_v3.zh-CN.md`
>
> English execution extension version: 2026-07-29
> Scope in this file: the structured document parsing and quality-governance program in Chinese Section 12, plus the admin UI reliability and code-governance program in Chinese Section 13.

---

## 1. Authority And Synchronization

This document is the agent-facing execution counterpart of Chinese Roadmap Sections 12 and 13.

The milestone IDs `SDP-M1` through `SDP-M8` and `UIR-M1` through `UIR-M4`, their dependency order, acceptance boundaries, and non-goals must remain synchronized between both files.

Interpretation rules:

1. The Chinese roadmap owns product intent, user-visible behavior, and prioritization.
2. This English document owns implementation constraints, invariants, task boundaries, and verification expectations.
3. An agent must not silently resolve a material conflict. Update both documents in one planning change or stop and report the conflict.
4. Historical accepted behavior remains authoritative unless a milestone explicitly replaces it.
5. Do not implement multiple milestones in one task unless the user explicitly approves that scope after impact analysis.

## 2. Current Repository Ground Truth

The repository already has:

- `Document -> DocumentRevision -> DocumentBlock -> Chunk -> EvidenceUnit`.
- Immutable revision-oriented evidence and source-location contracts.
- `Document.current_revision_id` and `Document.latest_revision_id`.
- `DocumentBlock.parent_block_id`, `block_kind`, `content`, `position`, `parser_name`, and `parser_version`.
- Page-aware PDF text extraction plus bounded OCR fallback.
- DOCX paragraph, heading, table, and image OCR extraction.
- XLSX sheet, row-number, and table-aware chunk generation.
- Exact content-hash deduplication and external-ID business identity.
- Dense retrieval, keyword retrieval, RRF fusion, and reranking.
- Versioned Summary and Outline knowledge artifacts.
- Graph extraction bound to server-owned revision, block, chunk, evidence, and source spans.

The main implementation mismatch is:

```text
structured parser output
  -> generic chunk dictionaries
  -> create_evidence_generation()
  -> block_kind = "paragraph"
  -> chunk_kind = "text"
  -> parser = "legacy/v0.2-m2"
```

Therefore the schema foundation exists, while production persistence discards part of the structure.

## 3. Program Invariants

These invariants apply to every milestone.

### 3.1 Structured Source Is Authoritative

Markdown and flattened text are projections for display, debugging, or retrieval. They are not the sole canonical parse representation.

### 3.2 Fail Closed Before Downstream Processing

A revision that fails parse-quality policy must not create embedding, artifact, classification, or graph-extraction work.

### 3.3 No Unowned Human Review Queue

Production parse routing may produce only:

- `accepted`
- `degraded`
- `rejected`

There is no `needs_review` state unless a staffed and bounded review workflow is separately approved.

### 3.4 Revision Immutability

Parser identity, parser configuration, parse blocks, quality report, chunk projections, and evidence belong to one immutable revision. Reparse creates another revision.

### 3.5 Evidence Fidelity

Repeated retrieval context, such as a table header, must not change the evidence span into synthetic source text. Evidence must still point to the actual source region.

### 3.6 Parser Names Are Not Quality Proof

Docling, PyMuPDF, pypdf, OCR engines, and future parsers must be compared against the same gold corpus. No parser is accepted based on popularity or anecdotal reports.

### 3.7 Compatibility Is Mandatory

The work must preserve:

- revision visibility and publication fencing;
- Dify and public retrieval response contracts;
- existing chunk and evidence reads;
- graph evidence resolution;
- cleanup, retention, and coordinated purge;
- organization and library authorization;
- old `legacy/v0.2-m2` revision readability.

## 4. Target Pipeline

```text
immutable source snapshot
  -> file and security inspection
  -> deterministic Parser Router
  -> ParsedDocumentV1
       -> ParsedBlockV1[]
       -> parser diagnostics
  -> ParseQualityReportV1
  -> frozen parse-quality policy
       -> accepted
       -> degraded
       -> rejected
  -> DocumentRevision + DocumentBlock
  -> structure-aware Chunk + EvidenceUnit
  -> embedding / summary / outline / classification / graph
  -> dense + keyword + RRF + reranker
```

No downstream job may race ahead of the parse-quality decision.

## 5. Canonical Contracts

### 5.1 ParsedDocumentV1

Required logical fields:

```text
contract_version
parser_name
parser_version
parser_config_hash
source_file_hash
mime_type
page_count
blocks
diagnostics
```

Requirements:

- strict schema with extra fields rejected;
- bounded strings and arrays;
- lowercase SHA-256 identities where hashes are used;
- deterministic canonical JSON and hash;
- no source text in audit-only projections;
- diagnostics separated into safe reason codes and private bounded detail.

### 5.2 ParsedBlockV1

Required logical fields:

```text
block_id
parent_block_id
sequence
block_kind
raw_block_type
text
structured_content
title_path
page_start
page_end
source_start
source_end
position
bbox
parse_confidence
attributes
```

Initial closed `block_kind` vocabulary:

```text
document_title
heading
paragraph
list
table
figure
caption
formula
page_header
page_footer
footnote
unknown
```

Validation rules:

- block IDs are unique and deterministic within a revision;
- parent references remain in the same revision;
- the parent graph is acyclic;
- sequence is deterministic and preserves the selected reading order;
- page and source ranges are ordered and bounded;
- coordinates and confidence values are finite;
- `structured_content` follows a closed shape per block kind;
- plain `text` is a projection and cannot replace table cells or other structure;
- parse confidence describes extraction confidence, not factual truth.

### 5.3 ParseQualityReportV1

Required logical fields:

```text
contract_version
parser_route
fallback_chain
total_page_count
text_page_count
ocr_page_count
text_coverage
block_counts
table_count
figure_count
formula_count
unknown_block_count
unparsed_page_count
reading_order_confidence
source_location_coverage
low_confidence_block_count
warning_codes
hard_failure_codes
quality_score
quality_status
policy_version
input_fingerprint
```

The report is immutable and bound to:

- library;
- document;
- revision;
- source file hash;
- parser/router/config identity;
- policy version.

## 6. Quality Policy

### 6.1 Status Semantics

| Status | Meaning | Downstream behavior |
|---|---|---|
| `accepted` | All mandatory gates pass | Downstream jobs may be created |
| `degraded` | Non-fatal loss exists but frozen minimums pass | Downstream behavior follows the frozen policy and carries degradation metadata |
| `rejected` | Usability cannot be established | No downstream jobs |

### 6.2 Mandatory Rejection Conditions

Reject automatically when any of these applies:

- corrupt or unreadable encrypted input;
- no usable text after required OCR;
- malformed block hierarchy or cross-revision parent;
- invalid, non-finite, or out-of-range coordinates/confidence;
- unsupported structured-content shape;
- required source locations cannot be established;
- unparsed critical pages exceed frozen limits;
- parser resource or safety limits are exceeded;
- stored parser output does not match its canonical fingerprint.

### 6.3 Calibration

Do not invent a production quality-score threshold before measuring the gold corpus.

The process is:

1. collect per-format metric distributions;
2. identify hard invariants independent of score;
3. calibrate candidate thresholds;
4. freeze `parse-quality-policy-v1`;
5. bind dataset, parser, config, evaluator, and policy hashes;
6. execute three independent post-freeze runs;
7. accept or reject the release.

## 7. Parser Router

The router is deterministic and versioned.

| Input | Primary adapter | Controlled fallback |
|---|---|---|
| TXT/Markdown | native text | reject invalid encoding |
| JSON/CSV | structured record adapter | reject strict parse failure |
| native PDF | layout-aware PDF adapter | current page-level pypdf adapter |
| scanned PDF | bounded render plus OCR | reject unavailable/over-limit OCR |
| DOCX | OOXML order, headings, tables, images | controlled flat-text compatibility path |
| XLSX | sheets, regions, merged cells, header hierarchy | current first-header-row path |
| PPTX | slide, shape, table, notes adapter | explicitly unsupported before `SDP-M8` |

Persist:

```text
router_version
selected_parser
selected_parser_version
fallbacks_attempted
parser_config_hash
reason_codes
```

Do not run every parser and ask an LLM to select an output. Multi-parser comparison is allowed only in bounded evaluation or in a deterministic fallback policy justified by gold results.

## 8. Persistence And Projection

### 8.1 Write Path

Replace or extend `PreparedChunk` with a strict block-aware input contract.

`create_evidence_generation()` must stop inventing:

```text
block_kind = paragraph
chunk_kind = text
parser = legacy/v0.2-m2
```

for revisions produced by a structured adapter.

### 8.2 Mapping Rules

- one parsed block may produce zero, one, or multiple chunks;
- one chunk may reference multiple blocks through `chunk_blocks`;
- one evidence unit remains bound to server-owned source;
- chunk metadata may repeat context but evidence spans remain source-accurate;
- parser identity and quality status propagate to safe chunk/Qdrant metadata;
- old legacy revisions retain their original identity.

### 8.3 Migration Strategy

- add nullable or safely defaulted storage first;
- backfill only facts that are actually known;
- never label legacy rows as output from a new parser;
- tighten constraints only after backfill verification;
- reparse by creating a new revision;
- rollback by restoring the prior current revision, not deleting diagnostics.

## 9. Parsing Evaluation

Create:

```text
eval/document_parsing/
  datasets/
  fixtures/
  gold/
  policies/
  results/
  scripts/
```

Initial gold categories:

- native single-column PDF;
- multi-column technical PDF;
- scanned PDF;
- mixed native/scanned PDF;
- cross-page tables;
- merged cells and repeated headers;
- headers, footers, footnotes, and numbered lists;
- figures and captions;
- formulas;
- DOCX headings, tables, text boxes, and images;
- XLSX multiple sheets, multi-level headers, hidden rows/columns, and merged cells;
- corrupt, encrypted, blank, and invalid-encoding files.

Required metrics:

```text
text completeness
heading hierarchy accuracy
block type macro F1
table structure accuracy
reading order accuracy
page localization accuracy
source span exact and overlap accuracy
OCR character/word error rate
parse rejection correctness
downstream retrieval Hit@K delta
```

Synthetic unit tests are necessary but are not sufficient release evidence.

## 10. Source Authority And Document Quality

Do not create a human review queue.

Separate:

1. system-computed parse quality;
2. source authority asserted by trusted ingestion configuration or validated business metadata.

Candidate fields:

```text
source_authority
publication_status
effective_date
expiration_date
is_official
quality_policy_version
```

Rules:

- authority does not rescue an irrelevant result;
- expired, rejected, or disallowed draft content is excluded before ranking;
- business boosts remain small, explainable, auditable, configurable, and evaluated;
- untrusted user metadata cannot assign itself higher authority.

## 11. Duplicate And Version Families

Preserve current exact identity:

- with external ID: business-identity upsert;
- without external ID: exact content-hash deduplication.

Add candidate-only signals:

```text
normalized_text_hash
source_external_id
supersedes_revision_id
duplicate_group_id
near_duplicate_score
duplicate_reason
```

Near-duplicate detection may group or collapse search display, but must not automatically delete, overwrite, merge evidence, or change business identity.

## 12. Hierarchical Retrieval

This is an experiment after parser quality is stable.

```text
query
  -> document/summary candidates
  -> section/heading candidates
  -> scoped chunk candidates
  -> dense + keyword + RRF
  -> reranker
  -> evidence, revision, publication, and visibility checks
```

Entry criteria:

- a retained evaluation set demonstrates failures for whole-document summary, cross-section comparison, or document-scoped discovery;
- Summary and Outline artifacts are fully revision and permission scoped;
- A/B results improve target metrics without material regression on ordinary factual retrieval.

Do not replace the current hybrid path and do not index summaries only.

## 13. Multimodal Boundary

Before `SDP-M8`, the program covers structure fidelity and OCR, not general visual understanding.

Later candidates:

- figure-caption-body relationships;
- chart data and visual question answering;
- formula source, LaTeX projection, and location;
- PPTX slide, shape, table, and speaker-note parsing.

Entry requires a real gold use case plus privacy, latency, cost, revision, authorization, retention, and deletion controls.

## 14. Milestone Plan

### SDP-M1: Contracts And Baseline Inventory

Scope:

- strict schemas for `ParsedDocumentV1`, `ParsedBlockV1`, and `ParseQualityReportV1`;
- canonical hashing and bounded safe diagnostics;
- current-parser capability/loss matrix;
- annotation guide;
- initial gold fixture manifest;
- evaluator CLI skeleton.

Likely code areas:

```text
app/schemas/
app/services/parser contracts
eval/document_parsing/
tests/parser contract tests
```

Must not change the production write path.

Acceptance:

- strict extra-field rejection;
- deterministic IDs/hashes;
- invalid hierarchy/ranges fail closed;
- fixtures and empty evaluator run reproducibly;
- current output inventory is documented.

### SDP-M2: Structured Persistence

Scope:

- adapters emit strict parsed-block DTOs;
- persist real block kind, hierarchy, content, position, and parser identity;
- structure-aware chunk projection;
- compatibility reads/backfill;
- no old-revision semantic rewrite.

Primary impact surfaces:

```text
app/services/ingest.py
app/services/evidence_write_path.py
app/services/splitter.py
app/models/document_block.py
app/models/chunk.py
app/models/chunk_links.py
app/models/evidence_unit.py
embedding worker and Qdrant payload projection
```

Acceptance:

- PDF, DOCX, and XLSX structure is inspectable in PostgreSQL;
- evidence/source location does not regress;
- Dify and retrieval response contracts remain compatible;
- legacy revisions remain readable;
- all direct GitNexus dependents are tested.

### SDP-M3: Quality Report And Automatic Gate

Scope:

- immutable revision-level quality-report persistence;
- frozen policy snapshot;
- gate before downstream job creation;
- safe diagnostics API and operational UI;
- retry/reparse semantics.

Acceptance:

- rejected parse creates no downstream job;
- degraded behavior is policy-bound;
- no pending human-review queue;
- failures retain safe reasons and private bounded diagnostics;
- retries create or target the correct revision without mutating accepted history.

### SDP-M4: Router And Format Enhancements

Scope:

- native/scanned/complex PDF routing;
- DOCX lists, headings, tables, images, captions, and text boxes;
- XLSX multi-level headers, merged cells, and region detection;
- optional third-party parser only after benchmark evidence.

Acceptance:

- every route and fallback is persisted and replayable;
- time, memory, page, archive, and OCR bounds are enforced;
- fallback does not silently lower mandatory quality;
- per-format gold metrics improve or remain within accepted regression limits.

### SDP-M5: Frozen Evaluation And Release Gate

Scope:

- complete evaluator and gold corpus;
- calibration artifacts;
- frozen dataset/config/parser/evaluator/policy identities;
- three independent post-freeze runs;
- parser release evidence.

Acceptance:

- reproducible metrics and hashes;
- accepted baseline comparison;
- parse, retrieval, and evidence metrics reported together;
- no release on synthetic tests alone.

### SDP-M6: Version Family And Retrieval Deduplication

Scope:

- normalized hashes and near-duplicate candidates;
- duplicate/version-family records;
- non-destructive search collapse;
- explainable reason and score.

Acceptance:

- exact identity behavior is unchanged;
- no automatic merge/delete;
- false grouping rate satisfies frozen policy;
- every collapsed result retains source and version access.

### SDP-M7: Hierarchical Retrieval Experiment

Scope:

- document/summary, section, and chunk candidate stages;
- permission and revision fencing at every stage;
- feature-flagged fallback to current hybrid retrieval;
- retained-set A/B evaluation.

Acceptance:

- target query categories improve;
- ordinary factual retrieval has no material regression;
- disabling the feature restores byte-compatible current behavior where contracted;
- diagnostics identify candidate stage and scores.

### SDP-M8: Bounded Multimodal Pilot

Scope:

- exactly one business use case with real gold data;
- image/chart/formula assets bound to revision and evidence;
- local versus controlled external model evaluation;
- privacy and deletion design.

Acceptance:

- measured user value;
- approved privacy and cost envelope;
- authorization and deletion consistency;
- no broad rollout before acceptance.

## 15. Dependency Graph

```text
SDP-M1 -> SDP-M2 -> SDP-M3 -> SDP-M4 -> SDP-M5
                              |
                              +-> SDP-M6
SDP-M5 + SDP-M6 -> SDP-M7
SDP-M5 -> SDP-M8
```

Only `SDP-M1` is authorized as the next implementation planning target.

## 16. Verification Requirements

Every implementation milestone must include:

- GitNexus impact before editing each function/class/method;
- warning and explicit review for HIGH/CRITICAL impact;
- strict schema tests;
- service tests;
- PostgreSQL migration/integration tests where persistence changes;
- Qdrant compatibility tests where payload changes;
- API contract tests where responses change;
- evidence and revision regression tests;
- Ruff, compileall, import smoke, `git diff --check`;
- `gitnexus_detect_changes()` before commit;
- parser evaluation commands and retained result artifacts after `SDP-M5`.

PostgreSQL async engines and pools must be created, used, and disposed in one event loop.

## 17. Security And Resource Limits

Required controls:

- no source content, secrets, local paths, or raw provider responses in public errors/audit targets;
- archive file-count, expanded-size, depth, and compression-ratio limits;
- PDF page, pixel, DPI, time, and memory limits;
- OCR page and runtime limits;
- third-party parser process/container isolation;
- bounded parser output and diagnostics;
- organization/library authorization for source, blocks, OCR text, reports, and evaluation artifacts;
- retention and coordinated purge integration.

## 18. Explicit Non-Goals

- no immediate embedding, Qdrant, RRF, or reranker replacement;
- no run-all-parsers architecture;
- no LLM reconstruction of missing cells, coordinates, or evidence;
- no unstaffed human review queue;
- no embedding/reranker fine-tuning before a gold baseline;
- no summary-only index;
- no combined parser, near-duplicate, hierarchical, and multimodal mega-milestone;
- no in-place rewriting of historical revisions;
- no parser-engine adoption based only on forum recommendations.

## 19. Risk Register

| Risk | Control |
|---|---|
| Structured DTO becomes an unbounded generic JSON bag | Closed contracts and per-kind structured-content schemas |
| New parser silently changes evidence offsets | Exact/overlap source-span evaluation and revision isolation |
| Quality score hides hard failures | Hard gates evaluated before aggregate score |
| New fields break Qdrant filters or old payloads | Additive payload versioning and compatibility tests |
| Third-party parser consumes excessive resources | Isolation, limits, timeout, and kill behavior |
| Near-duplicate grouping merges distinct business docs | Candidate-only grouping, no automatic merge |
| Hierarchical retrieval hurts simple questions | Retained A/B set and complete feature fallback |
| OCR produces plausible but wrong text | OCR confidence/coverage diagnostics and strict degraded policy |
| Reparse overwrites accepted evidence | New immutable revision plus publication switch |

## 20. Next Authorized Deliverables

Start only `SDP-M1` after separate implementation approval.

Deliver:

1. strict contract schemas and examples;
2. current parser capability/loss matrix;
3. gold annotation guide;
4. minimal evaluator CLI;
5. initial real fixture manifest;
6. GitNexus impact report for `SDP-M2`;
7. synchronized updates to both roadmap files if implementation discoveries change scope.

Do not modify the production parse/write path during `SDP-M1`.

---

## 21. Admin UI Reliability Program Authority

This section is the implementation-agent plan for Chinese Roadmap Section 13.

The user-visible decisions are already frozen:

- authenticated entry and the root route resolve to Intelligent Chat;
- desktop PC behavior is in scope, while mobile and narrow-screen adaptation are not;
- graph UI state must follow actual feature, data, and request outcomes;
- read paths need distinct loading, empty, partial-error, fatal-error, and refresh states;
- write-action verification proceeds page by page only after user confirmation;
- `myPermissions`, `listMyOrganizations`, `getGraphGovernanceAction`, and `getGraphPublication` remain in `admin-ui/src/api.js`;
- component extraction must preserve API contracts, permissions, fields, and user-visible behavior.

`UIR` is independent from `SDP`. Do not combine milestones from both programs in one implementation task.

## 22. Current UI Ground Truth

The current audited baseline is:

- the authenticated root route can render the layout and sidebar with an empty main region;
- direct leaf-route navigation works;
- product documentation, default-route implementation, and route tests do not currently express one default-home contract;
- the graph workspace can show an unavailable/nonexistent warning while rendering entity data;
- sampled primary page fields align with their FastAPI response schemas;
- major command buttons are wired to API wrappers, but real mutation verification is intentionally deferred to page-by-page review;
- the four retained API wrappers have no current production callers, while their backend endpoints remain valid;
- `GraphGovernance.js`, `Import.js`, `SchemaLifecycle.js`, `KnowledgeCatalog.js`, `Chat.js`, and `Documents.js` are the primary decomposition candidates;
- the current frontend test baseline is 609 passing tests, but static source assertions do not prove browser runtime behavior.

Treat the dirty worktree as user-owned. Do not revert, normalize, or reformat unrelated changes.

## 23. UI State Contract

### 23.1 Read State

Every migrated read surface must project explicit state:

```text
idle
loading
success-with-data
success-empty
partial-error
fatal-error
refreshing
```

Rules:

- never render empty state before a successful empty response;
- never convert request failure to an empty array merely to simplify rendering;
- preserve valid stale data when a refresh fails and label the refresh failure;
- identify primary versus auxiliary requests on multi-request pages;
- auxiliary failure must not hide valid primary data;
- retry must invoke the real read path and reject duplicate concurrent retry;
- route/library/scope changes must fence stale responses;
- public UI errors must not expose stack traces, secrets, local paths, or raw provider responses.

### 23.2 Mutation State

Mutation surfaces eventually require:

```text
submitting
success
failure
conflict
```

Do not broaden a milestone to mutation verification unless the user has confirmed that page. Existing mutation behavior remains frozen otherwise.

### 23.3 Graph Projection

The graph workspace must use this truth table:

| Feature/data/request outcome | Required projection |
|---|---|
| Main request succeeds with entities or relations | Render graph data; no unavailable warning |
| Main request succeeds with no entities and no relations | Do not mount an empty graph canvas; render empty state |
| Feature is explicitly disabled | Do not mount graph canvas; render disabled state |
| Main request fails | Render fatal read error with retry; do not claim empty data |
| Auxiliary status request fails while main data succeeds | Keep graph data and render bounded partial-status warning |

Do not infer disabled or empty state from default-initialized client values.

## 24. Component Boundary Rules

- Freeze behavior with tests before moving code.
- Prefer pure projections, request coordinators, and domain-specific components over generic wrappers.
- Keep the page view as composition and route-scope ownership.
- Do not create a new global state framework.
- Do not perform a global CSS redesign.
- Do not combine extraction of all large pages in one milestone.
- Line count is diagnostic only, not an acceptance metric.
- Before deleting code, check direct imports, namespace/dynamic access, tests, preview fixtures, documentation, and compatibility commitments.
- The four explicitly retained API wrappers are outside dead-code removal scope.
- Backend route deletion requires a separate deprecation plan and is never implied by frontend cleanup.

## 25. Milestone Plans

### UIR-M1: Entry Route And State Inventory

Goal:

Make Intelligent Chat the single authoritative authenticated entry and produce the state inventory required by `UIR-M2`.

Primary files:

```text
admin-ui/src/app.js
admin-ui/src/domain_navigation.js
admin-ui/src/views/Layout.js
admin-ui/domain_navigation.test.mjs
admin-ui/frontend_audit.test.mjs
docs/12-admin-ui.md
```

Required work:

1. Run GitNexus upstream impact for every function or method to be modified.
2. Add a failing behavioral route test that reproduces the blank root main region.
3. Make root navigation and post-auth default navigation resolve to `knowledge-use/chat`.
4. Preserve permission filtering for all non-default routes.
5. Synchronize route tests and `docs/12-admin-ui.md`.
6. Inventory each routed page for loading, empty, fatal error, partial error, refresh, and retry behavior.
7. Record the inventory for `UIR-M2` at
   `docs/roadmap/admin-ui-reliability/uir-m1-async-state-inventory.en.md`.

Non-goals:

- no mobile CSS;
- no graph-state rewrite;
- no large-component extraction;
- no mutation behavior changes;
- no deletion of the four retained API wrappers.

Acceptance:

- direct authenticated root load renders Intelligent Chat;
- post-login navigation renders Intelligent Chat;
- root refresh does not leave main content empty;
- unauthorized routes remain inaccessible;
- navigation documentation, implementation, and tests agree;
- the async-state inventory covers every route.

### UIR-M2: Read-State Reliability

Goal:

Establish one explicit read-state projection pattern and migrate pages incrementally.

Planning rule:

Start with one simple list page and one multi-request page selected from the `UIR-M1` inventory. Do not migrate every page in one patch.

Status on 2026-07-29:

- Batch A is complete for `Users.js` and `Dashboard.js`.
- Implementation evidence is recorded at
  `docs/roadmap/admin-ui-reliability/uir-m2-batch-a-evidence.en.md`.
- Batch B implementation and automated verification are complete for `Search.js`,
  `Libraries.js`, and `ApiKeys.js`.
- Batch B evidence is recorded at
  `docs/roadmap/admin-ui-reliability/uir-m2-batch-b-evidence.en.md`.
- Library Management and API Keys passed PC refresh smoke checks. The Search
  failure-state browser scenario remains pending because concurrent stale preview
  listeners on port `5599` served an older module; rerun it with one clean listener
  before closing the Batch B browser gate.

Required work:

1. Add pure state-projection helpers only where they remove repeated ambiguity.
2. Preserve valid previous data during refresh.
3. Separate successful empty data from request failure.
4. Separate primary request failure from auxiliary request failure.
5. Add real retry wiring to the corresponding read function.
6. Fence stale route, organization, library, tab, and filter responses.
7. Add behavior tests for initial loading, successful empty, fatal failure, partial failure, refresh failure, retry, duplicate retry, and stale response.
8. Continue page batches only after each prior batch passes browser verification.

Likely surfaces:

```text
admin-ui/src/views/*.js
admin-ui/src/*_ui.js
admin-ui/style.css
admin-ui/*.test.mjs
```

Non-goals:

- no write-operation redesign or broad mutation verification;
- no backend response relaxation;
- no error-to-empty fallback;
- no mobile work;
- no deletion of retained API wrappers.

Acceptance:

- migrated pages cannot display empty state during initial loading;
- fatal errors remain distinguishable from empty results;
- partial failure preserves valid primary data;
- retry performs a real request;
- stale responses cannot overwrite current scope;
- selected pages pass PC browser verification and relevant Node tests.

### UIR-M3: Graph Truthfulness And Governance Decomposition

Goal:

Make graph rendering match actual backend outcomes and reduce `GraphGovernance.js` along domain boundaries without changing governance behavior.

Required pre-edit analysis:

- GitNexus context and upstream impact for every changed function/method;
- explicit warning before any HIGH or CRITICAL edit;
- route/schema review for graph catalog, governance context/actions, publications, and published graph reads;
- characterization tests for the current successful governance and publication flows.

Target boundaries:

```text
page/scope orchestration
graph browsing
entity operations
relation operations
review queues
publication management
request/state projection
pure labels and validators
```

Required work:

1. Implement the Section 23.3 graph truth table.
2. Prevent auxiliary status failure from hiding valid entities or relations.
3. Avoid mounting graph canvas when a successful result is truly empty.
4. Provide real retry for failed primary reads.
5. Extract one domain boundary at a time with tests before the next extraction.
6. Preserve current permissions, request bodies, strict response checks, conflict handling, and stale-response fencing.

Non-goals:

- no graph backend redesign;
- no ontology or publication contract change;
- no visual redesign unrelated to state correctness;
- no mobile adaptation;
- no removal of `getGraphGovernanceAction` or `getGraphPublication`.

Acceptance:

- no unavailable/nonexistent warning appears alongside valid graph data;
- disabled, empty, fatal, and partial states are distinct;
- graph governance and publication commands preserve current contracts;
- decomposed modules have clear single-domain ownership;
- graph tests and PC browser verification pass.

### UIR-M4: Remaining Large-Page Governance And Safe Cleanup

Goal:

Decompose remaining high-risk page modules in user-confirmed order and remove only proven obsolete frontend code.

Candidate order:

```text
Import.js
SchemaLifecycle.js
KnowledgeCatalog.js
Chat.js
Documents.js
```

The actual order follows user page review and risk, not file size alone.

Per-page procedure:

1. Capture current route, API, permission, field, read-state, and confirmed mutation behavior.
2. Run GitNexus context and upstream impact for edited symbols.
3. Add characterization tests for behavior being preserved.
4. Extract pure transforms and request/state coordination first.
5. Extract domain UI components only when ownership is clear.
6. Verify displayed fields against FastAPI schemas.
7. Verify only user-confirmed mutations against a real backend or deterministic request interception.
8. Run static, test, preview, documentation, and compatibility reference scans before deleting code.

Protected exports:

```text
myPermissions
listMyOrganizations
getGraphGovernanceAction
getGraphPublication
```

These remain even if production-reference scans return zero.

Acceptance:

- each migrated page has explicit module ownership and read-state behavior;
- no confirmed valid display or operation is lost;
- no supported backend endpoint is removed;
- deletion evidence is recorded for every removed export or module;
- related Node tests and PC browser scenarios pass.

## 26. UIR Dependency And Authorization

```text
UIR-M1 -> UIR-M2 -> UIR-M3 -> UIR-M4
```

`UIR-M1`, `UIR-M2` Batch A, and Batch B implementation are complete. Finish the recorded Batch B Search browser follow-up, then continue with Batch C. Do not start `UIR-M3` until `UIR-M2` verification is complete.

`SDP-M1` remains the next structured-document target. `UIR-M1` and `SDP-M1` may progress as separate tasks, but must not share one implementation patch.

## 27. UIR Verification Gate

Every `UIR` milestone must include:

- GitNexus upstream impact before editing each function/class/method;
- HIGH/CRITICAL warning before edit;
- behavior tests rather than source-string assertions alone;
- relevant `admin-ui` Node tests;
- PC viewport browser verification;
- browser console and unhandled-promise inspection;
- FastAPI route and response-schema comparison for touched API surfaces;
- explicit stale-response and retry verification where reads change;
- `git diff --check`;
- `gitnexus_detect_changes()` before commit.

Do not require mobile screenshots or responsive-layout acceptance.

Mutation integration is added only for pages explicitly confirmed by the user.

## 28. UIR Explicit Non-Goals

- no mobile or narrow-screen adaptation;
- no deletion of the four protected API wrappers;
- no whole-admin rewrite;
- no Vue, Element Plus, or zero-build architecture replacement;
- no hidden conversion of network, authorization, or server errors to empty results;
- no batch mutation redesign before page-level confirmation;
- no backend route removal;
- no unrelated visual restyling.
