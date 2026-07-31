# 前端整合证据清单

## 当前基线

- 实现基线：`origin/main`
- 提交：`3849df8`
- 架构：`admin-ui/index.html` 直接加载 vendored Vue 3、Vue Router、
  Element Plus 和 ES modules。
- 当前路由：dashboard、users、libraries、permissions、documents、search、
  chat、chat-logs、import、api-keys、jobs、operations、audit。

## `origin/main` 已验收能力

- Views：KnowledgeCatalog、RetrievalTest、ClassificationReview、
  GraphGovernance、SchemaLifecycle。
- Helpers/components：catalog_ui、classification_review_ui、
  retrieval_test_ui、graph_governance_ui、graph_exploration_ui、
  schema_lifecycle_ui、GraphCanvas、GraphExplorer。
- 身份：`store.organizations` 和 `/me/organizations` 初始化。
- 权限：effective read、organization admin、library management 投影。
- API：Catalog、分类审核、组织检索诊断、Graph Catalog/Governance/
  Publication/Traversal、Schema Lifecycle。
- Tests：对应 console、API、命令、publication、view 和 UI 契约测试。

## 基线纠正证据

安全备份提交 `d636984` 与 `origin/main` 已分叉，并缺少 v0.8/v0.9 的
Organization、Catalog、Classification、Schema 和 Graph Catalog/Governance
后端代码。后端与测试差异约为 260 个文件、7.3 万行新增实现。

用户要求整合的是已完成 v0.9 的原版前端，且最初明确要求基于
`origin/main` 新建分支。因此实现基线纠正为 `3849df8`，安全备份分支只用于
前端删除操作的回档，不作为可运行 v0.9 基线。

## 权威产品映射

`git show origin/codex/remove-bundled-frontend:docs/12-admin-ui.md`
定义 9 个功能域、19 项历史页面能力和重复能力的唯一归属。实现不继承该文档
“API-only”的当时状态，只继承功能整合、权限和安全约束。

## 活跃前端契约

- `.trellis/spec/frontend/knowledge-catalog-console.md`
- `.trellis/spec/frontend/retrieval-test-console.md`
- `.trellis/spec/frontend/classification-review-console.md`

这些契约明确要求 no-build Vue 3 + Element Plus、后端最终授权、精确身份、
stale response 丢弃、409 不自动重放及受限内容展示。
