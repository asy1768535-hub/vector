# v0.4 Graph Extraction Pipeline Upgrade Summary

## Summary

v0.4 adds a fail-closed, Evidence-rooted graph extraction pipeline on top of the v0.2 Evidence Foundation and v0.3
Graph Relation Foundation. It can plan extraction Jobs and Units, call a strict OpenAI-compatible Provider, persist
auditable Attempts and Candidates, validate against the active ontology, and materialize eligible results as draft graph
facts with active support rows.

The release is accepted against the official DeepSeek API using `deepseek-v4-pro`, one calibration run, a
human-approved immutable policy, and three independent post-freeze Release runs.

## What Changed

### M1: Foundation And Safety

- Added fail-closed global and Library graph extraction controls.
- Added Job, Unit, Context Snapshot and raw Attempt persistence.
- Added graph extractor heartbeat lifecycle.
- Added Migration `0021`.

### M2: Provider Boundary

- Added the strict output parser and deterministic Prompt contract.
- Added the Evidence-aware Context Builder.
- Added OpenAI-compatible Provider and Mock boundaries.
- Added claim-token and lease-fenced Attempt recording.

### M3: Candidate Staging

- Added Entity/Relation Occurrence and Candidate staging.
- Added exact server-owned Evidence Claim resolution.
- Added review/conflict audit rows and formal provenance fields.
- Added Migration `0022`, which remains the release head.

### M4: Deterministic Rules

- Added `normalization_v1` and canonical graph hashes.
- Added deterministic Candidate aggregation and exact Entity matching.
- Added shared ontology/schema validation, conflict construction, confidence calculation and routing.

### M5: Operational Pipeline

- Added production Job/Snapshot creation and deterministic Unit planning.
- Added leased Worker orchestration, retry, full rerun, cancellation and stale recovery.
- Added Job-atomic Materializer behavior.
- Added sanitized v0.4 Job/Unit/Candidate APIs.
- Added post-publication auto trigger, compensation scan and sensitive-payload purge.

### M6: Evaluation And Release Evidence

- Pivoted the production extraction Provider from DashScope to the official DeepSeek API.
- Added fixed synthetic gold, smoke and Release datasets.
- Added isolated PostgreSQL Eval execution and immutable sanitized artifacts.
- Added a human-frozen `eval_policy_v1`, three post-freeze runs and an offline release verifier.
- Added real PostgreSQL Gold and retained-database acceptance.

## Database Upgrade

Upgrade to the current head:

```powershell
alembic upgrade head
alembic heads
```

Expected result:

```text
0022 (head)
```

Migration `0021` creates the extraction execution foundation. Migration `0022` adds staging, Candidate Evidence,
review and audit tables plus nullable formal provenance. There is no Migration `0023` in v0.4.

## Fail-Closed Rollout

The global defaults remain disabled:

```text
GRAPH_EXTRACTION_ENABLED=false
GRAPH_EXTRACTION_AUTO_TRIGGER_ENABLED=false
```

Production rollout should be incremental:

1. Apply migrations and keep both flags disabled.
2. Configure the official DeepSeek base URL/model and an environment-only API key.
3. Enable graph extraction globally while leaving auto trigger disabled.
4. Opt in one Library with external LLM enabled and an explicit security-level allowlist.
5. Start the graph extraction worker and use a manual Job.
6. Inspect Job, Unit, Attempt, Candidate and draft formal results.
7. Enable auto trigger only after cost, rate-limit, retention and incident procedures are accepted.

No source text may leave the system unless all global, Library, security-level and current-ready-revision gates pass.

## Compatibility

v0.4 preserves:

- the old Dify `/retrieval` response shape;
- Qdrant payload and Embedding Worker publication semantics;
- v0.2 revision and Evidence compatibility fields;
- v0.3 ontology, graph fact, Evidence binding and stale lifecycle contracts;
- existing Chat and retrieval behavior.

The only Qdrant-adjacent change is a fail-open call to the graph extraction trigger after the existing revision
publication transaction commits. It does not change payload construction or Qdrant upsert data.

## Materialization Semantics

v0.4 does not publish an active graph. Eligible production Candidates may create:

- draft Entities;
- active Entity Mentions as Evidence support rows;
- draft Relations;
- active Relation Evidence as support rows.

An active Mention or Relation Evidence row is not an active graph publication. Eval Jobs never call the Materializer and
must create zero formal rows.

## Operational Rollback

For an application rollback:

1. Disable auto trigger, then disable graph extraction globally.
2. Cancel queued/processing extraction Jobs.
3. Preserve or export audit evidence before sensitive-payload purge.
4. Continue serving existing retrieval, Chat and v0.3 graph APIs; they do not depend on active v0.4 extraction.

Do not downgrade `0022` or `0021` on a data-bearing system without a backup and a reviewed data-loss plan. The safest
rollback is to leave the schema in place and keep v0.4 flags disabled.

## Deferred Work

v0.4 intentionally excludes active graph version publication, graph retrieval/GraphRAG, review mutation/UI and broad
governance workflows. Active publication belongs to v0.5; the full governance UI remains a later milestone.

The release evidence and exact acceptance results are recorded in
`docs/testing/acceptance/v0.4-graph-extraction-pipeline.md`.
