# v0.7 Publication-Scoped Entity Linking Evaluation

This directory contains the corrective G2.1 evaluation boundary approved by commit
`ff2dd3cc65cdac3a724d2f1806c6afeb7e5cf533` with specification-tree SHA-256
`0da3738946dba65106849d51f94a2126c4f5856b05d1ca5711b05c19d57874d1`.

The committed v2 dataset contains 200 synthetic relation-oriented questions with a fixed 80/120
family-disjoint calibration/release split. Calibration contains 40 safety and 40 utility cases;
release contains 40 safety and 80 utility cases. The reference scorer is pure integer
`lexical-score-v2`. Runtime evaluation requires real PostgreSQL, Qdrant, and `bge-m3` embedding
services; missing live dependencies are a blocked run and cannot be replaced by mocks or skipped
evidence.

The tracked v1 invalidated calibration and the v2 calibration, policy, and ordinal-1 `NO_GO` chain
are immutable audit evidence. They are never policy authority for G2.1. Only the following fresh
calibration identity is authorized:

```text
run_id=v07-el-calibration-v3-20260720-01
database_id=vkt_v07_el_eval_calibration_v3_20260720_01
```

The evaluator implements the complete frozen command surface: dataset/scorer/boundary verification,
live preflight, calibration verification, policy freeze/verification, three post-freeze runs, and
release-evidence assembly/verification. Calibration can inspect only the 80 calibration cases. The
release holdout cannot be scored until a v2 policy is frozen from an explicitly approved v3 calibration.
After calibration is verified and its hashes and selected thresholds are published, execution must stop
for explicit human Gate B approval. Policy, post-freeze, and release artifacts are forbidden before
that approval.
