# P1 Canonical Entity Final Acceptance

验证阶段：P1.5（acceptance / verification / seal）
最终状态：`COMPLETED_WITH_KNOWN_LIMITATIONS`

P1 correctness acceptance 已通过。唯一未运行的能力是 PostgreSQL runtime integration：当前环境没有 `VECTOR_KB_PG_TEST_DSN`。这不阻塞 P1；缺口单独记录为 runtime verification gap。P2 仍未授权。

## Acceptance Matrix

| 范围 | 结果 | 证据边界 |
| --- | --- | --- |
| A1 Canonical identity exists | PASS | resolver/materializer unit tests；legacy/unresolved `NULL` 兼容 |
| A2 Same extraction replay | PASS | resolver replay fingerprint/idempotency tests |
| A3 Existing projection across documents | PASS | existing mapping across revision/job lineage + materializer tests |
| A4 Source deletion preserves identity | PASS | stale lifecycle tests；Mention stale，Entity/CanonicalEntity 保留 |
| A5 Relationless entity | PASS | materializer entity-only path；无 relation 不会令 Entity unresolved |
| A6 Cross-library isolation | PASS | resolver scope rejection + composite-FK static contract |
| A7 Name-only homonym safety | PASS | same-name ambiguity stays `pending_review` |
| A8 Alias-only safety | PASS | alias exact hit remains weak signal / `pending_review` |
| A9 Existing confirmed projection anchor | PASS | existing `Entity -> CanonicalEntity` deterministic reuse |
| A10 Canonical mapping conflict | PASS | conflict remains pending；不静默覆盖原 mapping |
| B1 V1/V2 projection identity | PASS | nullable scoped mapping/model and lifecycle tests |
| B2 Cross-ontology name-only boundary | PASS | same name without strong mapping stays `pending_review` |
| B3 Ontology switch | PASS | publication reconcile uses bound ontology；current switch 不迁移历史 publication |
| Decision audit / replay | PASS | append-only active/superseded decision tests；A -> B -> A replay returns current active |
| Candidate ordinary purge | PASS | candidate row retained；payload/sensitive fields scrubbed；Decision/CanonicalEntity untouched |
| Candidate physical-delete FK | PASS (schema/static) | `fk_entity_resolution_decisions_candidate` is `ON DELETE SET NULL` |
| Candidate physical-delete runtime | SKIPPED — PostgreSQL runtime integration unavailable | 未配置 `VECTOR_KB_PG_TEST_DSN` |
| Concurrency implementation contract | PASS | lock key is `library_id + subject_fingerprint`; no library-row lock in resolver path |
| PostgreSQL concurrency runtime | SKIPPED — PostgreSQL runtime integration unavailable | 未配置 `VECTOR_KB_PG_TEST_DSN` |
| CREATE NEW transaction ownership / unit rollback path | PASS | resolver caller-owned transaction + materializer transaction tests |
| PostgreSQL transaction rollback runtime | SKIPPED — PostgreSQL runtime integration unavailable | 未配置 `VECTOR_KB_PG_TEST_DSN` |
| Pending / Reject | PASS | no canonical mapping/entity/mention materialized for these outcomes |
| Publication / retrieval / chat boundary | PASS | publication items remain `Entity.id` / relation based and ontology-bound |
| Projection-level merge | PASS | same-canonical merge allowed; different canonical IDs fail closed; no Canonical Merge |
| Historical backfill | PASS (offline/static) | `0064` creates one CanonicalEntity per historical Entity and snapshots status |
| Composite FK runtime enforcement | SKIPPED — PostgreSQL runtime integration unavailable | 未配置 `VECTOR_KB_PG_TEST_DSN` |
| Partial unique index runtime enforcement | SKIPPED — PostgreSQL runtime integration unavailable | 未配置 `VECTOR_KB_PG_TEST_DSN` |

Ordinary purge 的验收语义不是把 `Decision.graph_entity_candidate_id` 置空：candidate row 保留且 Decision 可以继续引用它的审计关系。只有真实 physical DELETE candidate row 时，数据库 `ON DELETE SET NULL` 才会将该 FK 置空；本阶段没有 PostgreSQL runtime，因此不声称该行为已实测。

## Migration / Backfill Verification

```text
0063 -> 0064 -> 0065 (single head)
```

`0064` 的 backfill 使用 `SELECT id, gen_random_uuid()` 建立临时 1:1 mapping，再按原 Entity 写入 CanonicalEntity 并回填 `entities.canonical_entity_id`。没有按 `normalized_name`、alias、类型或 ontology version 分组/合并。状态快照为：

```text
pending_review                         -> pending_review
rejected / stale / disabled / deleted  -> disabled
其他                                   -> active
```

这保证历史 identity 安全回填，但不推断历史跨 ontology 等价关系；未来 reconciliation 才处理 `C1 <-> C2`，P1.5 不实现该 job。

## Final Contract

```text
OntologyVersion          -> schema meaning
CanonicalEntity          -> real-world identity
Entity                   -> ontology-specific projection
EntityMention            -> source occurrence
EntityResolutionDecision -> append-only identity decision audit
GraphPublication         -> ontology-specific visible snapshot
```

Identity continuity is preserved. Projection and publication are not auto-migrated by an ontology switch. Materialized does not imply automatically published.

## Known Limitations

- PostgreSQL runtime verification is unavailable without `VECTOR_KB_PG_TEST_DSN`.
- No fuzzy, embedding, or LLM resolver.
- No CanonicalAlias, generic business-identifier registry, stable cross-ontology type identity, historical reconciliation job, Canonical Merge/Split, or Review UI/API.

## Reproducible Verification

P1 targeted acceptance command:

```text
.\\.venv\\Scripts\\python.exe -m pytest -q tests/test_p1_canonical_entity_acceptance.py tests/test_p1_1_canonical_entity_models.py tests/test_p1_2_canonical_entity_resolution.py tests/test_p1_2_canonical_entity_resolution_pg.py tests/test_v03_stale_lifecycle.py tests/test_v04_m5_materializer.py tests/test_v04_m5_purge.py tests/test_v05_m1_publication_schema.py tests/test_v05_m5_publication_reconcile.py tests/test_v05_m5_publication_reconcile_pg.py tests/test_v06_m1_contract.py tests/test_v06_m2_snapshot_resolver.py tests/test_v06_m2_snapshot_resolver_pg.py tests/test_v06_m3_traversal.py tests/test_v06_m3_traversal_pg.py tests/test_v06_m4_api_evidence.py tests/test_v06_m4_api_evidence_pg.py tests/test_v09_graph_governance_actions.py tests/test_v09_graph_governance_contracts.py tests/test_v09_graph_governance_publication.py tests/test_current_ontology.py
```

Latest fresh run: `225 passed, 6 skipped` after adding the four acceptance contract tests. The skipped tests are PostgreSQL runtime-only checks and are not P1 correctness failures.

Full-suite known unrelated failures remain listed separately in the final handoff: shadow-policy (1), OCR (3), and write-guard (2).

## Delivery

Final checkpoint is recorded in Git after fresh verification and push. P2 remains:

```text
NOT AUTHORIZED
```
