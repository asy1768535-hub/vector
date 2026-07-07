# API Key API 接入说明

本文是给外部系统、脚本或 AI Agent 使用的接入模板。知识库后端只提供检索能力：`POST /libraries/{LIBRARY_ID}/query` 返回召回切片，不生成最终回答，也不新增任何后端接口。

## 1. 适用场景：输入问题，返回知识库召回切片

适用于这些调用方：

- 外部业务系统：把用户问题发给知识库，拿回相关切片后自行展示或继续处理。
- 本地脚本：批量验证某个知识库的检索效果。
- AI Agent：先检索企业知识库切片，再把切片作为上下文交给自己的 LLM。

核心流程只有一步：

```http
POST /libraries/{LIBRARY_ID}/query
```

请求体：

```json
{
  "query": "你的问题",
  "limit": 5
}
```

返回值中的 `results` 就是召回切片。`/query` 返回切片，不是最终回答；如需最终回答，请调用方将切片交给自己的 LLM。

## 2. 推荐配置方式：`.env`

推荐把知识库地址、知识库 ID、API Key 和可选的本地大模型配置放到脚本同目录的 `.env` 文件中，不要写死在代码里。

```dotenv
VECTOR_KB_BASE_URL=http://10.0.10.2:8100
VECTOR_KB_LIBRARY_ID=deploy_acceptance_server
VECTOR_KB_API_KEY=你的完整API_KEY

# 可选：仅 use_llm=True 且要调用自己的本地/私有大模型时填写
LOCAL_LLM_BASE_URL=可选
LOCAL_LLM_API_KEY=可选
LOCAL_LLM_MODEL=可选
```

说明：

- `VECTOR_KB_BASE_URL`：知识库服务地址。
- `VECTOR_KB_LIBRARY_ID`：目标知识库 ID。
- `VECTOR_KB_API_KEY`：在“我的 API Key”页面创建后复制的完整 Key，仅显示一次。
- `LOCAL_LLM_*`：调用方自己的模型服务配置，知识库后端不会读取这些变量。

安装 Python 依赖：

```bash
pip install requests python-dotenv
```

## 3. Python 推荐接入脚本

把下面脚本保存为 `kb_client.py`，并在同目录创建 `.env`。默认 `use_llm=False`，只返回知识库召回切片；需要最终回答时，把 `call_your_llm(prompt)` 替换为自己的模型调用后再开启 `use_llm=True`。

```python
from pathlib import Path
from typing import Any
import os

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

VECTOR_KB_BASE_URL = os.getenv("VECTOR_KB_BASE_URL", "").rstrip("/")
VECTOR_KB_LIBRARY_ID = os.getenv("VECTOR_KB_LIBRARY_ID", "")
VECTOR_KB_API_KEY = os.getenv("VECTOR_KB_API_KEY", "")

LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "")
LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "")


def require_env(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"请在 .env 中配置 {name}")
    return value


def search_kb(question: str, limit: int = 5) -> list[dict[str, Any]]:
    """调用知识库 /query，返回 results 召回切片。"""
    base_url = require_env("VECTOR_KB_BASE_URL", VECTOR_KB_BASE_URL)
    library_id = require_env("VECTOR_KB_LIBRARY_ID", VECTOR_KB_LIBRARY_ID)
    api_key = require_env("VECTOR_KB_API_KEY", VECTOR_KB_API_KEY)

    response = requests.post(
        f"{base_url}/libraries/{library_id}/query",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"query": question, "limit": limit},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def build_context(chunks: list[dict[str, Any]]) -> str:
    """把召回切片拼成 LLM 可读的上下文。"""
    parts = []
    for index, chunk in enumerate(chunks, start=1):
        title = chunk.get("title") or "未知来源"
        text = chunk.get("text") or ""
        similarity = chunk.get("similarity")
        document_id = chunk.get("document_id")
        chunk_id = chunk.get("chunk_id")
        parts.append(
            f"[来源 {index}]\n"
            f"标题：{title}\n"
            f"相关性：{similarity}\n"
            f"document_id：{document_id}\n"
            f"chunk_id：{chunk_id}\n"
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
        return chunks

    context = build_context(chunks)
    prompt = f"""请只根据以下知识库切片回答问题。若切片中没有答案，请说明无法从已给资料确认。

问题：{question}

知识库切片：
{context}
"""
    answer = call_your_llm(prompt)
    sources = [
        {
            "title": chunk.get("title"),
            "document_id": chunk.get("document_id"),
            "chunk_id": chunk.get("chunk_id"),
            "similarity": chunk.get("similarity"),
        }
        for chunk in chunks
    ]
    return {"answer": answer, "sources": sources}


if __name__ == "__main__":
    question = "你的问题"

    print("=== use_llm=False：只返回 results 切片 ===")
    chunks = ask(question, limit=5, use_llm=False)
    for item in chunks:
        print("来源文档：", item.get("title"))
        print("相关性：", item.get("similarity"))
        print("切片正文：", item.get("text"))
        print()

    # 替换 call_your_llm(prompt) 后，再打开下面两行：
    # print("=== use_llm=True：返回 answer + sources ===")
    # print(ask(question, limit=5, use_llm=True))
```

## 4. `use_llm` 开关说明

`use_llm` 是调用方脚本里的开关，不是知识库后端接口参数。

| 模式 | 行为 | 返回 |
|---|---|---|
| `use_llm=False` | 只调用 `/libraries/{LIBRARY_ID}/query` | `results` 切片列表 |
| `use_llm=True` | 先调用 `/query` 获取切片，再把 `build_context(chunks)` 的结果交给调用方自己的 LLM | `{ "answer": "...", "sources": [...] }` |

`call_your_llm(prompt)` 必须由接入方替换为自己的大模型调用。知识库服务不会保存或使用你的 `LOCAL_LLM_API_KEY`，也不会提供“生成最终回答”的新接口。

## 5. JavaScript/Node 简版

适合只取切片的 Node 脚本。生产接入仍建议像 Python 示例一样从 `.env` 或安全配置中心读取变量，不要把 API Key 写死在代码里。

```javascript
const VECTOR_KB_BASE_URL = process.env.VECTOR_KB_BASE_URL;
const VECTOR_KB_LIBRARY_ID = process.env.VECTOR_KB_LIBRARY_ID;
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
  return (await response.json()).results || [];
}
```

## 6. curl 仅用于临时测试

`curl` 适合临时验证 API Key、知识库 ID 和网络连通性，不建议作为生产接入方式。

```bash
curl -X POST "$VECTOR_KB_BASE_URL/libraries/$VECTOR_KB_LIBRARY_ID/query" \
  -H "Authorization: Bearer $VECTOR_KB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"你的问题","limit":5}'
```

## 7. 响应字段说明

成功响应示例：

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

| 字段 | 说明 |
|---|---|
| `results[].text` | 召回切片正文 |
| `results[].similarity` | 相似度/相关性分数；开启 rerank 时可能是重排后的分数 |
| `results[].document_id` | 来源文档 ID |
| `results[].chunk_id` | 来源切片 ID |
| `results[].title` | 来源文档标题 |
| `results[].metadata` | 额外元数据，例如向量分数、重排分数等 |

## 8. 常见错误

| HTTP 状态 | 常见原因 | 处理建议 |
|---:|---|---|
| 401 | API Key 缺失、错误或已失效 | 检查 `Authorization: Bearer <VECTOR_KB_API_KEY>` |
| 403 | 当前账号没有目标知识库权限 | 在权限管理中授予对应知识库 read 权限 |
| 404 | 知识库或资源不存在 | 检查 `VECTOR_KB_LIBRARY_ID` 和路径 |
| 413 | 上传文件超过大小限制 | 压缩或拆分文件后重试 |
| 415 | 文件类型不支持 | 使用支持的文件格式 |
| 503 | 知识库正在重建或服务暂不可用 | 稍后重试 |

安全建议：

- 不要在前端页面、URL、日志或截图中暴露真实 API Key。
- 不要把 API Key、LLM API Key 或真实 `.env` 文件提交到 Git 仓库。
- 为不同系统创建不同 API Key，便于审计和撤销。
- 不再使用的 API Key 应及时撤销。
