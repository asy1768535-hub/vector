# API Key API 接入说明

本文重点说明外部系统、脚本或 AI Agent 如何通过 API Key 调用知识库“检索切片 API”。示例只使用占位符，不包含真实 API Key。

## 1. 占位符

后续示例统一使用：

```text
BASE_URL   = 知识库服务地址，例如 https://your-domain.example.com
LIBRARY_ID = 知识库标识
API_KEY    = 你在“我的 API Key”页面创建并保存的密钥
```

> 不要把 API Key 写在 URL 参数里；请始终放在 `Authorization` 请求头中。

## 2. 认证方式

所有需要身份权限的接口都使用 Bearer Token：

```http
Authorization: Bearer <API_KEY>
```

注意：

- `Bearer` 后面有一个空格。
- API Key 具有当前账号对应的权限；没有知识库 read 权限时不能检索。
- 完整 API Key 只在创建时显示一次，请立即复制并妥善保存。

## 3. 检索知识库切片

外部系统调用检索接口时，系统会对 `query` 做向量化、检索，并可经过 rerank 重排，返回相关文本切片。该接口返回的是“检索切片”，不是大模型最终回答；调用方可自行把切片交给自己的 LLM 生成回答。

### 请求

```http
POST /libraries/{LIBRARY_ID}/query
```

### 请求头

```http
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

### 请求体

```json
{
  "query": "你的问题",
  "limit": 5
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| query | string | 是 | 用户问题或检索词 |
| limit | integer | 否 | 返回切片数量，范围 1-20，常用值为 5 |

### curl 示例

```bash
curl -X POST "BASE_URL/libraries/LIBRARY_ID/query" \
  -H "Authorization: Bearer API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"你的问题","limit":5}'
```

### 成功响应示例

```json
{
  "results": [
    {
      "text": "检索到的知识片段正文",
      "similarity": 0.82,
      "document_id": "文档 ID",
      "chunk_id": "分片 ID",
      "title": "来源文档标题",
      "metadata": {
        "vector_score": 0.82,
        "rerank_score": 0.91
      }
    }
  ]
}
```

响应字段重点：

| 字段 | 说明 |
|---|---|
| results[].text | 召回切片正文 |
| results[].similarity | 相似度/相关性分数；开启 rerank 时可能是重排后的分数 |
| results[].document_id | 来源文档 ID |
| results[].chunk_id | 来源切片 ID |
| results[].title | 来源文档标题 |
| results[].metadata | 额外元数据，例如向量分数、重排分数等 |

## 4. Python requests 示例

```python
import requests

BASE_URL = "BASE_URL"
LIBRARY_ID = "LIBRARY_ID"
API_KEY = "API_KEY"

url = f"{BASE_URL}/libraries/{LIBRARY_ID}/query"
headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}
payload = {
    "query": "你的问题",
    "limit": 5,
}

response = requests.post(url, headers=headers, json=payload, timeout=30)
response.raise_for_status()

for item in response.json()["results"]:
    print("来源文档：", item.get("title"))
    print("相关性：", item.get("similarity"))
    print("切片正文：", item.get("text"))
```

## 5. JavaScript fetch 示例

```javascript
const BASE_URL = "BASE_URL";
const LIBRARY_ID = "LIBRARY_ID";
const API_KEY = "API_KEY";

async function retrieveChunks(question) {
  const response = await fetch(`${BASE_URL}/libraries/${LIBRARY_ID}/query`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${API_KEY}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      query: question,
      limit: 5
    })
  });

  if (!response.ok) {
    throw new Error(`请求失败：${response.status}`);
  }
  return await response.json();
}
```

## 6. 可选：具备 insert 权限时上传文件

如果调用方账号对目标知识库具备 insert 权限，可使用文件上传接口导入文档。该能力不是检索切片的必要步骤，通常由管理后台或导入流程完成。

### 请求

```http
POST /libraries/{LIBRARY_ID}/import-file
```

### 请求头

```http
Authorization: Bearer <API_KEY>
```

文件上传使用 `multipart/form-data`，通常不要手动设置 `Content-Type`，由请求工具自动生成。

### curl 示例

```bash
curl -X POST "BASE_URL/libraries/LIBRARY_ID/import-file" \
  -H "Authorization: Bearer API_KEY" \
  -F "file=@./example.docx"
```

支持的常见格式包括：`.txt`、`.md`、`.markdown`、`.json`、`.csv`、`.pdf`、`.docx`、`.xlsx`。

## 7. 常见错误

| HTTP 状态 | 常见原因 | 处理建议 |
|---:|---|---|
| 401 | API Key 缺失、错误或已失效 | 检查 `Authorization: Bearer <API_KEY>` |
| 403 | 当前账号没有目标知识库权限 | 在权限管理中授予对应知识库 read 权限 |
| 404 | 知识库或资源不存在 | 检查 `LIBRARY_ID` 和路径 |
| 413 | 上传文件超过大小限制 | 压缩或拆分文件后重试 |
| 415 | 文件类型不支持 | 使用支持的文件格式 |
| 503 | 知识库正在重建或服务暂不可用 | 稍后重试 |

## 8. 安全建议

- 不要在前端页面、URL、日志或截图中暴露真实 API Key。
- 不要把 API Key 提交到 Git 仓库。
- 为不同系统创建不同 API Key，便于审计和撤销。
- 不再使用的 API Key 应及时撤销。
