# 关系型问答的图谱增强检索

## 目标

在不破坏现有 Hybrid RAG 问答可靠性和降级能力的前提下，仅为关系型问题增加一跳、证据约束的已发布知识图谱检索。图谱关系实际参与回答时，用户能看到“知识图谱增强”标识，并能单独查看每条关系对应的原文证据。

## 背景与事实

- 当前聊天入口 `app/api/chat.py::_retrieve_for_chat` 只调用 `app/services/retrieval.py::run_retrieval`，图谱事实尚未进入回答上下文。
- 当前检索支持 Qdrant dense、PostgreSQL keyword、RRF 融合与 Rerank，仍应作为所有问题的主检索路径。
- `app/services/chat_graph_context.py::load_chat_graph_context` 已能从单个引用切片的有效 Evidence 解析当前 Publication 种子，并复用 `app/services/graph_retrieval.py::execute_graph_retrieval_query` 执行一跳已发布图查询。
- v0.6 图检索支持实体 ID 种子、一至两跳遍历、预期 Publication fencing 和 Evidence locator；本任务不新增图数据库或另一套遍历服务。
- 聊天前端 `admin-ui/src/views/Chat.js` 已有普通引用、来源定位以及“查看知识图谱”弹窗，但当前响应与历史只保存 `ChatSource`，不能表达“本轮答案是否使用图谱”及独立关系证据。
- 当前 `工地测试2` 有 198 个已发布实体、26 条已发布关系，图谱覆盖率不足以替代 RAG。
- `docs/superpowers/plans/2026-08-03-ocr-schema-quality-gates.md` 已确认当前自动发布仍可能允许 draft、pending-review 或孤立业务实体进入不可信中间状态。影子评估可并行进行，正式回答融合必须依赖该计划 Phase 2、Phase 3 的 active Publication 质量契约。

## 需求

### R1 触发与降级边界

- 所有问题先执行现有 RAG；只有关系意图明显的问题进入图谱增强候选流程，例如负责、隶属、参与、依赖、影响、上下游、组成及对象关联。
- MVP 使用本地确定性意图规则，不新增 LLM 分类或实体抽取调用。规则在影子模式中校准，默认宁可漏触发，也不让普通问题增加延迟。
- 普通定义、摘要、原文定位及无有效实体的问题保持现有 RAG 行为。
- 图谱开关关闭、超时、Publication 变化、无种子、无关系、Evidence 不完整或内部错误时，静默退回现有 RAG，不阻断回答，也不显示“知识图谱增强”。

### R2 实体种子与 Publication 范围

- 从 RAG 高相关切片绑定的 active EntityMention 和 Evidence 中解析实体 ID，不根据问题文本猜测实体名称。
- 聚合多个命中切片后只选择当前 Library、当前 active Ontology、同一个当前 active Publication 内的种子。
- 种子按 RAG 排名、直接切片证据命中和稳定 ID 确定性排序并限制数量；不跨知识库合并同名实体。
- degraded Publication、draft、pending-review、stale、无有效 Evidence 或不属于冻结 Publication 的事实不能作为种子或答案事实。

### R3 图谱查询与关系上下文

- MVP 固定一跳、双向、有限种子、节点和关系数量，并携带 `expected_publication_id` 与 `include_evidence_locators=true`。
- 仅调用现有 v0.6 已发布图检索服务；默认两跳、全图搜索和跨知识库关系均不纳入 MVP。
- 关系按直接证据重合、RAG 种子排名、关系置信度和稳定 ID 排序；同一 Publication 中相同关系只保留一次。
- 只有关系端点都存在、Evidence locator 完整且能回查当前 ready 文档/修订/原文的关系才能进入回答上下文。
- Prompt 明确分隔“直接检索证据”和“知识图谱关联证据”，直接原文优先；两者冲突时要求模型说明证据不一致，不得静默覆盖。

### R4 共享问答契约

- 流式和非流式接口必须调用同一个图谱增强编排结果，使用相同的触发、种子、排序、证据和降级逻辑。
- 对现有响应做加法扩展：保留 `sources` 不变，新增 `graph_augmented` 和独立 `graph_evidence`；旧客户端忽略新增字段仍可工作。
- `graph_evidence` 只包含受控展示字段：Publication/Relation/Evidence 身份、实体名称、中文关系展示名、文档/切片身份、定位信息及有限原文片段；不返回任意 properties、Prompt、provider payload 或存储凭据。
- 会话历史恢复后仍能重现本轮是否使用图谱及关系证据，不能只存在于首次流式事件中。

### R5 前端展示

- 仅当至少一条经过 Evidence 回查的图谱关系实际进入回答 Prompt 时，在 LLM 回复下方显示“知识图谱增强”。影子命中或仅打开引用图谱不显示该标识。
- 在普通“引用来源”之前显示独立的“关系证据”区域，每条以“实体 A - 中文关系 - 实体 B”呈现，并可查看对应文档原文位置。
- 保留现有普通引用编号和“查看知识图谱”弹窗。关系证据可以进入现有来源查看器，也可从其实体/关系身份打开现有图谱交互，不再开发第二套图谱画布。
- 刷新页面或恢复历史会话后，标识与关系证据保持一致。

### R6 影子模式、灰度与隐私

- 先支持库级 `off | shadow | enabled` 模式和全局紧急关闭开关。新能力初始只启用 `shadow`，不改变 Prompt、答案或前端标识。
- 影子模式记录触发率、种子率、有效关系率、Evidence 回查成功率、额外延迟、截断和固定降级原因码。
- 通过 OCR/Schema 质量门和影子评估后，才能将指定知识库切换为 `enabled`。
- 指标与日志不得记录完整问题、Prompt、原文或敏感图谱属性，只保存计数、时延、模式、固定原因码及必要的受控身份。

## 验收标准

- AC1：非关系型问题、库级关闭状态和所有图谱失败路径的 RAG 结果、普通来源、答案与错误语义保持兼容。
- AC2：关系型问题命中同库 active Publication 的有效关系时，流式和非流式回答使用同一组经过原文回查的关系证据。
- AC3：任何进入 Prompt 的关系都能从返回的 Publication、Relation、Evidence、Document Revision 和 Chunk 身份回查到当前有效原文。
- AC4：`graph_augmented=true` 当且仅当至少一条关系证据实际进入 Prompt；此时回复下方显示“知识图谱增强”和独立“关系证据”。
- AC5：影子模式执行相同候选链路并生成脱敏指标，但答案、响应标识和用户可见来源与普通 RAG 相同。
- AC6：图谱超时、无种子、无关系、Publication 改变、Evidence 回查失败、degraded Publication 和响应不变量失败均自动退回 RAG。
- AC7：流式首个来源事件、非流式响应、数据库历史恢复及管理审计读取保持同一新增契约；旧 `sources` 字段不改名、不改语义。
- AC8：页面刷新后仍能显示已持久化的“知识图谱增强”和关系证据；点击关系证据能查看原文，现有图谱弹窗继续可交互。
- AC9：质量门未完成前，生产知识库不能从 `shadow` 切换为正式回答融合；active Publication 中 draft/pending-review、无有效证据和孤立业务实体数量必须为零。

## 技术约束

- 正式融合依赖 `docs/superpowers/plans/2026-08-03-ocr-schema-quality-gates.md` Phase 2、Phase 3；本任务不实现 OCR、Schema 噪声过滤或 Publication eligibility。
- 实现前必须按 `AGENTS.md` 对每个拟修改函数、类或方法运行 GitNexus upstream impact；HIGH/CRITICAL 必须先报告。提交前必须运行 `gitnexus_detect_changes()`。
- 保留工作树中无关的历史修改，不清理或重写现有 Publication 和聊天历史。

## 暂不纳入

- 用知识图谱替代现有 RAG，或让所有问题默认查询图谱。
- 两跳/全图搜索、复杂影响分析、跨知识库图谱融合。
- 新增 Agent、图数据库、图遍历服务或第二次 LLM 实体识别调用。
- OCR/PDF 路由、Schema 质量门、旧 Publication 自动修复或生产数据重抽取。
- 重做知识图谱画布或聊天引用图谱弹窗。
