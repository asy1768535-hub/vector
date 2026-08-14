# 技术设计

## 1. 总体架构

新流水线保持现有边界：

Document Revision -> Section-aware Chunk Groups -> Schema Router -> Bounded Extraction Queue -> Provider Rate Limiter -> Candidate Parse/Validation -> Document Aggregation/Materialization -> Draft Graph -> Automatic Publication Plan -> Activation -> Active Graph

Embedding 发布后即可支持向量检索。图谱流水线独立运行并持续更新任务进度，不阻塞上传请求。

## 2. 策略快照

GraphExtractionJob 的 policy/model snapshot 增加：

- build_mode
- unit_planning_version
- schema_routing_version
- schema_subset_hash
- batch_min_chunks / batch_max_chunks
- max_output_tokens
- max_entities_per_batch
- max_relations_per_batch
- provider_max_concurrency
- cache_policy_version

Job 创建时冻结这些字段。已有 v1 快照缺少字段时按 Deep 兼容策略解释。

## 3. 模式配置

| 配置 | Fast | Standard | Deep |
|---|---:|---:|---:|
| 主内容 | 关键 Section | 全文 Section Batch | 全文单元/扩展上下文 |
| Batch | 6-10 Chunk | 4-6 Chunk | 1-3 Chunk |
| Schema | 小型相关子集 | 相关子集 + 回退 | 完整快照 |
| 输出预算 | 1,500-2,500 | 2,500-4,000 | 现有行为或高上限 |
| 默认并发 | 4 | 4 | 2 |
| 质量回退 | 扩大 Schema/切小 Batch | 扩大 Schema/切小 Batch | 完整重试 |

最终数值由 Gold Set 和 Provider 基准确定，不能只依据理论值上线。

## 4. Section Batch 规划

### 4.1 v1 低风险修正

保持一 Chunk 一 Unit，但 Prompt 约束：

- 只允许从 c0 产生事实。
- p1/n1 只能消歧，不得作为事实主证据。
- Evidence 必须引用 c0。

这能立即降低相邻单元重复输出，且不改变表结构。

### 4.2 v2 分组

按以下边界确定性分组：

1. effective_title_path 相同。
2. Chunk 顺序连续。
3. 表格/列表保持完整 Block 边界。
4. 预估输入和输出不超过模式预算。
5. 每组 4-6 Chunk，超密集 Section 动态缩小。

Context ref 使用 c0..cN，每个原始 Chunk 恰好作为主内容出现一次。邻接背景仅在确有跨句指代时增加 p1/n1，并标记 role=neighbor。

现有 ExtractionContextSnapshot 已具备 context mapping 和多个证据引用能力；新增 unit_planning_version=v2 解释 ref 语义。候选绑定继续通过 context ref 定位 Evidence。

## 5. Schema Router

完整 Ontology 快照仍保存在 Job，模型请求只包含路由子集。

路由输入：

- Section 标题路径。
- Chunk 关键词与现有 embedding。
- Entity/Relation Type 的 key、label、description 和属性摘要。
- 库级核心类型白名单。

路由输出：

- 相关 Entity Type IDs。
- 由这些实体端点可达的 Relation Types。
- 必需属性和验证规则。
- 置信度与回退原因。

控制：

- 不调用同等级生成模型做路由。
- 最小/最大类型数量可配置。
- 低置信度时扩大 Top-K。
- Parser 遇到未提供但属于完整 Ontology 的类型时，以扩大子集重试。
- 仍无法覆盖时回退完整快照，并记录 schema_route_fallback。

## 6. Provider 与输出控制

Provider payload 增加最大输出 tokens，并在请求哈希中包含该值。

Prompt 使用紧凑响应契约：

- 禁止重复输出相同实体和关系。
- 每条事实只保留最短充分证据。
- 限制 aliases、properties 和 evidence 数量。
- 明确 Batch 的最大事实预算。

若 finish_reason 表示长度截断、JSON 无法解析或事实密度超过预算：

1. 不写入部分候选。
2. 把 Batch 按 Chunk 边界二分。
3. 在有限预算内重新入队。
4. 单 Chunk 仍超限时进入 Deep 回退或人工可见失败。

## 7. 并发与速率限制

在 worker service 内使用有界异步任务组：

- graph_extraction_worker_concurrency 默认 4。
- 每个任务仍通过现有 FOR UPDATE SKIP LOCKED 与租约领取。
- Provider 级 Semaphore 控制在途请求。
- Token bucket 同时限制 RPM/TPM；未知额度时从低并发启动。
- 429/5xx/timeout 使用指数退避和随机抖动。
- 连续限流触发 AIMD 降并发；稳定窗口后逐级恢复。

多进程部署时总并发必须由部署配置约束，不能让每个进程都误认为自己拥有完整额度。

## 8. 缓存与增量

缓存保存规范化模型语义输出，不直接保存新 Revision 的数据库 Evidence ID。

缓存键包含 library_id、security_scope、section_content_hash、schema_subset_hash、Prompt/Parser/Policy 版本和 model_config_hash。

命中时：

1. 验证 Section 文本逐字一致。
2. 重新把 context quote 绑定到当前 Revision Evidence。
3. 重新执行候选验证、置信度和物化。
4. 记录 cache hit，不伪造 Provider 调用。

缓存只允许库内复用，并遵循敏感载荷清理和保留期限。

## 9. 进度与 ETA

Job counts 从单一 Unit 数扩展为 planned_batches、queued_batches、processing_batches、succeeded_batches、fallback_batches、cached_batches 和 failed_batches。

ETA 使用最近窗口的有效吞吐计算：remaining_work / rolling_batches_per_second。

排队中显示队列位置和估算，不把排队时间伪装成处理时间。任务页不暴露 Prompt、原文或敏感 Provider 响应。

## 10. 自动发布与事后纠错

- 自动发布只处理 production 任务中已通过候选验证、Ontology 约束、Evidence 绑定和置信度门槛的事实；不合格候选不得进入正式图谱。
- 物化事务先独立提交草稿，再复用现有 plan_graph_publication(include_drafts=True) 和 activate_graph_publication 完成发布，禁止直接把事实状态改为 active。
- 初次构建和后续增量构建使用同一发布快照、激活、审计和回滚机制。计划人和激活人沿用抽取任务的 requested_by，系统任务允许为空。
- 发布计划和激活命令使用由任务、知识库、Ontology 与父发布版本派生的确定性幂等键。并发任务遇到父版本变化时重新规划，不激活过期快照。
- 完整成功与部分成功任务都触发自动发布；部分成功只发布已经验证和物化的正确事实。
- 发布失败不得把抽取任务改判为失败，也不得删除已经物化的草稿；工作进程记录发布失败并可安全重试。
- 用户在实际检索、浏览或问答中发现错误后，使用现有治理动作更正，再通过同一发布机制生成新版本。人工审核保留为可选纠错工具，不是正常入图门槛。

## 11. 兼容与迁移

- v1 Job 继续使用现有 Unit 和 Prompt 合约。
- 新 Job 通过 unit_planning_version 选择 v1 或 v2。
- 新字段优先放入已有 JSON snapshot；只有需要查询/索引的状态才增加列或表。
- API 响应增加字段必须保持旧字段兼容。
- Graph draft、review、publication 和 evidence contracts 不变。

## 12. 灰度与回滚

Feature flags 分离：

- GRAPH_EXTRACTION_CENTER_ONLY_ENABLED
- GRAPH_EXTRACTION_SCHEMA_ROUTING_ENABLED
- GRAPH_EXTRACTION_BOUNDED_CONCURRENCY_ENABLED
- GRAPH_EXTRACTION_SECTION_BATCHING_ENABLED
- GRAPH_EXTRACTION_CACHE_ENABLED

灰度顺序：开发基准库 -> 单个测试知识库 -> 新建知识库默认 Standard -> 现有知识库选择迁移。

回滚只影响新 Job：关闭对应 flag 后恢复 v1/Deep 规划。正在运行的 Job 按冻结快照完成，避免半途改变语义。

## 13. 风险

- Schema 子集可能漏类型：置信度门禁和完整 Schema 回退。
- 输出上限可能截断 JSON：检测 finish_reason 并拆 Batch，禁止部分写入。
- 并发可能触发 429：Provider 级限流和自适应降级。
- 缓存可能跨安全范围复用：库、security scope 和策略版本进入键。
- Batch 过大可能降低召回：动态缩小和 Gold Set 质量门禁。
- 性能目标受外部 Provider 波动影响：验收固定模型、机器和时间窗口，记录 Provider 延迟分布。
- 自动发布可能放大错误事实影响：发布前继续执行确定性验证、置信度和 Evidence 门槛，并保留版本审计、快速更正和回滚。
- 同一知识库并发完成多个抽取任务可能导致父发布版本过期：按知识库与 Ontology 串行化计划/激活，检测父版本变化后重新规划。
