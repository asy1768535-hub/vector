# AI 探索建图功能交接记忆

更新时间：2026-08-05  
项目目录：仓库根目录（vectorDatabase）

## 1. 用户真正要实现的产品逻辑

Schema 是未来长期使用的约束，主要用于后续新增、替换文件时保证实体类型和关系类型稳定。首次面对完全陌生的文件时，不应该要求用户先提供 Schema，而应该由 AI 先探索文件并提出 Schema。

目标流程：

```text
首次上传陌生文件
-> AI 阅读本批次全部文件
-> 发现实体类型、关系类型、关系方向和端点约束
-> 生成 AI 建议 Schema
-> 冻结本次任务的 Schema 快照
-> 使用该快照对全部文件正式抽取
-> 合并别名并验证原文证据
-> 保存知识图谱和待确认 Schema 草稿
```

后续上传有两种模式：

- `explore`：AI 探索。无论知识库是否已有 confirmed Schema，都重新探索本批文件并生成新的 AI draft。
- `governed`：严格 Schema。必须存在用户确认并激活的 Schema，直接使用它抽取，不运行 discovery。

首次上传默认应走 `explore`。没有 Schema 不能成为“开始上传”按钮不可用的原因。

## 2. 用户已经否定的设计

- 不接受固定 8 类实体、17 类关系作为所谓“AI 探索”。
- 不接受所有实体最终都降级为 `Entity`。
- 不接受具体关系（例如“汇报给”）降级成 `related_to` 或“关联”。
- 不接受通过 `项目` 后缀等业务硬编码合并实体。
- 不接受每个文件分别发现一套 Schema；同一上传批次必须共享一次 discovery 和同一个冻结快照。
- 不接受只靠 mock/gold deterministic 测试就声称真实模型已通过。
- 不接受为了修一个功能进行无关的全仓库审核、旧测试清理或大范围重构。

## 3. 最初暴露的问题

上传页面曾显示：

```text
当前知识库没有生效中的知识结构（Schema）
```

文件已经进入等待队列，但“开始上传”不可用。服务状态页面还曾显示多个图谱 Worker 离线、实例数 `0/0`。这些截图属于问题发生时的历史状态，不能当成当前运行状态，重新启动前需要再次确认。

旧图谱抽取结果与标准答案明显不一致：

- 标准答案：20 个实体、18 条关系。
- 旧实际结果：14 个实体、19 条关系。
- 漏掉：`李明`、`贵州省贵阳市`、`贵州省贵阳市花溪区`、`XY-2025-0318`、`贵阳南部电网`、`继电保护系统`、`黔电网批复〔2026〕118号`。
- 同一个项目被拆成 `青岩光伏电站` 和 `青岩光伏电站项目`。
- 所有类型显示为 `Entity`。
- `王芳 -> 周强` 应为“汇报给”，却显示为通用“关联”。

## 4. 当前代码中已经存在的实现

以下内容已经通过源代码检查确认存在，但不代表真实端到端已经成功：

- 批次级 `SchemaDiscoveryRun`。
- `library_id + source_set_hash` 唯一协调。
- 数据库 `FOR UPDATE/SKIP LOCKED` 抢占 discovery run。
- discovery 完成前图谱任务保持 `waiting_schema`。
- discovery 成功后为本批任务绑定同一个 ontology version、snapshot 和 snapshot hash。
- AI draft 使用 `origin=ai_discovery`、`confirmed=false`、`status=draft`。
- confirmed 后可切换为 governed Schema。
- 已移除固定 8/17 ontology、固定实体白名单、项目后缀归一和 `related_to` 泛化降级。
- 正式抽取从任务冻结快照读取 ontology，不读取后来变化的 active Schema。
- 正文过长时会生成 `u0-p1` 等分片，实际发送分片正文，并按源文档归并。
- discovery 输出预算动态计算，不再在生产调用处固定为 700。
- provider 支持 `response_format={"type":"json_object"}` 不兼容时降级重试。
- 网络错误、超时、408/409/429/5xx 最多重试两次。
- 空响应、截断响应和不合法 Schema 不会保存半成品 draft。

主要文件：

- `app/services/schema_discovery_runs.py`
- `app/services/graph_schema_discovery.py`
- `app/services/graph_extraction_batch_eval.py`
- `app/services/graph_extraction_provider.py`
- `app/services/graph_extraction_triggers.py`
- `app/services/token_budget.py`
- `app/services/schema_lifecycle_actions.py`
- `app/services/graph_extraction_materializer.py`
- `app/services/graph_normalization.py`
- `admin-ui/src/views/Import.js`
- `admin-ui/src/views/GraphGovernance.js`
- `scripts/graph_discovery_eval.py`
- `eval/graph_extraction/gold_standard.json`

## 5. 实际模型请求代码

网络层位于 `app/services/graph_extraction_provider.py`，实际向以下地址发送请求：

```text
POST {base_url}/chat/completions
```

请求主体实际包含：

```json
{
  "model": "<configured model>",
  "messages": [],
  "temperature": 0,
  "stream": false,
  "response_format": {"type": "json_object"},
  "chat_template_kwargs": {"enable_thinking": false},
  "max_tokens": "<computed output budget>"
}
```

`chat_template_kwargs` 只在模型名称包含 `qwen` 时加入。Authorization 使用 API key，但日志不应输出 API key 或完整文档正文。

Schema discovery 的消息由 `app/services/graph_schema_discovery.py` 构造，用户消息包含：

```json
{
  "response_format": {"type": "json_object"},
  "protocol_schema": {
    "entity_types": "array of {key,label,description,attributes}",
    "relation_types": "array of {key,label,description,direction,attributes}",
    "constraints": "array of {source_type_key,relation_type_key,target_type_key,cardinality}"
  },
  "excerpts": [
    {"key": "doc-1", "text": "<document excerpt>"}
  ]
}
```

正式抽取的真实应用层结构位于 `app/services/graph_extraction_batch_eval.py`，是 `batches` 数组，不是把 `batch_key` 放在顶层：

```json
{
  "schema_subset_hash": "...",
  "frozen_ontology": {
    "entity_types": [],
    "relation_types": [],
    "relation_constraints": [],
    "constraints": []
  },
  "batches": [
    {
      "batch_key": "u0-p1",
      "untrusted_context": "<split document text>"
    }
  ],
  "output_protocol": {
    "batches": "array of {batch_key,entities,relations}",
    "entity": "{local_id,name,entity_type_key,aliases,properties,confidence,evidence}",
    "relation": "{source_local_id,relation_type_key,target_local_id,properties,confidence,evidence}",
    "evidence": "array of {context_ref,quote}"
  }
}
```

## 6. 目前仍未解决的两个阻塞问题

### 6.1 已有 confirmed Schema 时 explore 模式仍会阻止上传

`app/services/graph_extraction_triggers.py` 当前逻辑：

```python
exploration_available = (
    graph_ready
    and schema_mode == "explore"
    and (not schema_ready or ai_draft)
)
```

因此，当知识库已有 confirmed active Schema 时，`schema_ready=True`、`ai_draft=False`，最终 `exploration_available=False`。

`admin-ui/src/views/Import.js` 在 explore 模式下又要求 `exploration_available === true` 才允许上传。因此报告中“explore 模式即使存在旧 Schema 也会重新 discovery”的说法与实际前端行为不一致。

正确规则应该是：

```python
exploration_available = graph_ready and schema_mode == "explore"
```

active Schema 是否存在，只应影响 governed 模式，不应影响 explore 模式。

### 6.2 Schema 语义校验失败后没有模型纠错重试

真实 Qwen 请求已经执行，但模型返回：

```json
{
  "source_type_key": "project",
  "relation_type_key": "located_in",
  "target_type_key": "string_literal"
}
```

`string_literal` 没有出现在 `entity_types` 中，所以校验器正确拒绝了该 Schema。但当前重试函数只重试 provider 网络/HTTP 错误。JSON 能解析但协议引用非法时会直接失败，不会把错误反馈给模型修正。

真实失败记录：

`eval/graph_extraction/results/graph-discovery-real-20260805T090051Z-failed.json`

该 artifact 中：

- `status=failed`
- `metrics=null`
- `schema_hash=unavailable_discovery_failed`
- 没有正式抽取结果

因此真实 20/18 评测尚未完成。

## 7. 下一位实现者只需要完成的工作

### 工作 A：修复 explore 上传条件

1. 修改 `graph_extraction_upload_configuration()`。
2. `schema_mode=explore` 时，只要运行时、provider、library、安全等级可用，就返回 `exploration_available=true`。
3. 即使已有 confirmed active Schema，也必须允许 explore。
4. governed 模式继续要求 confirmed Schema。
5. 增加后端和前端测试：已有 confirmed Schema + explore 模式仍可开始上传，并创建新的 discovery run。

### 工作 B：增加一次 Schema 纠错请求

1. 强化 discovery 提示词，明确所有 constraint 引用必须来自本次返回的 `entity_types` 和 `relation_types`。
2. 禁止未声明的 `string_literal`、`text`、`string` 成为关系端点。
3. 地点、编号、批复、合同等如果参与关系，应发现为实体类型；普通属性值不要建立关系约束。
4. `direction` 只允许 `directed/undirected`。
5. cardinality 只允许协议定义值。
6. 当输出 JSON 可解析但协议/引用校验失败时，额外请求模型修复一次。
7. 修复请求应包含校验错误和上一版 Schema，要求返回完整修正版 Schema。
8. 不得静默删除非法 constraint，也不得硬编码 location 等业务类型。
9. 第二次仍失败时才将 discovery run 标记为失败。

### 工作 C：完成真实端到端验证

必须真实经过：

```text
discovery
-> Schema 校验
-> 保存 draft
-> 冻结快照
-> 正式抽取全部文件
-> 实体归一/证据校验
-> materialization
-> scorer
```

最终报告：

- 实体召回率
- 关系召回率
- 类型准确率
- 别名合并率
- 脱敏后的 discovery 请求/响应
- 脱敏后的正式抽取请求/响应
- 失败时保存真实失败 artifact，不得伪造 1.0

## 8. 已报告的测试状态

上一位实现者报告：

- 后端定向测试：122 passed，1 deselected。
- 前端相关测试：146 passed，0 failed。
- Ruff、Python 编译、`git diff --check` 和导入烟测通过。
- 未启动实际服务。
- 真实模型 discovery 执行过，但因 `string_literal` 非法引用失败。

这些测试说明局部代码可运行，但不能替代真实端到端成功。

## 9. 工作区与操作限制

- 当前工作区是 dirty 状态，包含大量原有未提交修改和已删除文档。
- 不要回滚、恢复、清理或提交不是自己产生的修改。
- 不要使用 `git reset --hard` 或 `git checkout --`。
- 不要为了本功能修复无关历史测试。
- 未经用户明确要求，不要启动服务、重跑旧图谱任务或提交代码。
- 用户偏好中文、直接、少废话；不要把大量 token 花在重复审核和宽泛报告上。
- 每次报告必须区分：代码存在、mock 测试通过、真实模型端到端通过。

## 10. 验收标准

只有同时满足以下条件才能说“AI 探索建图完成”：

1. 无 Schema 时可以开始上传。
2. 有 confirmed Schema 时选择 explore 仍可以开始上传。
3. 同一上传批次只运行一次 discovery。
4. Schema 非法时可自动纠错一次。
5. discovery 成功后才进入正式抽取。
6. 全批文件使用同一个冻结快照。
7. 实体不会全部变成 `Entity`。
8. `reports_to` 等具体关系不会变成 `related_to`。
9. 别名合并不会依赖项目后缀硬编码。
10. 图谱最终成功写入并能在图谱页面浏览。
11. 真实 20 实体/18 关系评测产生非空指标。

