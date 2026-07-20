# 文档索引

> **v0.7 corrective G2.2 (2026-07-20):** Calibration v3 is preserved as
> non-authoritative audit evidence after exposing latency-measurement interference and
> a phase-aware acceptance defect. G2.2 permits only bit-for-bit scorer optimization,
> moving memory instrumentation outside latency samples, phase-aware artifact checks,
> and fresh v4 run identities. Dataset v2, scorer v2 outputs, thresholds, gates,
> holdout seal, security boundaries, and default-off rollout remain unchanged.

按功能拆分；每篇都自洽，可独立阅读。建议新人按 1→4 的顺序读完核心概念后，再按需查阅。

## 项目总览

| # | 文档 | 适合谁 |
|---|---|---|
| 01 | [项目介绍](./01-introduction.md) | 第一次接触 |
| 02 | [架构总览](./02-architecture.md) | 想知道系统怎么搭起来的 |
| 03 | [项目结构](./03-project-structure.md) | 想知道每个文件夹/文件是干嘛的 |
| 04 | [快速开始](./04-quickstart.md) | 想 10 分钟跑起来 |
| 05 | [配置说明](./05-configuration.md) | 改 `.env` 之前必读 |
| **18** | **[使用说明（操作手册）](./18-usage-guide.md)** | **日常怎么用 ← 推荐先看这篇** |

## 核心功能

| # | 文档 | 主题 |
|---|---|---|
| 06 | [认证系统](./06-authentication.md) | fastapi-users JWT cookie + API Key 双通道 |
| 07 | [权限系统](./07-permissions.md) | Casbin RBAC，(user, library, action) 三元组 |
| 08 | [库管理](./08-libraries.md) | 多租户隔离：每库一个 Qdrant collection |
| 09 | [文档摄入](./09-document-ingest.md) | 切分 / 入队 / 幂等 / 异步 embed |
| 10 | [检索接口](./10-retrieval-api.md) | Dify external knowledge base spec |
| 11 | [Embedding Worker](./11-worker.md) | `FOR UPDATE SKIP LOCKED` 队列消费 |
| 12 | [管理后台 UI](./12-admin-ui.md) | 零构建 Vue 3 SPA |

## 参考资料

| # | 文档 | 用途 |
|---|---|---|
| 13 | [API 完整参考](./13-api-reference.md) | 所有 endpoint 速查 |
| 14 | [数据库 Schema](./14-database-schema.md) | 9 业务表 + casbin_rule（迁移至 0010） |
| 15 | [部署 / 生产清单](./15-deployment.md) | 上线前要检查的项 |
| 16 | [测试](./16-testing.md) | 跑测试 + 加测试 |
| 17 | [常见问题](./17-faq.md) | 踩坑速查 |

## 一致性与发布

| # | 文档 | 主题 |
|---|---|---|
| 19 | [源库正文补全](./19-source-enrichment.md) | 跨库按外键回查正文 |
| 20 | [修订与删除一致性](./20-revision-and-deletion-consistency.md) | revision / 删除 outbox / 三阶段重建 |
| 21 | [批次 A 实施方案](./21-batch-a-implementation-plan.md) | 一致性实现记录 |
| 22 | [内部试运行清单](./22-internal-pilot-checklist.md) | 上线门槛 + 六步验收 |

## 版本说明

| # | 文档 | 主题 |
|---|---|---|
| 28 | [升级说明（main → v0.1.7）](./28-upgrade-summary-v0.1.7.md) | 做了什么、有什么用、为什么做、下一步建议 |
| 30 | [v0.2 Evidence acceptance runbook](./30-v0.2-evidence-acceptance-runbook.md) | M5 release gates, migration dry-run, rollback dry-run, PostgreSQL/Qdrant acceptance |
| 31 | [v0.2 Evidence Foundation upgrade summary](./31-upgrade-summary-v0.2-evidence-foundation.md) | Compatibility fields, release scope, and v0.3 evidence handoff |
| 32 | [v0.4 Graph Extraction evaluation runbook](./32-v0.4-graph-extraction-eval-runbook.md) | Synthetic Eval safety, real DeepSeek runs, immutable artifacts, verification and disposal |
| 33 | [v0.4 Graph Extraction Pipeline upgrade summary](./33-upgrade-summary-v0.4-graph-extraction-pipeline.md) | M1-M6 changes, fail-closed rollout, compatibility, rollback and v0.5 boundary |
| 34 | [v0.5 Active Graph Publication operator summary](./34-v0.5-active-graph-publication.md) | Feature gate, lifecycle, API roles, healthy/degraded reads, rollout and rollback |
| 35 | [v0.6 Published Graph Retrieval operator guide](./35-v0.6-published-graph-retrieval.md) | Configuration, evaluation gates, staged rollout, stable observability, incidents, disable and rollback |
| v0.4 Acceptance | [Graph Extraction Pipeline acceptance](./testing/acceptance/v0.4-graph-extraction-pipeline.md) | Exact commits, hashes, metrics, real PostgreSQL/Qdrant gates and release decision |
| v0.5 Acceptance | [Active Graph Publication acceptance](./testing/acceptance/v0.5-active-graph-publication.md) | Dirty boundary, release gates, compatibility, privacy/scope audit and release decision |
| v0.6 Acceptance | [Published Graph Retrieval acceptance](./testing/acceptance/v0.6-published-graph-retrieval.md) | Frozen M5 evidence, non-skipped PostgreSQL, compatibility, privacy/scope, rollback and release decision |
| v0.3 | [Graph Relation Foundation spec](./superpowers/specs/2026-07-09-v0.3-graph-relation-foundation.md) | Ontology, graph facts, evidence binding, lifecycle, API and acceptance contracts |
| v0.4 | [Graph Extraction Pipeline Master Plan](./superpowers/plans/2026-07-10-v0.4-graph-extraction-pipeline.md) | M1-M6 boundaries, evidence/schema contracts, safety, evaluation and release gates |
| v0.5 | [Active Graph Publication Master Plan](./superpowers/plans/2026-07-15-v0.5-active-graph-publication.md) | M1-M6 plan for graph publication schema, manifests, activation, API, reconciliation and acceptance |
| v0.6 Design | [Published Graph Retrieval Master Design](./superpowers/specs/2026-07-16-v0.6-published-graph-retrieval.md) | Approved Master Design for exact seed resolution, bounded published-graph traversal, Evidence locators and fail-closed reads |
| v0.6 M1 | [Contract, configuration and completion record](./superpowers/plans/2026-07-16-v0.6-published-graph-retrieval-m1.md) | Accepted strict DTOs, absolute v1 limits, startup validation, no route and no migration |
| v0.6 M2 | [Snapshot loader, exact resolver and completion record](./superpowers/plans/2026-07-16-v0.6-published-graph-retrieval-m2.md) | Completed healthy membership aggregates, exact seed/type resolution, cross-scope errors, publication fences and PostgreSQL acceptance |
| v0.6 M3 | [Deterministic bounded traversal and completion record](./superpowers/plans/2026-07-16-v0.6-published-graph-retrieval-m3.md) | Completed direction-aware BFS, stable ordering, atomic truncation, bounded per-hop SQL and PostgreSQL acceptance |
| v0.6 M4 | [Evidence locator, REST API and completion record](./superpowers/plans/2026-07-16-v0.6-published-graph-retrieval-m4.md) | Completed bounded frozen-support hydration, current-ready locators, strict response assembly, read permission, sanitized errors, timeout and PostgreSQL acceptance |
| v0.6 M5 | [Evaluation, performance and operations completion record](./superpowers/plans/2026-07-16-v0.6-published-graph-retrieval-m5.md) | Completed calibration, candidate-max policy freeze, three independent post-freeze runs and verified release evidence |
| v0.6 M6 | [Release acceptance and completion record](./superpowers/plans/2026-07-17-v0.6-published-graph-retrieval-m6.md) | Completed immutable evidence, non-skipped PostgreSQL, inherited compatibility, privacy/scope, rollback and full-suite gates |
| v0.7 Design | [Publication-Scoped Entity Linking Master Design](./superpowers/specs/2026-07-17-v0.7-publication-scoped-entity-linking.md) | G1 PASSED; single-scope entity-linking contract frozen and G2 feasibility planning authorized, with no evaluation or production implementation approval |
| v0.7 G2 | [Entity Linking Feasibility Plan](./superpowers/plans/2026-07-17-v0.7-publication-scoped-entity-linking-g2.md) | Corrective G2.1 approved after immutable ordinal-1 NO-GO; scorer-v2, fair benchmark, fresh holdout and new evidence chain authorized |
| v0.4 M1 | [M1 implementation and completion record](./superpowers/plans/2026-07-10-v0.4-graph-extraction-pipeline-m1.md) | Migration 0021, fail-closed configuration, Library safety controls, heartbeat and real PostgreSQL acceptance |
| v0.4 M2 | [M2 implementation plan](./superpowers/plans/2026-07-13-v0.4-graph-extraction-pipeline-m2.md) | Evidence-aware Context Builder, strict Parser, Prompt, Provider adapter and fenced Attempt recording |
| v0.4 M3 | [M3 implementation plan](./superpowers/plans/2026-07-13-v0.4-graph-extraction-pipeline-m3.md) | Candidate/Occurrence staging, exact Evidence binding, provenance and Migration 0022 |
| v0.4 M4 | [M4 implementation plan](./superpowers/plans/2026-07-13-v0.4-graph-extraction-pipeline-m4.md) | Shared Validator, normalization_v1, deterministic Candidate aggregation, conflicts and confidence routing |
| v0.4 M5 | [M5 implementation plan](./superpowers/plans/2026-07-14-v0.4-graph-extraction-pipeline-m5.md) | Operational Worker, Retry/Rerun, Materializer, API, triggers, compensation and purge |
| v0.4 M6 | [M6 implementation and completion record](./superpowers/plans/2026-07-14-v0.4-graph-extraction-pipeline-m6.md) | Real DeepSeek evaluation, frozen release policy, PostgreSQL/compatibility gates and v0.4 acceptance |

---

更多设计与一致性方案见上表 20/21 篇及各专题文档。
