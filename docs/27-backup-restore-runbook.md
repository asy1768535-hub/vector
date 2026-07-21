# 27 · 备份恢复与部署演练手册（Runbook）

> 面向 **v0.1.x 内部试运行版** 的最小可用备份/恢复手册。目标是「丢了能补回来、补完能验收」，
> 不是搭专业运维平台。**不引入 Docker / Prometheus / Grafana**，不改检索逻辑。
>
> 配套脚本模板：`scripts/backup_pg.ps1`、`scripts/restore_pg.ps1`、`scripts/backup_qdrant.md`
> （都是模板：参数走环境变量 / 命令行，不写死真实密码、IP、路径，不读真实 `.env`，不输出密钥）。

## 一、先搞清楚：状态都在哪

系统本身**无状态**（API / Embedding Worker / Cleanup Worker 三类进程随时可重启、可水平扩展，见 docs/15）。
所有要备份的东西在三处，**重要性递减**：

| 数据 | 位置 | 能否从别处重建 | 备份优先级 |
|------|------|----------------|-----------|
| **PostgreSQL**（`vector_kb` 库） | 外部 PG | **不能**——这是唯一权威源 | **最高** |
| **`.env`**（密钥 / 连接串） | 部署目录，**不入 Git** | 不能（密钥丢了要重置） | **高** |
| **Qdrant 向量** | 外部 Qdrant（默认 `:6333`） | **看库的 `lifecycle_mode`**（见下） | 中（managed 可省，靠 rebuild） |
| 原始上传文件 | **系统不持久化**（见 §六） | 取决于你自己是否另存了源文件 | 看你的留存策略 |

> **库分两类，恢复责任不同（关键）：**
> - **`lifecycle_mode=managed`（默认）**：本系统管理其 Qdrant collection，向量可由本系统 `rebuild` 重算（§七）。正文来源有两种——普通库取 PG `chunks.text`；带 `source_config` 的库回查**外部源库**（如案件库 `cpwsdata`）取正文，二者 rebuild 都由本系统负责。
> - **`lifecycle_mode=external`**：collection 由**外部系统**管理，本系统**只负责检索与权限**，对其 rebuild → `409`、delete 只取消注册而**绝不删除其 collection**（见 `app/services/rebuild.py` / `app/api/admin_libraries.py` 的 external 防护）。**其 Qdrant 数据的备份与恢复由外部系统负责，不在本手册范围。**

PostgreSQL 里有什么（`alembic upgrade head`，当前 head = `0011`）：

- `sys_users` / `sys_api_keys` / `casbin_rule`：账号、API Key 哈希、权限策略
- `sys_libraries`：库定义（slug、embedding 模型/维度、chunk 参数、`source_config` 等）
- `documents` / `chunks`：文档元数据 + **chunk 正文（`chunks.text`）**——Qdrant 重建的正文来源
- `embedding_jobs` / `qdrant_cleanup_outbox` / `rebuild_operations`：任务与清理队列
- `service_heartbeats`（docs/26）/ `audit_log`：运行状态心跳、审计

> **一句话**：PostgreSQL 在，几乎一切可恢复（向量靠 rebuild 重算）；PostgreSQL 丢，业务数据基本归零（见 §五）。

---

## 二、PostgreSQL 备份（pg_dump）

用 **custom 格式**（`-Fc`）：压缩、支持并行恢复、可选表恢复。脚本模板见 `scripts/backup_pg.ps1`。

> **备份输出目录**：`scripts/backup_pg.ps1` 默认 `.\backups` **仅用于本地演练**（已在 `.gitignore` 忽略
> `/backups/`、`*.dump`、`*.backup`、`*.snapshot`，绝不会误入库）。**生产请显式 `-OutDir` 指向项目目录之外**
> （例如 `C:\backup\vector-kb` 或独立备份盘 / 异地），避免备份产物堆在代码仓里。

PowerShell（手动一次性）：

```powershell
# 密码用 PostgreSQL 原生环境变量传入，避免出现在命令行/进程列表里
$env:PGPASSWORD = '<DB_PASSWORD>'
pg_dump -h <DB_HOST> -p 5432 -U <DB_USER> -F c -d vector_kb `
  -f "C:\backup\vector-kb\vector_kb_$(Get-Date -Format yyyyMMdd_HHmmss).dump"
Remove-Item Env:\PGPASSWORD
```

Linux / cron（对照）：

```bash
PGPASSWORD="$DB_PASSWORD" pg_dump -h "$DB_HOST" -U "$DB_USER" -F c \
  -f "/var/backups/vector-kb/vector_kb_$(date +%F_%H%M%S).dump" vector_kb
```

建议：

- **频率**：内部试运行每日一次即可；上线前/重大变更前手动加一份。
- **保留**：滚动保留最近 N 份（脚本含可选清理参数），异地再留一份周备份。
- **只导关键表**（可选最小集，账号/权限/库定义/正文）：
  `pg_dump ... -t sys_users -t sys_api_keys -t casbin_rule -t sys_libraries -t documents -t chunks`
  ——但**整库 dump 更省心**，除非库特别大。
- **校验**：备份后 `pg_restore -l vector_kb_xxx.dump` 能列出目录即视为可读。

---

## 三、PostgreSQL 恢复（pg_restore / psql）

脚本模板见 `scripts/restore_pg.ps1`。**恢复是破坏性操作**：会覆盖目标库，务必先确认目标库选对。

恢复到一个**全新空库**（最干净）：

```powershell
$env:PGPASSWORD = '<DB_PASSWORD>'
# 1) 建空库（已存在则跳过）
createdb -h <DB_HOST> -U <DB_USER> vector_kb
# 2) 恢复（custom 格式用 pg_restore）
pg_restore -h <DB_HOST> -U <DB_USER> -d vector_kb --no-owner "C:\backup\vector-kb\vector_kb_<TS>.dump"
Remove-Item Env:\PGPASSWORD
```

恢复**覆盖**已有库（先清后建同名对象）：

```powershell
pg_restore -h <DB_HOST> -U <DB_USER> -d vector_kb --clean --if-exists --no-owner "<DUMP_FILE>"
```

纯 SQL 文本备份（若用 `-Fp` 导出）用 `psql` 回灌：

```powershell
psql -h <DB_HOST> -U <DB_USER> -d vector_kb -f "<DUMP_FILE.sql>"
```

恢复后**对齐迁移版本**（备份与代码 schema 版本一致时通常已是 head）：

```powershell
alembic upgrade head    # 幂等；若备份较旧会补齐到 0011
alembic current         # 确认显示 0011 (head)
```

---

## 四、Qdrant 备份与恢复

Qdrant 是外部服务（默认 `http://<QDRANT_HOST>:6333`）。**向量可由 PG 重建**（§七），所以 Qdrant
快照是「加速恢复」而非「唯一来源」。两种做法，二选一：

### 4.1 用 Qdrant Snapshot API（推荐，按 collection）

详细 curl 示例见 `scripts/backup_qdrant.md`。要点：

```bash
# 为某 collection 建快照（collection 名 = 库的 qdrant_collection，见 sys_libraries）
curl -X POST "http://<QDRANT_HOST>:6333/collections/<COLLECTION>/snapshots" \
  -H "api-key: <QDRANT_API_KEY>"
# 下载快照文件到本地异地保存
curl -O "http://<QDRANT_HOST>:6333/collections/<COLLECTION>/snapshots/<SNAPSHOT_NAME>" \
  -H "api-key: <QDRANT_API_KEY>"
```

恢复：把快照文件放回 Qdrant 快照目录或用 recovery 接口上传（见 `scripts/backup_qdrant.md`）。

### 4.2 直接备份 Qdrant 存储卷

自托管 Qdrant 时，**停写后**打包其 storage 目录（部署时挂载的数据卷）即可；恢复就是还原该目录后重启 Qdrant。

### 4.3 干脆不备份 Qdrant

只要 PG 在、且库是 **`managed` 库**（正文在 `chunks.text`，或带 `source_config` 可回查外部源库），
可跳过 Qdrant 备份，恢复时整库 `rebuild`（§七）。代价是恢复要重算 embedding（取决于 bge-m3 吞吐与库规模）。

> **`lifecycle_mode=external` 库不适用本节**：其 collection 由外部系统管理，本系统不快照、不 rebuild、不删除——
> 备份恢复由外部系统负责。

---

## 五、配置 / `.env` / 上传文件备份

### 5.1 `.env` 安全保存原则（重要）

`.env` 含 `JWT_SECRET`、`DB_PASSWORD`、`QDRANT_API_KEY`、`EMBEDDING_API_KEY` 等机密：

- **绝不提交 Git**（仓库已 `.gitignore`；`scripts/check_release_safety.py` 会扫真实 `.env` 误跟踪）。
- 备份到**加密**位置（密码管理器 / 加密盘 / 受控密钥库），**不要**和 PG dump 放同一个明文目录随便传。
- 只保留**值的清单**，对照 `.env.example` 的键即可重建；切勿把真实值写进任何文档或脚本。
- `JWT_SECRET` 丢失：换新值后**所有已签发的登录态失效**（用户需重新登录），但数据不受影响。

### 5.2 上传 / 样本 / 配置文件

- **原始上传文件**：系统**不持久化**（§六）。如需留档，请在**系统之外**自行归档源文件目录，并纳入你的常规文件备份。
- **样本 / 验收资料**：属于业务资料，单独备份，**不要**进 Git（`samples/` 已被安全扫描列为禁跟踪目录）。
- **部署配置**：systemd unit、Nginx 配置等无机密的文件可随部署仓另存；含机密的按 §5.1 处理。

---

## 六、原始上传文件为什么不在备份范围

`/libraries/{slug}/import-file` 把上传文件**读进内存**（`app/api/documents.py:_read_capped`，上限
`MAX_IMPORT_FILE_BYTES`），抽取/切分成 **chunk 正文写入 PG（`chunks.text`）**，再由 worker embed 进 Qdrant。
**系统不在磁盘保存原始字节**。含义：

- 备份 PostgreSQL = 备份了所有**可被检索的正文**（chunk 粒度）。
- **原始文件（PDF/docx 本体）不可由系统找回**——如果业务需要留存原件，请在系统外另存。
- **外部正文库（source enrichment，如案件库 `cpwsdata`）**：这类库的正文不在本 PG，而在外部源库，
  Qdrant payload 仅存外键 + 回退文本。备份本 PG **不覆盖**外部源库——外部源库由其属主单独备份（见 §五要点）。

---

## 七、Qdrant 丢了、PostgreSQL 还在 → 用 rebuild 重算向量

这是最常见、也最从容的故障：向量是**派生数据**，PG 是源。**仅适用 `lifecycle_mode=managed` 库**
（普通库正文在 `chunks.text`；带 `source_config` 的库回查外部源库取正文）。

**managed 库**恢复步骤：

1. 确认 PG 正常、Qdrant 已起（可空库）。
2. 三类进程都在跑（尤其 **Embedding Worker**，rebuild 靠它消费 job）。
3. 对每个库触发重建（超管）：

   ```bash
   curl -X POST "http://<API_HOST>:8100/admin/libraries/<SLUG>/rebuild-collection" \
     -H "Cookie: <超管登录态>"
   ```

   或后台「库管理」页的「重建 collection」按钮。三阶段 `prepare → qdrant → activate`，
   `expected_job_count` 个 embedding job 入队，worker 逐个 embed，全部 `done` 后自动 finalize（见 docs/11 / docs/26）。
4. 在「运行状态」页（`/console/#/operations`）看 `rebuild_operations` 进度到 100%、`sys_libraries.index_state` 回到 `ready`。

> **带 `source_config` 的 managed 库**：只要外部源库（如 `cpwsdata`）还在、`source_config` 还在 PG，rebuild 同样能从外部源库回查正文重算。
> 若外部源库也丢了，则这些库无法重建（正文不在本系统）。
>
> **`lifecycle_mode=external` 库**：本系统**不负责** rebuild——对其 `rebuild-collection` 直接返回 `409`，且绝不删/建其 Qdrant collection。
> 其向量恢复由**外部系统**负责。本系统侧只要 PG 在，库定义/权限/检索路由即恢复，collection 一旦外部就绪即可继续检索。

---

## 八、PostgreSQL 丢了 → 哪些不可恢复

PG 是唯一权威源。**没有 PG 备份**时，即使 Qdrant 完好也救不回业务：

- **账号 / API Key / 权限策略**（`sys_users` / `sys_api_keys` / `casbin_rule`）：无法从 Qdrant 反推 → 全部丢失，需重建账号、重发 Key、重配权限。
- **库定义**（`sys_libraries`：slug、模型、维度、chunk 参数、`source_config`）：丢失 → 检索/重建都无从谈起。
- **文档元数据 + chunk 正文**（`documents` / `chunks`）：丢失。Qdrant payload 里**可能**残留 text，但不含完整业务关系，且无法据此恢复可见性过滤（revision）与鉴权。
- **审计日志**（`audit_log`）：丢失，不可追溯。
- **任务 / 清理 / 重建队列**：丢失（影响有限，可重新触发）。

**结论**：保住 PG dump 是第一要务。Qdrant 可不备份（靠 rebuild），但 **PG 必须有可恢复的备份**。

---

## 九、恢复后验收（演练 checklist）

每次恢复 / 演练后**按序**跑一遍，证明全链路通：

1. **配置就位**：`.env` 已恢复（机密齐全），`alembic current` 显示 `0011 (head)`。
2. **启动三类进程**（见 docs/15）：
   ```bash
   # API
   python -m app.main           # 或 uvicorn app.main:app --host 0.0.0.0 --port 8100
   # Embedding Worker
   python -m app.workers.embedder --watch
   # Cleanup Worker
   python -m app.workers.cleanup --watch
   ```
3. **健康检查**：`GET /health` 返回 200，`embedding` / `rerank` / `ocr` / db / qdrant 状态符合预期。
4. **运行状态页**：打开 `/console/#/operations`，确认 API / Embedding Worker / Cleanup Worker 三者 **在线**（docs/26）。
5. **向量在不在**：随便挑一个库做一次检索，命中正常 → Qdrant 数据在；若是「Qdrant 丢失走 rebuild」场景，先按 §七重建到 `index_state=ready` 再测。
6. **临时文档生命周期冒烟**（一篇就够，验收完删掉）：
   - 上传一篇临时文档 → 等 `embedding_jobs` 该 job 变 `done`；
   - 检索该文档独有的词 → 能命中；
   - 删除该文档 → 立刻检索不到；
   - 确认 `qdrant_cleanup_outbox` 对应行变 `done`（Cleanup Worker 已物理清理 Qdrant points）。
7. **登录态**：用一个已恢复账号登录后台成功（验证 `sys_users` / `casbin_rule` 生效）。

全部通过 = 恢复成功。任一步失败，回到对应章节定位（多半是 `.env` 缺值或某个 worker 没起）。

---

## 十、明确不做

- 不引入 Docker / Prometheus / Grafana / 任何专业备份运维平台。
- 不做自动定时备份编排（cron / 计划任务由部署方按 §二自行接入；脚本只给模板）。
- 不做 PITR / WAL 归档 / 流复制（内部试运行版用每日全量 dump 足够；有更高 RPO/RTO 需求再上）。
- 不在仓库内存放任何真实密钥、真实备份文件或业务样本。
