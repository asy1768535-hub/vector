# v0.2 Evidence Foundation Upgrade Summary

## Summary

v0.2 turns the vector database from revision-aware retrieval infrastructure into
an evidence-based foundation for future graph and answer provenance work. The
release adds document revisions, blocks, evidence units, chunk-to-evidence links,
folder and sync-source APIs, evidence/source read APIs, and an explicit M5
acceptance closure.

## Compatibility Kept

v0.2 intentionally keeps these compatibility fields:

- `documents.current_revision`
- `embedding_jobs.document_revision`
- `Qdrant payload.document_revision`

These fields remain available while the new v0.2 ID-based path rolls out:

- `documents.current_revision_id`
- `documents.latest_revision_id`
- `chunks.document_revision_id`
- `chunks.evidence_id`
- `embedding_jobs.document_revision_id`
- `embedding_jobs.document_revision_no`
- `Qdrant payload.document_revision_id`
- `Qdrant payload.evidence_id`

The old `/retrieval` response remains Dify-style compatible. The response shape
stays `{records:[{content, score, title, metadata}]}`. Evidence fields are
additive metadata keys, so existing Dify clients can ignore them.

## What Changed

M1 added the nullable-first schema and migration foundation for revisions,
blocks, evidence units, chunk links, folders, sync sources, and resumable
backfill state.

M2 moved the write path onto revision/block/evidence generation. New or changed
documents create evidence-backed chunks and embedding jobs; no-op writes remain
idempotent.

M3 made worker publication revision-aware. A revision becomes current only after
Qdrant upsert and compare-and-set publication. Stale workers and superseded
revisions do not enter ordinary retrieval.

M4 added folder movement, sync-source management, sync upsert/delete/batch
behavior, evidence detail reads, and chunk source reads with permission and
current-ready-revision checks.

M5 adds the release manifest, acceptance runbook, compatibility tests, release
notes, and v0.3 evidence handoff proof.

## M5 Release Gates

Before release, run:

- old tests
- new v0.2 tests
- migration dry-run
- rollback dry-run
- real PostgreSQL/Qdrant integration acceptance
- old `/retrieval` Dify compatibility acceptance
- v0.3 handoff check for evidence-based graph tables

The canonical operator steps are in
`docs/30-v0.2-evidence-acceptance-runbook.md`.

## v0.3 Handoff

v0.2 deliberately stops before ontology, entities, relations, graph extraction,
graph retrieval, and graph QA. The important handoff is that future graph tables
can bind directly to evidence anchors:

```text
entity_mentions(evidence_id) -> evidence_units.id
relation_evidence(evidence_id) -> evidence_units.id
```

This lets v0.3 model entity mentions and relation evidence without treating
chunks as the source of truth. Chunks remain retrieval units; evidence units are
the provenance anchors.

## Rollback Note

The v0.2 migration is nullable-first and includes a downgrade path to `0017`.
Rollback dry-runs must be performed only on disposable validation databases.
Once production writes v0.2 evidence data, rollback is an operational decision
that must account for data loss in v0.2-only tables.
