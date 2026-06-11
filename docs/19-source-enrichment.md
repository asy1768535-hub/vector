# 19 · 跨库正文补全（source enrichment）

## 解决什么问题

有些 Qdrant collection 的 payload 里**只存外键**，不存正文。典型例子是已有的
`case_chunks_000`：

```json
// 一个 point 的 payload（除向量外的全部内容）
{ "case_id": 1, "section_id": 1 }
```

真正的全文存在**另一个业务库**的大表里：

| 项 | 值 |
|---|---|
| 库 | `cpwsdata`（与本服务的 `vector_kb` 同一台 PG：`10.0.10.114:5434`） |
| 表 | `case_full_texts` |
| 主键 | `case_id`（bigint，唯一索引） |
| 正文列 | `full_text`（text） |
| 行数 | ~6182 万 |

检索时需要：Qdrant 命中 → 拿到一批 `case_id` → 一次批量回查 `case_full_texts`
→ 把 `full_text` 作为正文返回。

## 怎么开启

每个库有一个可选的 `source_config`（JSONB 列）。为空 = 不补全（按 `payload.text` 返回）。
非空 = 检索后按外键回查源库。

`case-chunks` 库的配置（已由 `scripts/register_case_chunks.py` 写入）：

```json
{
  "db_name":     "cpwsdata",
  "table":       "case_full_texts",
  "key_field":   "case_id",
  "key_column":  "case_id",
  "text_column": "full_text",
  "key_type":    "bigint"
}
```

| 字段 | 含义 | 默认 |
|---|---|---|
| `db_name` | 源库名（与主库同机，复用 host/port/user/password，只换库名） | 必填（或用 `dsn`） |
| `dsn` | 跨机时用完整连接串，优先级高于 `db_name` | 可选 |
| `table` | 源表 | 必填 |
| `key_field` | **Qdrant payload** 里的外键字段名 | 必填 |
| `key_column` | **源表**里用于匹配的列 | 默认 = `key_field` |
| `text_column` | 源表里作为正文返回的列 | 必填 |
| `key_type` | 外键 PG 类型（`bigint`/`integer`/`text`/`uuid`/`varchar`） | `bigint` |
| `extra_columns` | 额外带回到 `metadata` 的列（可选） | `[]` |

> **重要：一个库 = 一个 Qdrant collection（向量） + 可选的一个 PGSQL 全文源。**
> 不要为「PGSQL 那一半」单独再建一个库。库管理页里每一行就代表一个完整的库，
> 「全文源 (PGSQL)」列会显示它是否绑定了外部正文表。

## 四种设置方式

**1. 后台「库管理」UI（推荐）**

新建库 / 编辑库对话框底部有「PGSQL 全文源（可选）」开关：打开后填
源库名 / 源表 / Qdrant 外键字段 / 正文列 / 外键类型即可。编辑时关掉开关再保存 = 清除全文源。
列表的「全文源 (PGSQL)」列会显示绑定情况（如 `cpwsdata.case_full_texts · case_id→full_text`）。

> 注意：UI 新建库会自动建一个新的空 `lib_<slug>` collection。
> 若要把库**指向一个已存在的 Qdrant collection**（如 `case_chunks_000`），用下面的注册脚本。

**2. 注册脚本（接入已有 collection）**

```bash
python scripts/register_case_chunks.py
# 自定义：--collection 已有collection名 --source-db / --source-table /
#         --source-key-field / --source-text-column / --source-key-type
# 不要补全：--no-source
```

**3. 建库 API**

```bash
curl -b jar.txt -X POST http://localhost:8100/admin/libraries \
  -H "Content-Type: application/json" \
  -d '{
    "slug":"case-chunks","name":"案件库",
    "source_config":{"db_name":"cpwsdata","table":"case_full_texts",
                     "key_field":"case_id","text_column":"full_text","key_type":"bigint"}
  }'
```

**4. 改库 API**（哨兵：传 `{}` 清空补全，不传则不变）

```bash
# 设置/修改
curl -b jar.txt -X PATCH http://localhost:8100/admin/libraries/case-chunks \
  -H "Content-Type: application/json" \
  -d '{"source_config":{"db_name":"cpwsdata","table":"case_full_texts","key_field":"case_id","text_column":"full_text","key_type":"bigint"}}'

# 清空（回到按 payload.text 返回）
curl -b jar.txt -X PATCH http://localhost:8100/admin/libraries/case-chunks \
  -H "Content-Type: application/json" -d '{"source_config":{}}'
```

## 检索效果

两个检索端点都会自动补全：

- **Dify**：`POST /retrieval` → 每条 `records[].content` = `full_text`，
  `metadata` 含 `case_id` / `section_id`（+ 任意 `extra_columns`）
- **本地**：`POST /libraries/case-chunks/query` → 每条 `results[].text` = `full_text`

```bash
curl -X POST http://localhost:8100/libraries/case-chunks/query \
  -H "Authorization: Bearer vk_xxx" -H "Content-Type: application/json" \
  -d '{"query":"强制执行 裁定","limit":5}'
```

```json
{
  "results": [
    {
      "text": "黑龙江省大庆市红岗区人民法院\n执 行 裁 定 书\n（2023）黑0605执59号\n……",
      "similarity": 0.71,
      "document_id": "",
      "chunk_id": "",
      "title": null,
      "metadata": { "case_id": 257584, "section_id": 491230 }
    }
  ]
}
```

## 实现要点（`app/services/source_enrichment.py`）

- **一次批量回查**：`SELECT key_column, text_column[, extra...] FROM table
  WHERE key_column = ANY($1::<key_type>[])`，命中主键索引（Index Scan），
  即使 6000w 行也很快。
- **SQL 注入防护**：表名/列名/库名是 SQL 标识符，无法参数化 → 用白名单正则
  `^[A-Za-z_][A-Za-z0-9_]*$` 严格校验；`key_type` 走类型白名单；`dsn` 必须是
  `postgres(ql)://` 字符串；真正的查询值（外键列表）走 `$1` 参数化。
  配置在**建库/改库/注册脚本**三处都先校验，非法直接拒绝（API 返回 400）。
- **外键范围校验**：整型外键超出目标列范围（int4/int8）→ 视为查无此键丢弃，
  不让超界值毒化整批 bind。
- **连接池复用**：asyncpg 连接池按目标库缓存（`min_size=1, max_size=5`），
  懒加载 + single-flight（建池在锁外执行，不阻塞并发请求），进程退出时优雅关闭。
- **超时兜底**：建池/连接超时 `SOURCE_ENRICH_CONNECT_TIMEOUT`（默认 10s），
  整体回查预算 `SOURCE_ENRICH_TIMEOUT`（默认 15s）。源库挂起不会拖垮主库会话。
- **去重**：同一批若多个 section 命中同一 `case_id`，只回查一次，再按顺序映射回每条结果。
  map key 与查询侧用同一套 coerce 规范化（含 uuid），避免类型不一致全部 miss。
- **优雅降级**：源库**不可达/超时**时不让整个检索 500，而是退回 `payload.text`
  （case 库即空正文），并打 warning 日志；只有 `source_config` **配置非法**才硬失败。

## 超时 / 降级配置（`.env`，可选）

```env
SOURCE_ENRICH_TIMEOUT=15           # 整体回查预算（秒）
SOURCE_ENRICH_CONNECT_TIMEOUT=10   # 建池 / 连接超时（秒）
```

## 常见问题

**Q：检索结果 text 为空？**
- 该库 `source_config` 没设对（`SELECT slug, source_config FROM sys_libraries WHERE slug='case-chunks'`）
- payload 里的外键字段名 ≠ `key_field`
- 源表里查无该 `case_id`，或 `full_text` 本身为 NULL
- 源库临时不可达 → 已**优雅降级**为空正文（看服务日志的 `source enrichment degraded` warning）

**Q：源库连不上会怎样？**
- **不会**让检索整体失败。补全自动降级：返回结果但正文为空（退回 `payload.text`），
  日志打 `source enrichment degraded ... falling back to payload.text`。
- `cpwsdata` 与 `vector_kb` 默认须在同一 PG 实例（或用 `dsn` 指定跨机连接串）。

**Q：能跨机连不同 PG 吗？**
- 能。`source_config` 里用 `"dsn": "postgresql://user:pw@host:port/db"` 代替 `db_name`。
- ⚠️ `dsn` 由超管配置、直接作为出站连接目标，**只指向可信主机**（避免 SSRF）。
