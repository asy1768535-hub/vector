# 10 · Dify 兼容检索接口

## 端点

```
POST /retrieval
```

**鉴权**：`Authorization: Bearer <api_key>`（或 cookie，但 Dify 通常用 Bearer）

**权限**：在 `knowledge_id` 对应的库上有 `read` 权限。

## 请求 schema（严格按 Dify spec）

```json
{
  "knowledge_id": "medical",
  "query": "高血压怎么治",
  "retrieval_setting": {
    "top_k": 5,
    "score_threshold": 0.3
  },
  "metadata_condition": {
    "logical_operator": "and",
    "conditions": [
      {"name": ["title"], "comparison_operator": "contains", "value": "指南"}
    ]
  }
}
```

| 字段 | 类型 | 必填 | 默认 | 范围 |
|---|---|---|---|---|
| `knowledge_id` | str | ✅ | - | = 库的 slug |
| `query` | str | ✅ | - | 非空 |
| `retrieval_setting.top_k` | int |  | 5 | 1 ~ 100 |
| `retrieval_setting.score_threshold` | float |  | 0.0 | 0.0 ~ 1.0 |
| `metadata_condition` | object? |  | null | 见下 |

未知字段会被忽略（不报错），保证 Dify 升级 spec 时向后兼容。

## 响应 schema

```json
{
  "records": [
    {
      "content": "高血压用药一般遵循阶梯治疗原则……",
      "score": 0.87,
      "title": "高血压用药指南",
      "metadata": {
        "document_id": "ce4e…",
        "chunk_id": "8d1f…",
        "author": "WHO",
        "year": 2024
      }
    }
  ]
}
```

字段名严格匹配 Dify 期望：`records` / `content` / `score` / `title` / `metadata`。

- 当没有命中：`{"records": []}`
- `metadata` 里包含 chunk 维度的 `document_id` 与 `chunk_id`，外加摄入时传的业务 metadata 字段（展开）

## metadata_condition 详细

`logical_operator`：`and` / `or`

`conditions` 列表，每项：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | `list[str]` | 字段名（数组：任一字段匹配即可） |
| `comparison_operator` | str | 见下 |
| `value` | str / int / float / bool / null | 比较值 |

### 支持的算子

| 算子 | Qdrant 等价 | 备注 |
|---|---|---|
| `=`, `eq` | `match.value` | 精确等值 |
| `!=`, `ne` | `match.except` | 不等于 |
| `contains` | `match.text` | 文本包含（payload 字段必须建过 text 索引） |
| `in` | `match.any` | `value` 必须是数组 |
| `is null` / `empty` | `is_empty` | |
| `is not null` / `not empty` | `is_not_empty` | |

**未识别的算子静默跳过**（不会报 400），其他条件正常生效。这样 Dify 升级算子时本服务不会立刻挂掉。

### 示例

匹配 `title` 包含「指南」且 `year >= 2020`？年份范围 Qdrant 支持但 Dify 当前 spec 没有 `>=`，可以用 `in` 列出：

```json
{
  "logical_operator": "and",
  "conditions": [
    {"name": ["title"], "comparison_operator": "contains", "value": "指南"},
    {"name": ["year"], "comparison_operator": "in", "value": [2020, 2021, 2022, 2023, 2024]}
  ]
}
```

## 错误响应

| 状态 | 含义 |
|---|---|
| 200 | 正常（哪怕 records 是空数组） |
| 401 | API Key 无效 / 过期 / 撤销 |
| 403 | 库不存在或当前用户在该库无 `read` 权限（统一 403，避免库存在性枚举） |
| 422 | 请求 body 不合 schema |
| 500 | embedding 服务挂了 / Qdrant 挂了，看 server 日志 |

## 性能

单次 `/retrieval` 的耗时分解（典型）：

| 步骤 | 耗时（参考） |
|---|---|
| Casbin enforce（内存） | < 1ms |
| 查库元数据（PG） | < 5ms |
| Embed 查询（bge-m3 HTTP） | 30-100ms |
| Qdrant search | 10-30ms |
| 序列化 + 返回 | < 5ms |
| **合计** | **50-150ms** |

Embed 是单点瓶颈；要并发 100+ QPS 时建议把 bge-m3 服务水平扩。

## 客户端示例

### cURL

```bash
curl -X POST http://localhost:8100/retrieval \
  -H "Authorization: Bearer vk_xxxxxxxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"knowledge_id":"medical","query":"高血压怎么治","retrieval_setting":{"top_k":3}}'
```

### Python

```python
import httpx

async with httpx.AsyncClient() as client:
    resp = await client.post(
        "http://localhost:8100/retrieval",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "knowledge_id": "medical",
            "query": "高血压怎么治",
            "retrieval_setting": {"top_k": 5, "score_threshold": 0.3},
        },
    )
    records = resp.json()["records"]
```

### Dify 配置

「设置」→「知识库」→「外部知识库」：

- **API Endpoint**：`http://<your-host>:8100/retrieval`
- **API Key**：alice 在「我的 API Key」生成的明文
- **Knowledge ID**：库的 slug，如 `medical`

> Dify 在调用前会做一次 schema 探测，确保你的服务返回了正确的 `{records:[…]}` 结构。本项目的响应字段严格匹配。

## 测试覆盖

- `tests/test_dify_contract.py` —— 请求接受最小 / 完整 payload，响应字段名严格匹配
- `tests/test_retrieval_filter.py` —— `metadata_condition` 各算子 → Qdrant filter 的映射正确

不依赖真实 Qdrant；只验证 schema 与映射逻辑。

## 不支持 / 未来扩展

| 功能 | 状态 | 路线图 |
|---|---|---|
| reranker（cross-encoder 精排） | ❌ | P5；可加 `bge-reranker-v2-m3` 在 services/retrieval.py 末段 |
| 混合检索（向量 + BM25） | ❌ | 需要 Qdrant `sparse_vectors` 或外接 ES |
| 查询改写 / HyDE | ❌ | 上游 RAG 框架（Dify）负责 |
| 流式响应 | ❌ | Dify spec 不要求 |
