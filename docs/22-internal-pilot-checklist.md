# 22 · 内部试运行上线清单（v0.1.0-internal-pilot）

> **范围声明：本版本仅用于「单个部门、内部试运行 1~2 周」，不是全公司正式推广。**
> 试运行暴露真实问题后再决定是否做 Hybrid、本地 reranker、复杂文档解析等增强。
> 这些都**不是上线阻断项**——当前检索效果已足够开始试用。

本清单是交给**部署人员**的一页式核对表。详细原理见 [04 快速开始](./04-quickstart.md)、[15 部署](./15-deployment.md)、[16 测试](./16-testing.md)、[20 一致性](./20-revision-and-deletion-consistency.md)。

## 一、发布整理产物（开发侧已完成）

| 产物 | 说明 |
|---|---|
| 版本标签 `v0.1.0-internal-pilot` | 明确的可部署版本基线 |
| `.env.example` | 无密钥配置模板，复制为 `.env` 后填写 |
| `scripts/acceptance.py` | 六步闭环验收脚本（见下） |
| pytest 全绿 | 156 passed / 12 集成用例需 `VECTOR_KB_PG_TEST_DSN` 时才跑 |

## 二、服务器上线门槛（部署人员逐条确认）

- [ ] **1. 服务器专用 `.env`**：由 `.env.example` 复制填写。`DB_PASSWORD` / `JWT_SECRET`(`openssl rand -hex 32`) / `EMBEDDING_API_KEY` / `QDRANT_API_KEY` 现填现管，**绝不进 git**。
- [ ] **2. 数据库迁移**：`alembic upgrade head`（当前 head = **0010**，含 0009 revision/rebuild_operations、0010 qdrant_cleanup_outbox）。首次迁移注意 0009 活动唯一索引前需清理历史重复活动行（见 docs/20 §11.1）。
- [ ] **3. 托管三类进程**（systemd 单元见 docs/15）：
  - API：`uvicorn app.main:app --host 0.0.0.0 --port 8100`
  - Embedding Worker：`python -m app.workers.embedder --watch`
  - Cleanup Worker：`python -m app.workers.cleanup --watch`
- [ ] **4. 固定内网地址 + 防火墙 + API Key**：API/PG/Qdrant/embedding 走内网固定地址；不暴露 PG、Qdrant 到公网；Qdrant 设 `QDRANT_API_KEY`；业务方用各自 API Key（明文只显示一次）。
- [ ] **5. 日志轮转 + 监控 + 备份**：监控指标见 docs/15（5xx 率、`/health`、`embedding_jobs` pending/failed 堆积、Qdrant 内存）；`pg_dump` 定时；Qdrant snapshots 定时。
- [ ] **6. 实际恢复一次备份演练**：真的 `pg_restore` 一次 + 真的恢复一次 Qdrant snapshot，确认可恢复，而非只配了备份。
- [ ] **7. 服务器重跑六步闭环验收**（见下），全部 ✅ 才算可用。
- [ ] **8. 选一个部门试运行 1~2 周**，再逐步开放其他部门。

## 三、Embedding Provider（必须二选一并 pin 住）

整条检索链路依赖 embedding 服务，**单点**。务必明确：

- **推荐：本地 bge-m3**（`.env.example` 默认）——免费、不依赖云、不会欠费。
- 备选：阿里云 DashScope text-embedding-v3——**必须确保账户余额**；欠费会同时阻断 embedding 与 rerank，整条链路瘫。

> 注意：切换 embedding 模型会改变向量空间，**已建库需重建**（不能只改 `.env`）。试运行前就把 provider 定下来。

## 四、六步闭环验收（交付脚本）

部署人员在服务器上，用一个有 insert/read/delete 权限的 API Key 跑：

```bash
ACC_API_KEY=<服务器上的明文Key> python scripts/acceptance.py --slug <已建库slug>
```

脚本走真实 HTTP API，逐步打印证据，全绿即通过：

| 步 | 验证点 | 期望 |
|---|---|---|
| 1 | 上传临时文档 | 201，返回 document_id / job_id |
| 2 | 任务变 done | 轮询 job 由 pending → done，Qdrant 出现该文档向量 |
| 3 | 明确内容检索命中 | 用文档独有句检索，命中该文档 |
| 4 | 改后重传 | 新内容可检索；旧内容因 revision 过滤**不可见** |
| 5 | 删除 | 立即检索**不到**（软删 + 可见性过滤即时生效） |
| 6 | Cleanup Worker 物理清理 | Qdrant 该文档向量被物理删除，outbox 标 done |

> 脚本本身会兜底清理临时文档；Key 走环境变量 `ACC_API_KEY`，不落 shell history。

## 五、试运行期定位（非阻断，记录即可）

- 检索默认 **dense、不重排**；建议调用方 `top_k >= 5`。本地 reranker 待后续部署（可提升精度；此前的提升数据来自 DashScope qwen3-rerank，不代表本地 bge-reranker 已验证）。
- 表格/跨段汇总类问题召回较弱，属已知项，等真实使用反馈再优化切分或加 query 改写。
