# Evidence Locator V1 Operational Handoff

This document describes the Phase 1 M5 rollout boundary. It does not change
the publication pipeline and does not authorize a production backfill apply.

## Contract and flags

The runtime reports these bounded versions through `/health` and startup
self-check logs:

- `locator_contract_version`: `EvidenceLocatorV1` (`v1`)
- `parser_unit_contract_version`: `ParserUnitV1` (`v1`)
- `projection_version`: bounded Qdrant locator projection version

The related settings are:

- `ENABLE_EVIDENCE_WRITE_PATH`: additive dual-write for new imports; keep off
  until the target deployment has passed its import acceptance checks.
- `ENABLE_EVIDENCE_LOCATOR_READ`: read the verified locator first and fall back
  to legacy scalar fields when false or when validation fails.
- `ENABLE_EVIDENCE_LOCATOR_PROJECTION`: add the bounded locator projection to
  new embedding payloads. Existing payload fields remain unchanged.

Roll out in this order: deploy contract and dual-read code, verify `/health`,
enable the write path for an isolated library, validate counts and read parity,
then enable projection for new points. Locator reads must remain fallback-safe
while old Qdrant points and legacy rows are present.

## Rollback

For an application rollback, set `ENABLE_EVIDENCE_LOCATOR_READ=false` and
`ENABLE_EVIDENCE_LOCATOR_PROJECTION=false`, restart API and workers, and verify
that legacy scalar source fields and existing Qdrant payload consumers still
serve requests. Keep additive locator JSONB data; do not delete historical
evidence or rewrite an immutable revision.

For a backfill rollback, leave the backfill batch unapplied or use the guarded
marker/hash rollback plan. It may remove only locator data created by the same
tool run when the marker, revision scope, unit identity, and locator hash all
match. It must never perform an unconditional metadata cleanup.

## Monitoring boundaries

Record counts and bounded timings, not source data. At minimum monitor:

- import p50/p95 latency and parser failure rate by parser/version;
- evidence detail and source-window p50/p95 latency, including fallback rate;
- retrieval p50/p95 latency and projection validation rejection count;
- Qdrant payload byte p50/p95/max for the locator projection;
- database write amplification and transaction rollback count for dual-write;
- peak importer/worker memory and bounded OCR/parser unit counts;
- backfill scanned, skipped, updated, failed counts and cursor progress.

Initial acceptance thresholds are operational guardrails, not a schema query
contract: locator projection payloads must remain within the code-defined byte
bound, one revision must be processed in a bounded transaction, and p95
latency/write amplification must be measured against the legacy baseline before
enabling a wider rollout. A regression beyond the deployment's approved
baseline is a reason to disable the read/projection flags and investigate.

## Logging and privacy

Health and startup projections may contain only contract versions, flag state,
bounded counters, error codes, IDs already approved for operational tracing,
and short non-reversible status summaries. Never log document text, evidence
quotes, raw or normalized content hashes, source paths, object keys, bucket
names, ETags, OCR arrays, API keys, or other secrets. Full locator envelopes
remain in the protected database path and are not copied into ordinary health,
retrieval, or federated responses.

## Format support boundary

The current acceptance matrix covers TXT/Markdown, native and OCR-aware PDF
paths, DOCX, XLSX, CSV, and structured JSON. Direct image upload is still
unsupported. XLS is tested as an optional parser capability; production import
dispatch remains unsupported until the dependency and format decision is
approved. The legacy `.xlsx` splitter provenance value is retained for
compatibility and is a separate technical debt item.

## Acceptance and external dependencies

Run the DB-free M5 fixture and M0-M4 regression suite in CI. The real
PostgreSQL/Qdrant acceptance requires disposable services and explicit
`VECTOR_KB_PG_TEST_DSN` and `VECTOR_KB_QDRANT_TEST_URL` settings. If either
service is unavailable, report the automated DB-free result and the external
skip separately; do not substitute SQLite, mocks, or an old product batch.

