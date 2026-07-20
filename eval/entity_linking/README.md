# v0.7 Publication-Scoped Entity Linking Evaluation

This directory contains the corrective G2.2 evaluation boundary approved by commit
`de9a4b514eb38156a2db5cf66edbed33b055fba3` with specification-tree SHA-256
`183175a0e8dd33a85f42af42f5a9b7389c935c51452bfef49fa52d1609d54612`.

The committed v2 dataset contains 200 synthetic relation-oriented questions with a fixed 80/120
family-disjoint calibration/release split. Calibration contains 40 safety and 40 utility cases;
release contains 40 safety and 80 utility cases. The reference scorer is pure integer
`lexical-score-v2`. Runtime evaluation requires real PostgreSQL, Qdrant, and `bge-m3` embedding
services; missing live dependencies are a blocked run and cannot be replaced by mocks or skipped
evidence.

The tracked v1 invalidated calibration, the v2 calibration/policy/ordinal-1 `NO_GO`
chain, and calibration v3 are immutable audit evidence. They are never policy authority
for G2.2. Only the following fresh calibration identity is authorized:

```text
run_id=v07-el-calibration-v4-20260720-01
database_id=vkt_v07_el_eval_calibration_v4_20260720_01
```

The evaluator implements the complete frozen command surface: dataset/scorer/boundary verification,
live preflight, calibration verification, policy freeze/verification, three post-freeze runs, and
release-evidence assembly/verification. Calibration can inspect only the 80 calibration cases. The
release holdout cannot be scored until a v2-schema policy is frozen from an explicitly approved v4 calibration.
After calibration is verified and its hashes and selected thresholds are published, execution must stop
for explicit human Gate B approval. Policy, post-freeze, and release artifacts are forbidden before
that approval.
