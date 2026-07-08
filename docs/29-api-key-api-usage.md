# API Key API 接入说明

本文给外部系统、脚本、其他 AI / Agent 或外部开发者使用。你可以把这份文档单独发给接入方。

最重要的结论：API Key 默认用于调用知识库**检索接口**：

```http
POST /libraries/{LIBRARY_ID}/query
Authorization: Bearer <API_KEY>
```

这个接口返回的是 `results` 检索切片，不是后端直接生成的最终 `answer`。如果你需要最终自然语言回答，请把 `results` 交给你自己的 LLM，由调用方自己生成答案。

---

## 1. 最常用：API Key 调用知识库检索接口

### 接口

```http
POST /libraries/{LIBRARY_ID}/query
```

### 鉴权

```http
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

### 请求体示例

```json
{
  "query": "你的问题",
  "limit": 5
}
```

### 返回什么

成功响应返回 `results`：

```json
{
  "results": [
    {
      "text": "检索到的知识片段正文",
      "similarity": 0.82,
      "document_id": "文档 ID",
      "chunk_id": "切片 ID",
      "title": "来源文档标题",
      "metadata": {
        "vector_score": 0.82,
        "rerank_score": 0.91
      }
    }
  ]
}
```

注意：

- `/query` 返回的是检索切片列表 `results`。
- `/query` **不直接返回最终回答**，不会默认生成 `answer`。
- `results` 已经带来源标识，可用于引用展示、来源定位和后续 LLM 上下文拼接。

---

## 2. `LIBRARY_ID` 实际填什么

文档和示例里的 `LIBRARY_ID` 实际上填的是知识库的 **slug / 库唯一ID**。

例如管理后台里知识库唯一 ID 是：

```text
deploy_acceptance_server
```

那么接口路径就是：

```http
POST /libraries/deploy_acceptance_server/query
```

它不是数据库自增 ID，也不是隐藏主键。

---

## 3. 推荐配置方式：`.env`

推荐把知识库地址、知识库 slug、API Key 和可选的调用方 LLM 配置放到脚本同目录的 `.env` 文件中，不要写死在代码里。

```dotenv
VECTOR_KB_BASE_URL=http://10.0.10.2:8100
VECTOR_KB_LIBRARY_ID=deploy_acceptance_server
VECTOR_KB_API_KEY=你的完整API_KEY

# 可选：仅当你要在调用方脚本里自己接 LLM 时填写
# 这些变量不会被知识库后端读取
LOCAL_LLM_BASE_URL=可选
LOCAL_LLM_API_KEY=可选
LOCAL_LLM_MODEL=可选
```

说明：

- `VECTOR_KB_BASE_URL`：知识库服务地址。
- `VECTOR_KB_LIBRARY_ID`：目标知识库的 slug / 库唯一ID。
- `VECTOR_KB_API_KEY`：在“我的 API Key”页面创建后复制的完整 Key，仅显示一次。
- `LOCAL_LLM_*`：调用方自己的模型服务配置。知识库后端不会读取这些变量。

安装 Python 依赖：

```bash
pip install requests python-dotenv
```

---

## 4. Python 推荐接入脚本

下面脚本的默认行为是：调用 `/query`，打印检索切片和来源字段。它不是最终问答接口。

如果你需要最终回答，请把 `call_your_llm(prompt)` 替换成你自己的 LLM 调用，然后开启脚本里的 `use_llm=True`。这里的 `use_llm` 只是调用方脚本里的本地流程开关，**不是后端接口参数**。

```python
from pathlib import Path
from typing import Any
import os

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

VECTOR_KB_BASE_URL = os.getenv("VECTOR_KB_BASE_URL", "").rstrip("/")
VECTOR_KB_LIBRARY_ID = os.getenv("VECTOR_KB_LIBRARY_ID", "")  # 知识库 slug / 库唯一ID
VECTOR_KB_API_KEY = os.getenv("VECTOR_KB_API_KEY", "")

LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "")
LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "")


def require_env(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"请在 .env 中配置 {name}")
    return value


def auth_headers() -> dict[str, str]:
    api_key = require_env("VECTOR_KB_API_KEY", VECTOR_KB_API_KEY)
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def search_kb(question: str, limit: int = 5) -> list[dict[str, Any]]:
    """调用知识库 /query，返回 results 检索切片，不是最终 answer。"""
    base_url = require_env("VECTOR_KB_BASE_URL", VECTOR_KB_BASE_URL)
    library_id = require_env("VECTOR_KB_LIBRARY_ID", VECTOR_KB_LIBRARY_ID)

    response = requests.post(
        f"{base_url}/libraries/{library_id}/query",
        headers=auth_headers(),
        json={"query": question, "limit": limit},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def get_chunk_source(document_id: str, chunk_id: str | None = None) -> dict[str, Any]:
    """可选扩展：根据 document_id + chunk_id 查看命中切片在原文中的位置/窗口。"""
    base_url = require_env("VECTOR_KB_BASE_URL", VECTOR_KB_BASE_URL)
    library_id = require_env("VECTOR_KB_LIBRARY_ID", VECTOR_KB_LIBRARY_ID)

    params = {"chunk_id": chunk_id} if chunk_id else None
    response = requests.get(
        f"{base_url}/libraries/{library_id}/documents/{document_id}/source",
        headers=auth_headers(),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def build_context(chunks: list[dict[str, Any]]) -> str:
    """把检索切片拼成调用方 LLM 可读的上下文。"""
    parts = []
    for index, chunk in enumerate(chunks, start=1):
        title = chunk.get("title") or "未知来源"
        text = chunk.get("text") or ""
        similarity = chunk.get("similarity")
        document_id = chunk.get("document_id")
        chunk_id = chunk.get("chunk_id")
        metadata = chunk.get("metadata") or {}
        parts.append(
            f"[来源 {index}]\n"
            f"标题：{title}\n"
            f"相关性：{similarity}\n"
            f"document_id：{document_id}\n"
            f"chunk_id：{chunk_id}\n"
            f"metadata：{metadata}\n"
            f"正文：\n{text}"
        )
    return "\n\n---\n\n".join(parts)


def call_your_llm(prompt: str) -> str:
    """占位函数：请替换为你自己的大模型调用。"""
    # 你可以在这里调用自己的模型服务，例如 Ollama、vLLM、LM Studio、
    # OpenAI 兼容接口或公司内部模型网关。
    # 建议从 .env 读取 LOCAL_LLM_BASE_URL / LOCAL_LLM_API_KEY / LOCAL_LLM_MODEL，
    # 不要把大模型 API Key 写死在代码里。
    raise NotImplementedError("请将 call_your_llm(prompt) 替换为自己的模型调用")


def ask(question: str, limit: int = 5, use_llm: bool = False):
    chunks = search_kb(question, limit=limit)

    if not use_llm:
        # 返回的是检索切片 results，不是最终 answer。
        return chunks

    context = build_context(chunks)
    prompt = f"""请只根据以下知识库检索切片回答问题。若切片中没有答案，请说明无法从已给资料确认。

问题：{question}

知识库检索切片：
{context}
"""
    answer = call_your_llm(prompt)
    sources = [
        {
            "title": chunk.get("title"),
            "document_id": chunk.get("document_id"),
            "chunk_id": chunk.get("chunk_id"),
            "similarity": chunk.get("similarity"),
            "metadata": chunk.get("metadata"),
        }
        for chunk in chunks
    ]
    return {"answer": answer, "sources": sources}


if __name__ == "__main__":
    question = "你的问题"

    print("=== use_llm=False：只返回 /query 的 results 检索切片，不是最终回答 ===")
    chunks = ask(question, limit=5, use_llm=False)
    for index, item in enumerate(chunks, start=1):
        print(f"--- 结果 {index} ---")
        print("title：", item.get("title"))
        print("document_id：", item.get("document_id"))
        print("chunk_id：", item.get("chunk_id"))
        print("similarity：", item.get("similarity"))
        print("metadata：", item.get("metadata"))
        print("text：", item.get("text"))
        print()

    # 可选：根据第一条结果继续查看命中切片在原文中的位置/窗口
    # if chunks and chunks[0].get("document_id"):
    #     source = get_chunk_source(chunks[0]["document_id"], chunks[0].get("chunk_id"))
    #     print("=== /source 原文定位 ===")
    #     print(source)

    # 替换 call_your_llm(prompt) 后，再打开下面两行：
    # print("=== use_llm=True：调用方自己生成 answer + sources ===")
    # print(ask(question, limit=5, use_llm=True))
```

---

## 5. 是否经过 LLM

必须区分两层：

| 模式 | 是否经过后端 LLM | 行为 | 返回 |
|---|---:|---|---|
| 默认文档模式 | 否 | 只调用 `POST /libraries/{LIBRARY_ID}/query` | `results` 检索切片列表 |
| 调用方自己接 LLM | 否，仍不经过后端 LLM | 先 `/query` 获取切片，再由调用方把切片交给自己的 LLM | 调用方自行组织，例如 `{ "answer": "...", "sources": [...] }` |

说明：

- `/query` 不是“后端直接问答接口”。
- `/query` 不会默认返回 `answer`。
- Python 示例里的 `use_llm=True` 是调用方脚本里的本地流程，不是后端参数。
- `call_your_llm(prompt)` 必须由接入方替换为自己的大模型调用。
- 知识库服务不会读取你的 `LOCAL_LLM_API_KEY`，也不会因为你在请求体里传 `use_llm` 就生成回答。

---

## 6. 返回字段说明

| 字段 | 说明 | 常见用途 |
|---|---|---|
| `results[].text` | 检索切片正文。若知识库开启了全文源补全，它可能是回查 PGSQL 全文源后补全过的正文。 | 展示摘要；拼接给调用方 LLM |
| `results[].similarity` | 相似度/相关性分数；开启 rerank 时可能是重排后的分数。 | 排序、调试召回质量 |
| `results[].document_id` | 来源文档 ID。 | 后续调用 `/source`、`/source/full`、`/file` |
| `results[].chunk_id` | 来源切片 ID。 | 定位具体命中切片 |
| `results[].title` | 来源文档标题。 | 引用展示、结果列表标题 |
| `results[].metadata` | 额外元数据，例如向量分数、重排分数或文档相关字段。 | 调试、筛选、展示补充信息 |

当前 API Key 检索结果本身已经带来源标识：`title` / `document_id` / `chunk_id` / `metadata`。

---

## 7. 引用 / 来源定位 / 原文 / 原文件

如果你只需要展示“这条结果来自哪里”，通常直接使用 `/query` 返回的：

- `title`
- `document_id`
- `chunk_id`
- `metadata`

如果你还需要更进一步查看来源，可以继续调用下面接口。

### 7.1 查看命中切片在原文中的位置/窗口

```http
GET /libraries/{slug}/documents/{document_id}/source?chunk_id={chunk_id}
Authorization: Bearer <API_KEY>
```

用途：根据 `document_id + chunk_id` 查看命中切片在原文中的位置、上下文窗口或定位信息。适合做“引用展开”“跳到原文附近”。

### 7.2 查看该文档完整归一化原文

```http
GET /libraries/{slug}/documents/{document_id}/source/full
Authorization: Bearer <API_KEY>
```

用途：查看该文档的完整归一化文本。适合做“查看全文”。

### 7.3 下载原始文件

```http
GET /libraries/{slug}/documents/{document_id}/file
Authorization: Bearer <API_KEY>
```

用途：下载该文档最初上传的原始文件。适合做“下载附件 / 原文件”。

这里的 `{slug}` 与前文 `LIBRARY_ID` 是同一个含义：知识库 slug / 库唯一ID。

---

## 8. 全文源补全（source enrichment）是什么

大白话说明：有些知识库的向量库里只保存了较短文本或外键，真正完整正文在 PGSQL 表里。管理员可以在知识库层面开启“全文源补全”。开启后：

- 调 `/query` 时，系统仍先做知识库检索。
- 命中结果如果能关联到 PGSQL 全文源，会自动回查并补全正文。
- 因此 `results[].text` 可能不是纯向量库原始 payload，而是补全后的正文。

重要边界：

- 这是**知识库级配置**，由管理员在新建/编辑知识库时控制。
- 新建知识库默认开启。
- 普通 API Key 调用方不能在单次 `/query` 请求里传参数动态开启或关闭。
- 请求体里不要传类似 `source_enrichment_enabled`、`use_source_enrichment` 之类参数；这不是请求级能力。

---

## 9. 使用判断表

| 你想做什么 | 应该怎么做 |
|---|---|
| 我只想拿切片做搜索展示 | 调 `POST /libraries/{LIBRARY_ID}/query`，展示 `results` |
| 我想自己接 LLM 生成最终回答 | 先调 `/query`，再把 `results` 拼成上下文交给你自己的 LLM |
| 我想展示引用来源 | 使用 `results[].document_id` / `results[].chunk_id` / `results[].title` / `results[].metadata` |
| 我想定位命中切片在原文中的位置 | 调 `GET /libraries/{slug}/documents/{document_id}/source?chunk_id={chunk_id}` |
| 我想看某文档完整归一化原文 | 调 `GET /libraries/{slug}/documents/{document_id}/source/full` |
| 我想下载原始文件 | 调 `GET /libraries/{slug}/documents/{document_id}/file` |
| 我想按单次请求开关全文源补全 | 当前不支持；全文源补全是知识库级开关 |
| 我想用 API Key 创建/管理 API Key | 当前说明不覆盖；API Key 不应用于创建/管理 API Key |

---

## 10. JavaScript/Node 简版

适合只取检索切片的 Node 脚本。它返回 `results`，不伪装成最终问答接口。

```javascript
const VECTOR_KB_BASE_URL = process.env.VECTOR_KB_BASE_URL;
const VECTOR_KB_LIBRARY_ID = process.env.VECTOR_KB_LIBRARY_ID; // 知识库 slug / 库唯一ID
const VECTOR_KB_API_KEY = process.env.VECTOR_KB_API_KEY;

async function searchKb(question, limit = 5) {
  const response = await fetch(`${VECTOR_KB_BASE_URL}/libraries/${VECTOR_KB_LIBRARY_ID}/query`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${VECTOR_KB_API_KEY}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ query: question, limit })
  });

  if (!response.ok) {
    throw new Error(`知识库请求失败：${response.status}`);
  }

  const data = await response.json();
  return data.results || [];
}

searchKb("你的问题", 5).then((results) => {
  for (const item of results) {
    console.log({
      title: item.title,
      document_id: item.document_id,
      chunk_id: item.chunk_id,
      similarity: item.similarity,
      metadata: item.metadata,
      text: item.text
    });
  }
});
```

---

## 11. curl 仅用于临时测试

`curl` 适合临时验证 API Key、知识库 slug 和网络连通性，不建议作为生产集成方式。

```bash
curl -X POST "$VECTOR_KB_BASE_URL/libraries/$VECTOR_KB_LIBRARY_ID/query" \
  -H "Authorization: Bearer $VECTOR_KB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"你的问题","limit":5}'
```

用途：确认这三件事是否正确：

- API Key 能鉴权。
- `VECTOR_KB_LIBRARY_ID` 填的是正确的知识库 slug。
- `/query` 能返回 `results`。

---

## 12. 常见错误

| HTTP 状态 | 常见原因 | 处理建议 |
|---:|---|---|
| 401 | API Key 缺失、错误或已失效 | 检查 `Authorization: Bearer <VECTOR_KB_API_KEY>` |
| 403 | 当前账号没有目标知识库权限 | 在权限管理中授予对应知识库 read 权限 |
| 404 | 知识库或资源不存在 | 检查 `VECTOR_KB_LIBRARY_ID` / `slug` 和路径 |
| 413 | 上传文件超过大小限制 | 压缩或拆分文件后重试 |
| 415 | 文件类型不支持 | 使用支持的文件格式 |
| 503 | 知识库正在重建或服务暂不可用 | 稍后重试 |

---

## 13. 常见误区

- `/query` 不直接返回最终回答；它返回 `results` 检索切片。
- `use_llm=True` 不是后端参数，只是示例脚本里的本地流程开关。
- `LIBRARY_ID` 填的是知识库 slug / 库唯一ID，不是数据库自增 ID。
- 全文源补全不是调用方按次开关；它是管理员配置的知识库级能力。
- API Key 不应用于创建/管理 API Key；请在管理后台创建、查看前缀、撤销 API Key。
- 不要把“调用方自己接 LLM”理解成“知识库后端会自动回答”。

安全建议：

- 不要在前端页面、URL、日志或截图中暴露真实 API Key。
- 不要把 API Key、LLM API Key 或真实 `.env` 文件提交到 Git 仓库。
- 为不同系统创建不同 API Key，便于审计和撤销。
- 不再使用的 API Key 应及时撤销。
