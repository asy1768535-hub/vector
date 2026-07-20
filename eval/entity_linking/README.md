# v0.7 Publication-Scoped Entity Linking Evaluation

This directory is the evaluation-only G3 boundary approved by commit
`97507e010934c63f18fc06cb3d5098d1dfe28a11` with specification-tree SHA-256
`360b4d6ae30cfa53ec77901436841c0ad48dfa1228c09ddcb112448a0e67d55c`.

The committed dataset contains 200 synthetic relation-oriented questions with a fixed 40/60
family-disjoint calibration/release split. The reference scorer is pure integer `lexical-score-v1`.
Runtime evaluation requires real PostgreSQL, Qdrant, and `bge-m3` embedding services; missing live
dependencies are a blocked run and cannot be replaced by mocks or skipped evidence.

The tracked v1 calibration is immutable invalidated audit evidence. It is never a policy authority.
Only the following replacement calibration identity is authorized:

```text
run_id=v07-el-calibration-v2-20260720-01
database_id=vkt_v07_el_eval_calibration_20260720_01
```

The evaluator implements the complete frozen command surface: dataset/scorer/boundary verification,
live preflight, calibration verification, policy freeze/verification, three post-freeze runs, and release
evidence assembly/verification. After the replacement calibration is verified and its external reference
hashes are published, execution must stop for explicit human policy approval. Policy, post-freeze, and
release artifacts are forbidden before that approval.
