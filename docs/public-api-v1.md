# Public Read API v1

Operational recording, request limits, answer leases, retention, and rollback are
documented in [Public API operations](./public-api-operations.md).

`/api/v1` 是面向内部应用和客户集成的只读知识接口。它复用现有知识库权限、
跨库兼容性、文档目录、知识图谱、联邦检索和问答服务，不建立第二套知识数据。

M6 不提供上传、修改、删除、API Key 签发、会话历史或用量记录，也不返回对象
存储地址、模型提示词、供应商原始响应和内部异常。

## 1. 启用条件

接口默认关闭：

```dotenv
PUBLIC_API_V1_ENABLED=false
```

启用前必须同时启用以下已有能力，否则服务拒绝启动：

```dotenv
ORGANIZATION_AUTHORIZATION_ENABLED=true
CROSS_LIBRARY_COMPATIBILITY_ENABLED=true
PERSONAL_LIBRARY_SCOPES_ENABLED=true
FEDERATED_RETRIEVAL_ENABLED=true
KNOWLEDGE_CATALOG_ENABLED=true
GRAPH_CATALOG_ENABLED=true
PUBLIC_API_V1_ENABLED=true
```

关闭时所有 `/api/v1` 路由返回统一的 `not_found`，并且不会先查询用户、组织或
知识库数据。回滚只需将 `PUBLIC_API_V1_ENABLED=false` 并重启服务；M6 没有数据
库迁移和新增持久化状态。

## 2. 认证与权限

两种认证方式使用同一个实名用户和当前权限：

- 浏览器登录后的 JWT Cookie；
- `Authorization: Bearer <API_KEY>`。

API Key 必须绑定当前 Organization，且未过期、未撤销。每次请求都会重新检查
账号、Organization 成员关系、知识库状态和 `read` 权限。保存的知识库范围只是
个人选择，不授予任何权限。

## 3. 路由

```text
GET  /api/v1/libraries
POST /api/v1/scopes/validate
GET  /api/v1/libraries/{slug}/documents/{document_id}
GET  /api/v1/libraries/{slug}/entities/{entity_id}
GET  /api/v1/libraries/{slug}/relations/{relation_id}
GET  /api/v1/libraries/{slug}/evidence/{evidence_id}
POST /api/v1/entities/search
POST /api/v1/relations/search
POST /api/v1/retrieval
POST /api/v1/answers
POST /api/v1/answers/stream
```

交互式 OpenAPI 位于 `/docs`。所有成功和错误响应都包含服务生成的 32 位小写
十六进制 `request_id`，HTTP 响应头同时包含 `X-Request-Id`。

## 4. 知识库范围

聚合检索、搜索和问答必须明确提供一个 `scope`，且只能选择以下一种形式。

直接选择 1 到 20 个知识库：

```json
{
  "scope": {
    "library_slugs": ["projects", "compliance"]
  }
}
```

使用当前用户拥有的命名范围：

```json
{
  "scope": {
    "scope_id": "00000000-0000-4000-8000-000000000001"
  }
}
```

服务保留用户选择顺序。任意成员不存在、已删除、无权访问、跨 Organization 或
不兼容时，整个请求失败；不会静默删除、替换或增加知识库，也不会返回部分结果。

可在执行前检查文本和图谱兼容性：

```bash
curl -X POST "http://127.0.0.1:8100/api/v1/scopes/validate" \
  -H "Authorization: Bearer $VECTOR_KB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "scope": {"library_slugs": ["projects", "compliance"]},
    "channels": ["text", "graph"]
  }'
```

不同 Embedding 输出契约、向量维度、检索策略或图谱 Schema/Publication 契约的
知识库不能在对应通道中一起使用。权限不属于兼容性指纹，始终单独实时检查。

## 5. 检索

`/retrieval` 只检索，不调用问答模型：

```bash
curl -X POST "http://127.0.0.1:8100/api/v1/retrieval" \
  -H "Authorization: Bearer $VECTOR_KB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "scope": {"library_slugs": ["projects", "compliance"]},
    "query": "甲公司投资了哪家施工单位？",
    "top_k": 8,
    "candidate_k": 20,
    "score_threshold": 0.2
  }'
```

响应包含：

```json
{
  "contract_version": "public-retrieval-v1",
  "request_id": "0123456789abcdef0123456789abcdef",
  "scope": {},
  "sources": [],
  "chunks": [],
  "graph": {
    "available": false,
    "entities": [],
    "relations": [],
    "documents_examined": 0,
    "truncated": false
  }
}
```

`sources` 和 `chunks` 使用相同连续排名和相同的 Library、Document、Revision、
Chunk 身份。图谱事实始终保留各自 Library、Ontology、Publication 和 Evidence
身份，不会按同名实体跨库合并。

## 6. 同步问答

```bash
curl -X POST "http://127.0.0.1:8100/api/v1/answers" \
  -H "Authorization: Bearer $VECTOR_KB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "scope": {"library_slugs": ["projects"]},
    "query": "甲公司投资了哪家施工单位？",
    "top_k": 8,
    "candidate_k": 20
  }'
```

响应顶层固定为：

```json
{
  "contract_version": "public-answer-v1",
  "request_id": "0123456789abcdef0123456789abcdef",
  "answer": "根据资料，甲公司投资了乙公司。[1]",
  "sources": [],
  "chunks": [],
  "graph": {}
}
```

模型只接收响应所对应的已授权 Chunk。模型调用前会再次校验完整范围。没有命中
证据时返回固定的无证据答案，不调用模型。M6 不保存问题、答案或部分输出。

## 7. SSE 问答

`POST /api/v1/answers/stream` 接收与同步问答相同的 JSON，请求头增加：

```text
Accept: text/event-stream
```

正常事件顺序：

```text
event: meta
data: {"contract_version":"public-answer-v1","request_id":"..."}

event: delta
data: {"request_id":"...","text":"根据资料"}

event: result
data: {"contract_version":"public-answer-v1","request_id":"...","answer":"...","sources":[],"chunks":[],"graph":{}}
```

每个 `delta.text` 最多 2048 个字符。`result` 使用与同步接口完全相同的
`PublicAnswerResponse` 合同。流开始后的失败只发送一个不含原始异常和内容的
`error` 事件，不发送 `result`。客户端断开时服务会关闭上游模型流，不保存部分
答案。

## 8. 错误格式

```json
{
  "error": {
    "code": "scope_incompatible",
    "request_id": "0123456789abcdef0123456789abcdef",
    "message": "The selected knowledge libraries are incompatible.",
    "details": [
      {
        "library_slug": "compliance",
        "reason_codes": ["embedding_profile_mismatch"]
      }
    ]
  }
}
```

稳定错误码：

| HTTP | code | 含义 |
|---|---|---|
| 401 | `authentication_required` | 缺少或无效认证 |
| 403 | `scope_forbidden` | 当前用户不能使用完整范围 |
| 404 | `not_found` | 公共 API 未启用 |
| 404 | `resource_not_found` | 已授权范围内的资源不存在 |
| 409 | `scope_incompatible` | 选择的知识库在请求通道不兼容 |
| 422 | `request_invalid` | 请求不符合 v1 合同 |
| 502 | `upstream_failed` | 已授权的上游检索或模型调用失败 |
| 503 | `service_unavailable` | 知识服务暂不可用 |
| 503 | `answer_unavailable` | 问答模型未配置或未启用 |
| 500 | `internal_error` | 未分类内部失败 |

只有已授权的兼容性失败可以在 `details` 中返回受限的知识库 slug 和原因码。
认证、权限、资源隐藏和内部错误不返回问题、答案、Chunk、Evidence 文本、存储位置、
密钥、供应商响应或异常详情。

## 9. v1 兼容策略

在 `/api/v1` 内，可以新增不影响旧客户端忽略的可选字段。以下变化必须发布新的
API 版本，不能直接修改 v1：

- 删除或重命名字段；
- 改变字段必填性、类型或边界含义；
- 改变稳定错误码的含义；
- 改变范围的全有或全无规则；
- 改变排序、游标或分页语义；
- 改变 SSE 事件类型和终止规则。

旧的 `/retrieval`、`/chat`、Catalog、Graph Catalog 和版本化图谱接口继续保持原
合同；`/api/v1` 不通过 HTTP 转发到这些旧路由。
