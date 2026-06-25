# 25 · 本地模型接入（Embedding / Rerank / OCR）

本篇说明本项目如何连接**本地** Embedding、Rerank、OCR 三类能力：接口契约、配置项、启动后如何一键检查。**不绑定**任何具体运行框架——只要服务实现下述接口契约即可（Infinity / TEI / vLLM / Ollama 等都行，自行选型）。

## 一、一句话

- Embedding、Rerank 走 HTTP 接口契约（OpenAI 兼容 `/v1/embeddings`、标准 `/rerank`），**本地服务无需 API Key**。
- OCR 用进程内 RapidOCR（onnxruntime），无外部服务、无 Key。
- 配好后用 `scripts/check_local_ai_services.py` 一键自检；运行期看 `/health`。

## 二、接口契约

### 1. Embedding —— OpenAI 兼容 `/v1/embeddings`

- 默认模型 `bge-m3`，默认维度 **1024**（`EMBEDDING_DIM` 必须等于模型实际输出维度，否则写 Qdrant / 检索必失败）。
- 请求（项目发出）：
  ```json
  POST {EMBEDDING_BASE_URL}
  { "model": "bge-m3", "input": ["文本1", "文本2"] }
  ```
- 响应（服务返回，OpenAI 格式）：
  ```json
  { "data": [ {"embedding": [/* 1024 floats */]}, {"embedding": [/* ... */]} ] }
  ```
- 本地无需鉴权；`EMBEDDING_API_KEY` 留空即不发 `Authorization` 头。

### 2. Rerank —— 标准 `/rerank`

- 默认模型 `bge-reranker-v2-m3`。与 Infinity / TEI / Jina / Cohere 的 `/rerank` 结构一致。
- 请求：
  ```json
  POST {RERANK_BASE_URL}
  { "model": "bge-reranker-v2-m3", "query": "...", "documents": ["...", "..."], "top_n": 3 }
  ```
- 响应：
  ```json
  { "results": [ {"index": 1, "relevance_score": 0.93}, {"index": 0, "relevance_score": 0.41} ] }
  ```
- 本地无需鉴权；`RERANK_API_KEY` 留空（留空时回退 `EMBEDDING_API_KEY`，本地两者都空）。
- 默认关闭：`RERANK_ENABLED=false`；配好地址 + 模型后置 true，或在某个库上开 `rerank_enabled`。

### 3. OCR —— 进程内 RapidOCR

- 无外部服务：`pip install -e ".[ocr]"`（含 rapidocr_onnxruntime / pypdfium2 / Pillow）。
- 按库 `ocr_enabled` 生效；首次使用懒加载并初始化引擎（较重，仅一次）。

## 三、配置项（`.env`，详见 `.env.example` 与 `app/config.py`）

| 配置 | 默认 | 说明 |
|------|------|------|
| `EMBEDDING_BASE_URL` | `http://<host>:8111/v1/embeddings` | 本地 OpenAI 兼容 embeddings 端点 |
| `EMBEDDING_MODEL` | `bge-m3` | |
| `EMBEDDING_DIM` | `1024` | 必须与模型实际维度一致 |
| `EMBEDDING_API_KEY` | 空 | 本地留空 |
| `RERANK_PROVIDER` | `standard` | `standard` 本地 / `dashscope` 备用 |
| `RERANK_ENABLED` | `false` | 配好本地 reranker 后再开 |
| `RERANK_BASE_URL` | 空 | 本地示例 `http://<host>:9000/rerank` |
| `RERANK_MODEL` | （空，建议 `bge-reranker-v2-m3`） | |
| `RERANK_API_KEY` | 空 | 本地留空 |

> DashScope（阿里云）配置在 `.env.example` 中以**备用（非默认）**形式保留：仅在本地服务不可用时临时切换，需 `sk-` Key 且账户余额（欠费会同时瘫 embedding + rerank）。

## 四、启动后检查

### 1. 一键自检脚本（推荐，默认不访问云）

```bash
python scripts/check_local_ai_services.py
```
检查并逐项打印耗时：Embedding 连通性 / 模型 / 返回向量数 / 维度是否 1024；Rerank 请求-响应结构与排序合法性；RapidOCR 本地初始化。**任一失败返回非零退出码**，不打印 API Key。

- 默认从 `.env` 读取地址，但**命中云地址会被拒绝并判失败**（保证默认不打云）；确需用云加 `--allow-cloud`。
- 常用：`--embedding-url`、`--rerank-url`、`--rerank-model`、`--skip-rerank`、`--skip-ocr`。
- 仅查本地（忽略 `.env` 里的云地址）：显式传 `--embedding-url http://<host>:8111/v1/embeddings` 等。

### 2. 运行期健康检查

```bash
curl -s http://127.0.0.1:8100/health
```
返回字段含 `embedding`(ok/fail) + `embedding_model`/`embedding_dim`、`rerank`(off/ok/fail)、`ocr`(off/ok/missing)。启动时 worker / API 也会打印 `[selfcheck]` banner（见 `app/services/selfcheck.py`）。

## 五、待定 / 选型留给部署方

- 具体推理框架（Infinity / TEI / vLLM / Ollama 等）与本地端口、显存/批大小，由部署方按硬件定；本项目只依赖上面的接口契约。
- 本地 reranker 的实际地址/端口（`RERANK_BASE_URL`）需部署方确认后填入。
- 从云切回本地（或反之）会改变向量空间/排序行为；切换 embedding 模型需重建已有库（见 docs/20）。
