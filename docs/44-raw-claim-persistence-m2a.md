# Phase 2 M2A Raw Claim Persistence

Status: implemented storage layer only. This milestone does not connect a
provider, parser, extraction worker, candidate pipeline, materializer, or
publication path.

## Storage Decision

M2A uses two additive tables:

- `graph_raw_claims` stores the immutable RawClaimV1 content core. Typed nested
  fields and validated stable evidence references are bounded JSONB because M0
  already validates their shape, scope, hashes, and sensitive-key exclusions. A child
  evidence table would add joins and migration surface without improving the
  current locator contract.
- `graph_raw_claim_occurrences` stores one immutable extraction occurrence and
  its full extractor/model/prompt/parser/config/ontology provenance and complete
  scoped evidence refs. Its `claim_id` points to the reusable content core.

The core identity is `(library_id, document_revision_id,
content_scoped_claim_fingerprint)`. The occurrence identities are the supplied
`extraction_occurrence_id` and `extraction_occurrence_fingerprint`; the row
primary key is a separate storage UUID. A same-content rerun may reuse the core
but must create a distinct occurrence when provenance changes. Reusing an
existing occurrence is allowed only when both occurrence identities and all
stored provenance match.

Core evidence refs remove only occurrence-specific `job_id` and
`extraction_unit_id`, attach a stable evidence identity, and are sorted by that
identity. The occurrence row retains the full validated refs, so the core is
reusable without losing job/unit traceability.

## Invariants

- `claim_schema_version` is `raw_claim_v1`; direction is the four-value M0
  contract, including `unknown`.
- The service accepts `RawClaimV1` only and revalidates its serialized value
  before any `flush`. Arbitrary dictionaries, dangling refs, cross-scope refs,
  invalid hashes, unbounded values, and sensitive fields are rejected.
- M0 validates only the internal payload scope and locator contract; it does
  not prove that a referenced database evidence row exists. Before any raw
  claim mutation, M2A performs one batch EvidenceUnit query and one batch Chunk
  query for all references. Each EvidenceUnit must be present, `active`, in the
  same library/document/revision, and have `text_quote_hash` equal to the
  reference `quote_sha256`. A referenced Chunk must be present in the same
  scope, point to the referenced EvidenceUnit, and agree with its block. A
  declared source span must match both EvidenceUnit scalar bounds; if the
  database has no complete bounds, the reference is rejected as unverifiable.
  Missing rows, scope/hash/status/chunk/block/span mismatches fail closed.
- `unit_text_sha256` is independently verified: a `chunk_id` requires the
  SHA-256 of the actual `Chunk.text`; when `unit_id == block_id`, the actual
  `DocumentBlock.text` is required; when there is no chunk and
  `unit_id == evidence_id`, the EvidenceUnit is the minimum unit and its unit
  hash must equal `quote_sha256`, with the stored quote text hash checked too.
  A reference that cannot be classified into one of these verifiable unit
  sources is rejected; no legacy exception is assumed.
- A typed locator `source.text` object is allowed because it is a bounded span;
  document text strings, quote values, raw/normalized file hashes, storage paths, object
  keys, buckets, etags, and secrets are not persisted.
- The service checks that the revision, job, and extraction unit all agree on
  library/document/revision scope. Foreign keys add database-level existence
  and `RESTRICT` delete protection.
- There are no mapping, canonical, publication, status, update, or delete
  fields/APIs. PostgreSQL triggers reject direct UPDATE/DELETE on both tables.
- Candidate payload purge cannot cascade into these rows. Job, unit, revision,
  document, and library deletion is restricted while raw claims remain; a
  future retention policy must explicitly archive or migrate provenance before
  deletion is permitted.

## Transaction Boundary

`create_or_get_raw_claim` performs scope reads, batched evidence validation,
identity checks, core reuse, and occurrence insertion in one caller transaction
and an internal savepoint. A non-unique failure rolls back the new core and
occurrence together, so the caller cannot commit a half-written pair. A unique
constraint race rolls back the savepoint, reloads both identities, and only
reuses rows whose complete immutable payload matches; if a concurrent writer
created the core but not this occurrence, the occurrence is retried in a new
savepoint. The service has no generic mutation method; the caller owns the
final commit/rollback.

## Query Boundary

Read helpers require `library_id` and `document_revision_id` for claim and
occurrence reads. There is no public HTTP route in M2A. Indexes are limited to
library/revision and job/unit access plus the required unique fingerprints.

## Migration And Rollback

Migration `0056` is linear after `0055`, additive, and has no backfill or
production apply. Downgrade drops only the two M2A tables, their indexes,
triggers, and trigger function. The migration does not modify candidate tables,
job payload columns, evidence rows, or existing publication data.

The next shadow-writer milestone may call this service from the extraction
transaction only after deciding its failure isolation and latency budget. It
must not infer raw predicates from canonical relation keys.

## Retention Audit

No current cleanup, candidate purge, materializer, publication, or extraction
worker calls this M2A repository, and no existing hard-delete path was changed
for it. The `RESTRICT` foreign keys intentionally prevent deletion of a job or
extraction unit while an occurrence retains its provenance; candidate payload
purge therefore cannot delete raw claims. A future retention implementation
must explicitly archive or migrate that provenance before permitting hard
delete. This milestone does not add cleanup behavior.
