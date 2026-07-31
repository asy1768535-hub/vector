# Upload-triggered Graph Extraction

## 1. Scope / Trigger

Use this contract when changing file add/replace controls, upload form fields,
post-embedding graph extraction, or the boundary between extracted drafts and
the graph visible to Q&A.

## 2. Signatures

Configuration endpoint:

```text
GET /libraries/{slug}/v04/graph-extractions/upload-configuration
-> {
     available: boolean,
     default_requested: boolean,
     default_build_mode: "fast" | "standard" | "deep",
     allowed_security_levels: string[],
     reasons: string[]
   }
```

Upload fields:

```text
POST /libraries/{slug}/import-file
graph_extraction_requested: boolean = false
security_level: string | null
```

Persisted revision option:

```json
{"graph_extraction_requested": true}
```

The option lives in `DocumentRevision.parser_config`; it is not an
`EmbeddingJob` column and requires no migration.

## 3. Contracts

- The switch belongs in the file upload area for add, single replace, and
  batch replace flows.
- Library creation exposes one "enable knowledge graph" switch. It persists
  the Library-level graph opt-in, external-model permission, and a non-empty
  security-level allowlist.
- Library creation also selects a Schema template. `enterprise` runs the
  existing idempotent enterprise-Ontology seeder and activates the resulting
  Schema in the same database transaction; `none` creates no Schema.
- Enabling the graph switch defaults the form to `enterprise`, while an
  explicit `none` selection remains allowed and must warn that graph uploads
  stay blocked until a Schema is activated.
- Selecting a Library applies `default_requested` to the upload switch.
  The user can still override that default for the current add or replace.
- When enabled, the client loads configuration for the selected Library,
  selects an allowed security level, and disables submission until the
  response is valid and `available=true`.
- The upload API revalidates the same conditions server-side before accepting
  the extraction request.
- A changed upload persists intent on its target revision. After that revision
  becomes current, the embedder requests graph extraction with `force=true`.
- `force=true` bypasses only the global automatic-trigger switch. Runtime
  enablement, provider credentials, Library graph enablement, external-model
  permission, allowed security level, and an active Ontology remain required.
- An unchanged/deduplicated upload never schedules another extraction.
- Extraction materializes validated draft facts, then the system automatically
  plans and activates a graph Publication. Human review is not a prerequisite.
- Facts that fail validation remain excluded. Errors discovered during search,
  browsing, or Q&A are corrected later through the existing governance and
  versioned Publication workflow.

## 4. Validation & Error Matrix

| Condition | Result |
|---|---|
| Configuration loading or malformed | Upload action disabled |
| Runtime/provider/Library/Ontology unavailable | Fixed reason shown; upload action disabled |
| Requested security level not allowlisted | HTTP `409`, code `graph_extraction_security_level_not_allowed` |
| Server configuration unavailable at upload time | HTTP `409`, code `graph_extraction_unavailable` with reason codes |
| Upload content unchanged | Upload reports unchanged; extraction intent is not persisted |
| Extraction switch off | Existing upload behavior is unchanged |

## 5. Good / Base / Bad Cases

- Good: the user enables extraction, sees an allowed security level and ready
  status, confirms upload, then extraction starts after embedding publishes the
  revision.
- Base: the user leaves extraction off and uploads exactly as before.
- Bad: the client enables upload before configuration returns, trusts only
  client validation, stores intent on a deduplicated revision, or bypasses
  system validation and the versioned Publication boundary.

## 6. Tests Required

- Frontend tests assert the switch location, configuration call, security-level
  forwarding, confirmation, and add/single-replace/batch-replace coverage.
- Service tests assert every availability reason and that forced triggering
  bypasses only the global automatic-trigger switch.
- Upload tests assert intent persistence on the target revision and no intent
  for unchanged uploads.
- Embedder tests assert the persisted option is forwarded as `force=true` only
  after revision Publication.
- Browser QA checks loading-disabled and ready-enabled states, desktop/mobile
  overflow, and console errors.

## 7. Wrong vs Correct

Wrong:

```python
if graph_extraction_requested:
    mark_every_candidate_active_without_validation()
```

Correct:

```python
revision.parser_config = {
    **(revision.parser_config or {}),
    "graph_extraction_requested": True,
}
# The embedder creates an extraction job. Validated facts are materialized and
# automatically published through the normal plan/activate boundary.
```
