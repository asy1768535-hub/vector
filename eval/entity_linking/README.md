# v0.7 Publication-Scoped Entity Linking Evaluation

This directory contains the corrective G2.7 evaluation boundary approved by commit
`9e2743c44785faf3d3aa10493305e66ef8862354` with specification-tree SHA-256
`ca2aa20067a021e10709a5488fd976f3d8c7bf8d4eab7766044eeceb05f89f17`.

The committed v4 dataset contains 200 synthetic relation-oriented questions with a fixed 80/120
family-disjoint calibration/release split. Calibration contains 40 safety and 40 utility cases;
release contains 40 safety and 80 utility cases. The reference scorer is pure integer
`lexical-score-v2`. Runtime evaluation requires real PostgreSQL, Qdrant, and `bge-m3` embedding
services; missing live dependencies are a blocked run and cannot be replaced by mocks or skipped
evidence.

The tracked v1 invalidated calibration, earlier v2 policy chains, calibration v9, policy v8,
all three post-freeze v9 artifacts, and release-evidence v8 `NO_GO` are immutable audit evidence.
They are never policy authority for G2.7. Only the following fresh calibration identity is authorized:

```text
run_id=v07-el-calibration-v10-20260721-01
database_id=vkt_v07_el_eval_calibration_v10_20260721_01
```

The evaluator implements the complete frozen command surface: dataset/scorer/boundary verification,
live preflight, calibration verification, policy freeze/verification, three post-freeze runs, and
release-evidence assembly/verification. Calibration can inspect only the 80 calibration cases. The
release holdout cannot be scored until a v2-schema policy is frozen from an explicitly approved v10 calibration.
After calibration is verified and its hashes and selected thresholds are published, execution must stop
for explicit human Gate B approval. Policy, post-freeze, and release artifacts are forbidden before
that approval.

The control manifest fixes `candidate_k=50`, exact vector search, stable score/ID ordering,
tie probes at 51/102/201, and fail-closed overflow. Normal product retrieval defaults remain
unchanged. The v4 calibration and release families are disjoint from v2/v3 and each other;
the observed v3 holdout was not used to select scorer behavior, thresholds, families, or gates.
