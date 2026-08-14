# 实施计划

## 0. 基线与质量门禁

- 固化当前 6 个 DOCX、208 Chunk 为性能基准集，不提交含敏感内容的原文副本。
- 建立脱敏 Gold Set，覆盖段落、表格、列表、跨句关系、别名、无事实内容。
- 扩展评测脚本，输出端到端耗时、调用次数、输入/输出 tokens、Provider 延迟、重复候选、实体/关系 F1 和 Evidence 有效率。
- 保存当前 v1/Deep 基线报告，作为所有优化项的比较对象。
- 在没有基线报告前，不启用默认行为变更。

## 1. 观测与模式契约

- 为 GraphExtractionJob snapshot 增加 Fast/Standard/Deep 模式和版本化策略字段。
- 增加 Batch/Unit 吞吐、排队时间、在途并发、tokens、429、重试、缓存命中和 ETA 指标。
- 扩展任务监控 API 与前端详情，不暴露敏感内容。
- 增加配置启动校验和模式契约测试。

## 2. 中心切片约束

- 更新 Prompt：v1 只从 c0 生成事实，邻居仅用于消歧。
- Parser/validator 拒绝以邻居为唯一主证据的 v1 新候选。
- 评估重复候选、召回和证据绑定变化。
- 通过质量门禁后，以独立 feature flag 灰度。

回滚点：关闭 center-only flag 即恢复旧 Prompt 版本；旧 Job 继续使用旧 prompt hash。

## 3. Schema 路由

- 实现确定性 Schema 类型索引和 Section 路由器。
- 构造最小可用 Entity/Relation 子图及属性摘要。
- 增加路由置信度、扩大 Top-K 和完整 Schema 回退。
- 把 subset hash 和 routing version 纳入请求、缓存和审计。
- 覆盖 Schema 外类型、低置信度、空路由和回退测试。

门禁：平均输入 tokens 至少下降 60%；Gold Set F1 相对基线下降不超过 2 个百分点；Schema 外接受数为 0。

## 4. 输出预算与安全拆分

- 为 Provider 增加 max_output_tokens，纳入请求哈希。
- Prompt 增加事实数、别名数、属性数和证据长度预算。
- 检测长度截断和不完整 JSON。
- 实现按 Chunk 边界二分重试，不持久化部分输出。
- 增加高事实密度、截断、非法 JSON 和单 Chunk 超限测试。

## 5. 有界并发和限流

- 将 worker 单请求循环改为有界异步任务组。
- 保持现有 claim、租约续期、lost lease 和幂等终态。
- 增加 Provider Semaphore、RPM/TPM token bucket、429/5xx 退避和 AIMD 并发调节。
- 更新本地和部署启动配置，明确“进程数 × 每进程并发 = 总并发”。
- 进行并发 1/2/4/8 梯度压测，Standard 默认只选择通过限流和质量门禁的档位。

门禁：同一 Unit 不重复调用；429 比例低于 1%；Worker 中止后租约可恢复；连接池和内存占用符合部署预算。

## 6. Section Batch v2

- 实现按 title path、Block 类型和 token 预算的确定性分组。
- Context ref 扩展为 c0..cN，每个原始 Chunk 只作为主内容出现一次。
- 扩展 Prompt、Parser 和 Evidence binder 支持多中心映射。
- 高密度 Batch 自动拆分，表格和列表不跨不安全边界。
- v1 与 v2 Job 并存，按 unit planning version 路由。

门禁：模型调用次数下降至少 70%；Evidence quote 逐字定位率 100%；重复候选数下降至少 50%；v1 历史 Job 可继续完成和读取。

## 7. 缓存与增量重算

- 定义库内、安全范围内的抽取缓存模型和清理策略。
- 以 Section 内容、Schema subset、Prompt/Parser/Policy 和模型配置构造缓存键。
- 命中缓存后重新绑定当前 Revision Evidence，并重新验证和物化。
- 修改 Revision 只重算变化 Section，随后执行全局聚合。
- 覆盖跨库隔离、策略变化失效、部分 Revision 变化和敏感清理测试。

## 8. 前端与产品配置

- 建库和编辑页增加 Fast/Standard/Deep 模式，默认 Standard。
- 上传页展示模式，但不要求用户等待图谱完成。
- 任务页展示 Batch 进度、实时并发、排队/处理耗时、ETA、缓存命中和限流状态。
- 分开展示“抽取完成”“自动发布中”“已可用”和“自动发布失败可重试”，不向用户提供强制审核步骤。
- 响应式验证桌面和移动端不出现表格遮挡。

## 9. 自动发布与事后纠错

- 在生产抽取任务物化提交后调用现有发布规划服务，固定使用 include_drafts=True，再调用激活服务；不直接修改实体或关系的正式状态。
- 使用抽取任务的 requested_by 作为规划和激活审计人，并基于任务、知识库、Ontology 和当前父发布生成确定性幂等键。
- 完整成功和部分成功都发布已验证事实；无合格事实时不创建无意义的新发布版本。
- 将发布异常与物化异常分离：发布失败保留草稿和抽取结果，记录可重试状态，不把已完成的抽取改判为失败。
- 处理同库并发发布的父版本变化：检测冲突、重新规划并幂等激活，禁止过期计划覆盖较新的正式图谱。
- 事后更正继续复用治理动作、发布快照、激活、审计和回滚流程；不新增人工审核状态机。
- 为当前已物化的 85 个实体和 9 条关系执行一次补偿性计划与激活，并验证图谱目录按 active 查询可见。

## 10. 验证

新增 graph_extraction_benchmark.py，提供 validate-dataset、run 和 compare 子命令，保存结构化性能及质量报告。

重点回归：

- tests/test_v04_m5_worker.py
- tests/test_v04_m5_jobs.py
- tests/test_v04_m6_gold_pg_integration.py
- tests/test_v04_m4_candidate_routing.py
- admin-ui/jobs_redesign.test.mjs
- admin-ui/libraries_redesign.test.mjs
- admin-ui/import_redesign.test.mjs
- 自动发布聚焦测试：完整成功、部分成功、无合格事实、重复执行幂等、发布失败保留草稿、父版本并发冲突和事后更正再发布。
- Ruff 和 git diff --check

## 11. 上线顺序

1. 只上线观测和基准，不改结果。
2. 灰度中心切片约束。
3. 灰度 Schema 路由和输出预算。
4. 并发从 1 -> 2 -> 4 逐级放量。
5. 单测试库启用 Section Batch v2。
6. 新知识库默认 Standard。
7. 现有知识库由管理员选择迁移。
8. 最后启用缓存与增量重算。

每一步必须同时满足性能、质量和错误率门禁；任一门禁失败即关闭对应 flag，不推进下一阶段。

## 12. 高风险文件与回滚点

- app/services/graph_extraction_prompt.py：Prompt 版本必须冻结，旧 Job 不得读取新 Prompt。
- app/services/graph_extraction_context.py：Evidence mapping 和 Batch 边界必须有确定性测试。
- app/services/graph_extraction_worker.py：并发、租约和幂等是最高风险区域。
- app/services/graph_extraction_provider.py：输出上限、429 和超时不得泄露 API Key 或原始敏感内容。
- app/services/graph_extraction_jobs.py：策略快照和 unit planning version 必须可重放。
- Candidate/Evidence/Materializer：保持草稿与验证语义；自动发布仍必须经过正式规划和激活边界。
- app/services/graph_publication_planner.py 与 graph_publication_activation.py：复用现有幂等、父版本校验、审计和回滚，不为自动发布建立旁路。
- admin-ui/src/views/Jobs.js：只投影持久化状态，不创造前端独立状态机。

## 13. 开始实施前

- 用户明确批准本 PRD、设计和实施顺序。
- 当前正在运行的 v1 图谱任务完成或确认可继续独立运行。
- Gold Set 和基线报告已准备。
- 对 DeepSeek 账户的并发/RPM/TPM 限额完成只读确认。
- GitNexus 对所有拟修改符号完成 impact analysis；HIGH/CRITICAL 风险先向用户报告。
- 不在未提交的大范围工作树中回退或覆盖既有用户改动。
