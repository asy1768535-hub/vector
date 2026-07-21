# Published Graph Retrieval Evaluation

This directory contains the synthetic, repository-safe v0.6 graph retrieval release dataset.

Frozen inputs:

- `gold_v1.json`: 40 Entities, 215 Relations, 24 Evidence locators, 2 Libraries, 3 Ontologies and 12 property privacy canaries.
- `release_v1.jsonl`: 48 ordered exact-query cases covering direction, two-hop traversal, ambiguity, scope/state failure, truncation and privacy.
- `manifests/release_v1.json`: canonical/file hashes, category counts, request-config hash and the 6,000 Entity + 4,000 Relation performance generator contract.

All JSON and JSONL files are compact, key-sorted, UTF-8 and LF-terminated. The loader recomputes source hashes, counts, references, topology and category coverage. Runtime UUIDs are deterministic UUIDv5 values and are never committed to this directory.

Offline validation:

```powershell
.\.venv\Scripts\python.exe scripts/graph_retrieval_eval.py validate-dataset
```

Database phases require `VECTOR_KB_PG_TEST_DSN`, `--allow-create-drop-eval-db`, an unused `vkt_v06_m5_eval_*` database name and a clean tracked implementation tree. The runner creates, upgrades and drops only the named disposable database.

No `release_policy_v1.json`, post-freeze result or `release_evidence_v1.json` belongs here before explicit Gate B approval. A calibration artifact is evidence for threshold review, not v0.6 release acceptance.
