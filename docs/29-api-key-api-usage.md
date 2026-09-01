# API Key 接入智能问答

本文给外部系统、脚本、Agent 和项目集成方使用。新的外部接入统一使用稳定的
Public v1 合同；完整接口定义见 [Public Read API v1](./public-api-v1.md)，MCP
接入见 [MCP Knowledge Adapter](./mcp-knowledge-adapter.md)。

## 1. 创建与权限

在管理后台“我的 API Key”页面创建密钥时，必须选择所属组织。密钥只继承该用户
在该组织内的知识库权限，不会因为持有 Key 自动获得更多权限。

创建后只显示一次完整密钥。推荐每个接入系统使用独立 Key，并设置过期时间。

```dotenv
VECTOR_KB_BASE_URL=https://your-vector-kb.example.com
VECTOR_KB_LIBRARY_ID=your_library_slug
VECTOR_KB_API_KEY=创建后显示的完整密钥
```

不要把真实 Key 放进前端代码、URL、日志、截图或 Git 仓库。

## 2. 获取最终回答

```http
POST /api/v1/answers
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

请求体：

```json
{
  "scope": {
    "library_slugs": ["your_library_slug"]
  },
  "query": "星盾 S3 的单套容量是多少？",
  "top_k": 5,
  "candidate_k": 20,
  "score_threshold": 0.0
}
```

`library_slugs` 填知识库 slug，可选择同一组织内 1 到 20 个兼容知识库。服务会
实时检查组织成员关系、知识库状态和 `read` 权限；任意一个库无权访问时整次请求
失败，不会静默缩小范围。

curl 测试：

```bash
curl -X POST "$VECTOR_KB_BASE_URL/api/v1/answers" \
  -H "Authorization: Bearer $VECTOR_KB_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"scope\":{\"library_slugs\":[\"$VECTOR_KB_LIBRARY_ID\"]},\"query\":\"你的问题\",\"top_k\":5,\"candidate_k\":20}"
```

## 3. 返回内容

同步问答固定返回：

```json
{
  "contract_version": "public-answer-v1",
  "request_id": "0123456789abcdef0123456789abcdef",
  "answer": "根据资料，星盾 S3 的单套容量为 5MWh。[1]",
  "sources": [
    {
      "rank": 1,
      "library_slug": "your_library_slug",
      "document_id": "文档 UUID",
      "document_revision_id": "修订 UUID",
      "chunk_id": "切片 UUID",
      "seq": 3,
      "page": 8,
      "title_path": ["产品参数"],
      "title": "产品说明书",
      "score": 0.91,
      "vector_score": 0.82,
      "rerank_score": 0.91
    }
  ],
  "chunks": [
    {
      "rank": 1,
      "library_slug": "your_library_slug",
      "document_id": "文档 UUID",
      "document_revision_id": "修订 UUID",
      "chunk_id": "切片 UUID",
      "title": "产品说明书",
      "content": "证据正文",
      "content_truncated": false,
      "score": 0.91
    }
  ],
  "graph": {
    "available": true,
    "entities": [],
    "relations": [],
    "documents_examined": 1,
    "truncated": false
  }
}
```

字段含义：

| 字段 | 内容 | 客户端用途 |
|---|---|---|
| `answer` | 最终自然语言回答 | 直接展示给用户 |
| `sources` | 文档、修订、切片、页码、标题路径和检索分数 | 引用列表、定位来源 |
| `chunks` | 与 `sources` 相同排名和身份的证据正文 | 引用展开、审计 |
| `graph.entities` | 命中的已发布实体事实 | 图谱解释、关系展示 |
| `graph.relations` | 命中的已发布关系事实 | 关系链展示 |
| `request_id` | 本次请求标识 | 排查错误、关联服务日志 |

`sources[n]` 与 `chunks[n]` 的排名及 Library、Document、Revision、Chunk 身份严格
对应。客户端不要把向量分数、重排分数或模型文本当作权限判断依据。

## 4. 图谱证据与文件位置

图谱实体和关系的 `fact.evidence[]` 包含 `evidence_id`、`document_id`、
`document_revision_id`、页码和字符区间。读取证据详情：

```http
GET /api/v1/libraries/{slug}/evidence/{evidence_id}
Authorization: Bearer <API_KEY>
```

响应会给出证据引用、上下文窗口、页码、字符位置和关联事实。读取文档及当前修订
信息：

```http
GET /api/v1/libraries/{slug}/documents/{document_id}
Authorization: Bearer <API_KEY>
```

Public v1 不直接返回对象存储内部地址。这样可以保留权限检查，避免把长期文件地址
暴露给外部客户端。

## 5. 流式回答

```http
POST /api/v1/answers/stream
Authorization: Bearer <API_KEY>
Content-Type: application/json
Accept: text/event-stream
```

请求体与同步问答相同。事件顺序为 `meta`、多个 `delta`、最后一个 `result`；
`result` 使用与同步接口相同的 `answer`、`sources`、`chunks`、`graph` 合同。

## 6. 只检索不回答

不需要模型回答时使用：

```http
POST /api/v1/retrieval
```

请求体与 `/api/v1/answers` 相同，响应没有 `answer`，保留 `sources`、`chunks` 和
`graph`。旧接口 `POST /libraries/{slug}/query` 仍只返回 `results` 检索切片。

## 7. 旧 Chat 接口兼容说明

`POST /chat/messages` 是已有 Web 聊天链路，不是新外部接入的首选稳定合同。使用该
接口的旧客户端必须同时兼容：

- 普通检索回答：`sources` 有内容；
- 图谱增强回答：`graph_evidence` 有内容，`sources` 可能为空。

Public v1 已把两类结果统一为固定的 `sources`、`chunks` 和 `graph`，新客户端不应
再根据两套顶层字段猜测回答类型。

## 8. 常见错误

| HTTP | 原因 | 处理 |
|---:|---|---|
| 401 | Key 缺失、错误、过期或已撤销 | 检查 Bearer Key，必要时重发 Key |
| 403 | 当前 Key 无权访问完整范围 | 检查组织成员关系和知识库 `read` 权限 |
| 404 | Public v1 未启用或资源不存在 | 检查部署配置、slug 和资源 ID |
| 409 | 多库范围不兼容 | 调用 `/api/v1/scopes/validate` 查看原因码 |
| 422 | 请求不符合合同 | 检查 `scope`、query 和 top/candidate 参数 |
| 503 | 问答模型或知识服务不可用 | 使用 `request_id` 排查服务状态 |

## 9. MCP

MCP 客户端使用同一个组织绑定 API Key。`answer` 工具返回与
`POST /api/v1/answers` 相同的回答、来源、证据正文和图谱结构；
`get_evidence` 以及 `vector-kb://.../evidence/{evidence_id}` 可读取证据详情。

若 `list_libraries` 或 `list_permissions` 为空，先检查配置的 Key 是否属于正确组织，
以及该用户是否已获得目标库权限。这通常是权限配置问题，不是 MCP 连接故障。
