# EvidenceLocatorV1 Backfill

`scripts/backfill_evidence_locator_v1.py` enriches immutable `ready`
revisions with additive `evidence_locator_v1` envelopes. It does not create or
delete rows, change revision identity, change scalar source fields, or rerun
parsers.

The command is dry-run by default. Use `--apply` only after reviewing the
counts and failure codes:

```text
.\\.venv\\Scripts\\python.exe scripts/backfill_evidence_locator_v1.py \\
  --library-id <uuid> --batch-size 25 --max-revisions 25
```

The output contains bounded counts, a resumable `next_cursor`, and a random
`run_id` for an apply run. Continue with `--cursor <next_cursor>`. A revision
with any conflicting or malformed existing locator is savepointed as failed;
other revisions can still commit.

Each database page is capped at 500 units and a single revision is capped at
10,000 units. A revision over that cap fails with `revision_unit_limit` rather
than causing an unbounded maintenance query.

Only a matching `evidence_locator_v1_backfill` marker, unit/revision identity,
and locator hash can authorize rollback. Rollback is also dry-run by default:

```text
.\\.venv\\Scripts\\python.exe scripts/backfill_evidence_locator_v1.py \\
  --rollback-run-id <run_id> --batch-size 25
```

Add `--apply` only to remove those exact tool-owned locator entries. Other
metadata is preserved. There is no unconditional metadata cleanup command.

The backfill does not add a database index. M0-M3 measurements do not show a
locator filter or lookup workload that justifies an index, and normal retrieval
filtering does not depend on locator JSONB. Reconsider an additive concurrent
index only when production measurements show locator filtering p95 latency,
database CPU, or JSONB scan volume crossing an agreed operational threshold;
record the measurement and index plan before adding a migration.

Logs and CLI output must not include source text, quotes, raw file hashes,
storage paths, object keys, or secrets.
