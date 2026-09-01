# 09 · 文档摄入

## 接口

```http
POST /libraries/{slug}/documents       ← 需要该库 insert 权限
```

body：

```json
{
  "title": "高血压用药指南",
  "external_id": "guideline-2024-001",
  "text": "高血压是一种慢性病……",
  "splitter": "text",
  "metadata": {"author": "WHO", "year": 2024}
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | str | ✅ | 正文（plain / Markdown / JSON 字符串） |
| `title` | str? |  | 标题；会进 Qdrant payload，检索结果原样回填 |
| `external_id` | str? |  | 调用方 ID，用于幂等（同一 external_id 重提会覆盖？见下） |
| `splitter` | str |  | `text` (默认) / `markdown` / `none` |
| `metadata` | dict? |  | 自定义业务字段，会展开进 Qdrant payload，可用于 `metadata_condition` 过滤 |

响应：

```json
{
  "document_id": "ce4e…",
  "status": "pending",
  "chunk_count": 5,
  "job_id": "8d1f…"
}
```

## 切分策略

`services/splitter.py` 提供 3 种：

### `text`（默认）

`RecursiveCharacterTextSplitter`，分隔符层级：

```
"\n\n"  →  "\n"  →  "。"  →  "！"  →  "？"  →  ". "  →  "? "  →  "! "  →  " "  →  ""
```

中英文混排都能正确切。

### `markdown`

先按标题切（`MarkdownHeaderTextSplitter`，识别 `#` / `##` / `###` / `####`），再对每段长度超过 chunk_size 的二次走 `RecursiveCharacterTextSplitter`。

适合：技术文档、长篇 Markdown 报告。

### `none`

不切分，整段当一个 chunk。

适合：短问答、Slack 消息、Twitter 帖子。

> 切分参数 `chunk_size` / `chunk_overlap` 取自**库级配置**（`sys_libraries`），不接受请求级覆盖（避免乱）。

## 幂等

```
content_hash = sha256(text).hexdigest()

SELECT * FROM documents
  WHERE library_id = ? AND content_hash = ? AND deleted_at IS NULL
```

命中 → 直接返回已存在的 `document_id`，不重新切分 / 不入队。

> 用 `text` 本体的 hash 而不是 `external_id`，因为同一 external_id 完全可能要更新内容（业务可控）。当前实现：相同 text 跳过；不同 text 但同 external_id 会建一个新 document（external_id 不强唯一）。

> 如果你需要更严格的去重 / 更新语义，可以扩展：传 `external_id` 时先 `DELETE` 同 external_id 的旧 document 再插入新。改 `services/ingest.py:ingest_text()`。

## 写库 + 入队

```python
# services/ingest.py
1. 计算 content_hash
2. 幂等检查
3. splitter.split_text(text, chunk_size, chunk_overlap, splitter)
4. INSERT documents (status='pending')
5. INSERT chunks (id=uuid, seq, text, metadata)
6. INSERT embedding_jobs (status='pending')
7. COMMIT  → 接口返回
```

完成后接口立即返回 `pending`。后续由 worker 异步处理。

## 异步 Embed → Qdrant Upsert

详见 [11 Worker](./11-worker.md)。简略流程：

```
worker → CLAIM (FOR UPDATE SKIP LOCKED)
       → embed_texts([chunk.text for chunk in chunks])
       → qdrant.upsert_points(collection, [{
             id: str(chunk.id),
             vector: vec,
             payload: {
                 library_id, document_id, chunk_id, seq,
                 text, title, external_id,
                 ...metadata (展开)
             }
         }])
       → UPDATE embedding_jobs SET status='done', finished_at=now()
       → UPDATE documents     SET status='ready'
```

## 文档状态机

```
pending  ──→ processing ──→  ready
              │
              └──→ failed     (attempt_count >= max_attempts)
              │
              └──→ deleted    (软删)
```

| 状态 | 含义 |
|---|---|
| `pending` | 已入队，待 worker 抢锁 |
| `processing` | worker 正在 embed + upsert |
| `ready` | 完成，Qdrant 已可检索 |
| `failed` | 多次失败被弃；`last_error` 字段有原因 |
| `deleted` | 软删；Qdrant 对应 points 已异步清理 |

## 列表 / 详情 / 删除

```http
GET    /libraries/{slug}/documents?status=ready&limit=50
GET    /libraries/{slug}/documents/{doc_id}
DELETE /libraries/{slug}/documents/{doc_id}
```

- 列表支持 `status` / `external_id` 过滤
- 删除 = 软删 PG + BackgroundTasks 异步删 Qdrant points（按 payload.document_id 过滤）

## 失败排查

| 现象 | 看哪里 |
|---|---|
| 摄入直接 400 `text produced zero chunks` | text 是纯空白；或者 splitter=`text` 但内容里没有任何分隔符 |
| 文档一直 `pending` | worker 没起；`python -m app.workers.embedder --watch` |
| 文档变 `failed` | 看 `documents.last_error` 或 `embedding_jobs.last_error` |
| 检索查不到刚摄入的 | 文档状态是不是 `ready`？Qdrant payload 里 chunk_id 是不是写进去了？ |

## PDF 逐页文字层 / 扫描页 OCR

`/import-file` 上传 `.pdf` 时（`app/services/pdf_extract.py`），**逐页**判断：

1. `pypdf` 抽该页文字层，非空白字符数 **≥ `PDF_OCR_MIN_TEXT_CHARS`** → 直接用文字层。
2. 低于阈值（图片/扫描页）：
   - 库级 `ocr_enabled` **关** → 跳过该页（单张图片页不会让整份失败）；若整份都无文字 → `400`，提示去开启 OCR。
   - **开**但 OCR 依赖缺 → `400`，提示 `pip install -e ".[ocr]"`。
   - **开**且可用 → 仅对该页用 `pypdfium2` 渲染为 PNG（`PDF_OCR_RENDER_DPI`），交给现有 `ocr.ocr_image()` 识别。

各页按原页序合并，带 `【第 N 页】` 来源标记后作为一份文档（`splitter="text"`）入库。约束：

- 只对低文字页渲染，每次只持有一页 PNG；OCR 在异步 Import Worker 中执行。
- 真正进 OCR 的页数超过 **`PDF_OCR_MAX_PAGES`** 立即 `400`，不再渲染（防 CPU 跑飞）。
- 不还原图片表格行列结构、不做票据字段结构化、不接云 OCR。

> 文字版 PDF 行为与之前一致，不触发 OCR。参数见 [05 配置](./05-configuration.md)。

## 独立图片 OCR

异步导入支持 `.bmp/.jpeg/.jpg/.png/.tif/.tiff/.webp`。知识库必须开启
`ocr_enabled`，并安装 `.[ocr]` 依赖；否则任务会明确提示开启 OCR 或安装依赖。

RapidOCR 返回的每个文字区域都会生成 `image_region` 解析单元，保存识别文字、
像素坐标 `bbox`、置信度、父级段落和原文件 revision/SHA 绑定。OCR 文字按图片中的
识别顺序进入 normalized text、chunk 和后续向量/图谱链路。当前不生成视觉向量，
也不理解纯照片、图形关系或票据字段。原始文件名始终作为独立解析段进入 normalized
text 和 chunk；因此 OCR 结果错误时仍可按文件名检索，没有可识别文字时则生成仅含
文件名的文档，不生成伪造的 `image_region`。

## 异步大文件与文件夹上传

管理端通过 `POST /libraries/{slug}/import-sessions` 创建上传会话，随后按服务端返回的
分块大小调用 `PUT /libraries/{slug}/import-sessions/{job_id}/content`，并使用
`Upload-Offset` 续传。全部字节写入并 `fsync` 后，客户端调用
`POST /libraries/{slug}/import-sessions/{job_id}/complete`。

Complete 只有在文件大小、旧版 DOC 结构和 SHA-256 校验完成、任务提交为 `queued` 后
才返回 `202`。页面显示“已接收/排队”后可以关闭浏览器；Importer、Embedding、图谱
抽取和审核均由后台继续，不在上传请求内执行。`409` 或 `429` 响应可能带
`Upload-Offset` 与 `Retry-After`，重试必须以服务端已提交 offset 为准，避免重复字节。

## 批量摄入

当前没有专门的批量接口（spec 里规划过 `/documents:batch`，但首版未实现）。要批量插入：

- **客户端循环调 POST**：简单粗暴，吞吐受单连接 RTT 限制
- **直接 `INSERT` 到 DB**：可写脚本批量塞 `documents` + `chunks` + `embedding_jobs`，worker 自动消费。这是高吞吐路径

> 第二种是 `cpwsImportData` 的做法（百万级 case 导入）。本项目暴露的 HTTP 接口为通用场景设计，量大时建议另写专用 importer。
