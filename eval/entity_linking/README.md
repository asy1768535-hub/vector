# v0.7 Publication-Scoped Entity Linking Evaluation

This directory is the evaluation-only G3 boundary approved by commit
`75bf4141be743c1164bfa9841d0737509d7575fe` with specification-tree SHA-256
`b5cde88a6705b1da9053dd36bef46ec2b096b7bd18555b65b5c3ff6fab908075`.

The committed dataset contains 200 synthetic relation-oriented questions with a fixed 40/60
family-disjoint calibration/release split. The reference scorer is pure integer `lexical-score-v1`.
Runtime evaluation requires real PostgreSQL, Qdrant, and `bge-m3` embedding services; missing live
dependencies are a blocked run and cannot be replaced by mocks or skipped evidence.

Only one calibration identity is authorized:

```text
run_id=v07-el-calibration-v1-20260717-01
database_id=vkt_v07_el_eval_calibration_20260717_01
```

After the calibration artifact is verified and its external reference hashes are published, execution
must stop for human policy approval. This directory must not contain `link_policy_v1.json`, post-freeze
results, or release evidence before that approval.
