# 技术设计：关系型问答的图谱增强检索

## 1. 设计原则

该能力是现有 RAG 的可选增强层，不是新的问答主链路。最低可行机制为：RAG 先召回切片，确定性判断关系意图，从命中切片的受治理 Evidence 解析已发布实体，一次执行有界一跳查询，回查原文后才把关系证据加入 Prompt。任一新增步骤失败即返回原 RAG 结果。

```text
问题
  -> 现有 Hybrid RAG
  -> 关系意图门
  -> 命中切片/Evidence -> 当前 Publication 实体种子
  -> v0.6 一跳双向查询
  -> Evidence 当前性回查
  -> 排序、去重、预算裁剪
  -> shadow: 只记录指标
     enabled: RAG 上下文 + 关系证据 -> 现有回答模型
```

## 2. 模块边界

### 2.1 新共享编排服务

新增一个小型 `app/services/chat_graph_augmentation.py`，返回不可变结果对象，例如：

```python
ChatGraphAugmentation(
    mode="off|shadow|enabled",
    triggered=False,
    augmented=False,
    graph_evidence=(),
    prompt_records=(),
    reason_code="not_relationship_query",
    latency_ms=0,
)
```

该服务拥有关系意图判断、种子聚合、图查询、Evidence 回查、排序/去重、预算裁剪和固定降级原因。`app/api/chat.py` 的流式与非流式端点只消费该结果，不各自实现图逻辑。

现有 `app/services/chat_graph_context.py` 的切片可见性、Evidence 到 Publication 解析和 v0.6 调用规则应提取为可复用的公开 helper，原 `GET /libraries/{slug}/chat/graph-context` 继续通过同一 helper 工作，响应契约不变。不得复制 Publication 选择 SQL 到第二个服务。

### 2.2 关系意图门

MVP 使用可单测的确定性规则：标准化空白与标点后，匹配关系动词/疑问结构组合，而不是只要出现一个词就触发。示例包含“谁负责 X”“A 属于哪个部门”“A 和 B 有什么关系”“哪些系统依赖 X”。定义、摘要、列举原文、翻译等明确非关系模式不触发。

规则只决定是否尝试图查询，不决定答案事实。词表与原因码是代码常量；影子指标用于调整规则，不存储原问题。

### 2.3 多切片种子解析

输入是 `run_retrieval` 的有序 records。只接受能解析为 UUID 的 `chunk_id`，按 RAG 顺序去重并限制参与解析的切片数。共享 Publication resolver 完成：

1. 校验 Chunk 属于当前 Library、ready Document 和当前 ready Revision。
2. 收集 Chunk、active EntityMention、active RelationEvidence 的 Evidence ID。
3. 只匹配 active Publication 的 active frozen items，拒绝 degraded Publication。
4. 多切片必须收敛到同一个当前 Publication/Ontology；若身份不一致，固定降级为 `publication_changed`。
5. 种子优先级为直接 Entity item 命中、关系端点命中、RAG 顺序、稳定 UUID；限制为现有 v0.6 最大种子数以内。

### 2.4 图查询与 Evidence hydration

构造一次 `GraphRetrievalQueryRequest`：

- `expected_publication_id`：解析出的精确 Publication。
- `ontology_version_id`：同一 Publication 的 Ontology。
- `direction="both"`、`max_hops=1`。
- `include_evidence_locators=true`。
- 节点、关系和种子采用比平台上限更小的聊天预算，具体默认值经影子基准确定，且始终受现有全局上限约束。

v0.6 返回 locator 后，聊天层必须再次按 Library、Document、当前 Revision、Evidence status 和 Chunk 当前性回查可展示原文。locator 只有坐标，没有原文；不能直接把实体/关系标签当作证据写入 Prompt。

### 2.5 排序、去重和 Prompt

候选关系首先丢弃端点缺失、Evidence 不可回查和跨 Publication 项。排序键依次为：Evidence 与高排名 RAG 切片直接重合、关联种子最佳 RAG 名次、关系 confidence（有值时）、稳定 Relation ID。相同 Publication/Relation ID 只保留一次；相同 Evidence 原文可被多个关系引用，但 Prompt 中原文片段按 Evidence ID 去重。

图上下文使用独立标题和受控文本：

```text
[直接检索证据]
...

[知识图谱关联证据]
关系 1：实体 A - 中文关系 - 实体 B
证据：...
```

关系事实与直接证据冲突时，系统提示模型明确说明不一致并优先引用直接原文。图上下文有独立字符预算；预算不足时裁剪低排名关系，不挤占现有 RAG 的最低上下文预算。

## 3. API 与持久化契约

### 3.1 响应模型

在 `ChatMessageResponse`、`ChatHistoryMessage` 和流式 `sources` 事件中增加：

```json
{
  "graph_augmented": true,
  "graph_evidence": [
    {
      "publication_id": "...",
      "relation_id": "...",
      "source_entity_id": "...",
      "source_entity_name": "A",
      "relation_type_key": "responsible_for",
      "relation_label": "负责",
      "target_entity_id": "...",
      "target_entity_name": "B",
      "evidence_id": "...",
      "document_id": "...",
      "document_revision_id": "...",
      "chunk_id": "...",
      "title": "...",
      "page_start": 1,
      "page_end": 1,
      "content": "受限长度的证据片段"
    }
  ]
}
```

字段使用严格 Pydantic 模型、列表上限和文本长度上限。`sources` 保持原状。`graph_augmented` 由服务端根据实际进入 Prompt 的关系数计算，客户端不得推断。

### 3.2 历史持久化

为 `chat_messages` 增加非空布尔列 `graph_augmented`（默认 false）和可空 JSONB `graph_evidence`。JSON 只保存上述 allowlist DTO，不保存任意 properties 或 provider 内容。采用一条 additive Alembic 迁移；旧消息自然读取为 `false/[]`，无需回填或重写历史。

该选择比新增多张聊天图谱表更小：数据有严格上限、只随消息读取、无需独立查询或关系完整性维护。若未来需要按 Relation 做大规模审计检索，再以迁移拆表，不在 MVP 预建抽象。

## 4. 流式与非流式时序

`_retrieve_for_chat` 继续只负责授权、库状态和 RAG。新增共享准备函数在 RAG 后调用图谱编排，返回原 records、回答用 records/context 和 graph metadata。非流式调用现有 answer 生成后返回新增字段；流式在第一个 `sources` 事件中同时发送新增字段，并在完成/失败持久化时保存同一份不可变 metadata。

影子模式执行至 Evidence 回查和排序，但传给回答模型的仍是原 records，响应固定为 `graph_augmented=false, graph_evidence=[]`。内部只记录聚合指标。

## 5. 前端状态

`admin-ui/src/views/Chat.js` 的 AI 消息模型增加 `graph_augmented` 与 `graph_evidence`，发送、SSE、非流式和历史恢复都使用同一字段名。

展示顺序：回答正文 -> “知识图谱增强”标识与“关系证据”折叠区 -> 现有“引用来源” -> 输入区。关系行显示中文 label，保留英文 key 仅作内部身份，不在普通展示中重复。点击证据复用现有来源定位弹窗；点击关系/实体身份复用现有 citation graph dialog。空数组和 shadow 模式不渲染区域。

## 6. 开关、观测与发布

- 全局配置提供紧急 kill switch、超时和聊天图谱预算。
- Library 增加 `graph_assisted_chat_mode`，枚举 `off|shadow|enabled`，默认 `off`；管理端库编辑页显示，建库页暂不增加复杂选项。
- 只有全局开启且库模式为 `shadow/enabled` 才执行图链路；只有 `enabled` 才允许进入 Prompt。
- 原因码至少包含：`disabled`、`not_relationship_query`、`no_chunk_ids`、`no_seeds`、`no_relations`、`invalid_evidence`、`publication_changed`、`publication_unavailable`、`timeout`、`internal_error`、`augmented`。
- 日志只记录 Library ID、模式、计数、截断、时延和原因码；生产指标使用计数器/直方图，不记录查询或证据正文。

发布顺序：代码与迁移 -> 全局开启但库默认 off -> 测试库 shadow -> 质量门完成并验证 active Publication -> 比较 shadow 指标与人工样本 -> 单库 enabled。回滚只需将库模式改为 off 或关闭全局开关；新增字段保留，不回滚用户数据。

## 7. 兼容性与风险

- 旧客户端忽略新增响应字段；旧历史行使用数据库默认值。
- 最大风险是低质量 Publication 把噪声固化进答案，因此正式融合被质量门硬阻塞，不能用“查询成功”代替“事实可信”。
- 图查询增加延迟，通过保守意图门、一次聚合查询、严格超时和 fail-open 控制。
- 多切片可能命中不同 Publication；不得混合，必须使用当前精确 Publication 或降级。
- 图关系和 RAG 原文可能冲突；不得在编排层静默消解，必须保留直接证据优先的 Prompt 约束。

## 8. 预计影响面

拟涉及 `app/api/chat.py`、`app/services/chat_graph_context.py`、新增聊天图谱编排服务、聊天 schema/history model/service、Library model/admin schema/API、Alembic 迁移、`admin-ui/src/views/Chat.js`、知识库编辑 UI 及对应测试。真正编辑任一函数/类/方法前，必须重新获得 GitNexus upstream impact 并向用户报告风险；当前文档不授权这些代码修改。
