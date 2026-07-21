# Qdrant 备份 / 恢复（snapshot 模板）

> 配套 docs/27-backup-restore-runbook.md §四。Qdrant 向量是**派生数据**：只要 PostgreSQL 在、
> 库是托管库，可整库 `rebuild` 重算（docs/27 §七），所以 Qdrant 快照属「加速恢复」，非唯一来源。
>
> 下列命令是**模板**：`<QDRANT_HOST>` / `<QDRANT_API_KEY>` / `<COLLECTION>` 等均为占位符，
> 按你的部署替换；不要把真实 api-key 写进任何提交的文件。无 api-key 部署可去掉 `-H "api-key: ..."`。

`<COLLECTION>` = 库的 `qdrant_collection`（见 PG `sys_libraries` 表）。

## 1. 为某 collection 建快照

```bash
curl -X POST "http://<QDRANT_HOST>:6333/collections/<COLLECTION>/snapshots" \
  -H "api-key: <QDRANT_API_KEY>"
# 返回 JSON 含本次 snapshot 的 name（形如 <COLLECTION>-<时间戳>.snapshot）
```

列出已有快照：

```bash
curl "http://<QDRANT_HOST>:6333/collections/<COLLECTION>/snapshots" \
  -H "api-key: <QDRANT_API_KEY>"
```

## 2. 下载快照到异地保存

```bash
curl -O "http://<QDRANT_HOST>:6333/collections/<COLLECTION>/snapshots/<SNAPSHOT_NAME>" \
  -H "api-key: <QDRANT_API_KEY>"
# 下载得到 <SNAPSHOT_NAME> 文件，转存到备份盘 / 异地
```

## 3. 全部 collection 批量快照（思路）

1. 先从 PG 取所有库的 collection 名：
   ```sql
   SELECT qdrant_collection FROM sys_libraries WHERE deleted_at IS NULL;
   ```
2. 对每个 `<COLLECTION>` 重复第 1、2 步（用你习惯的 shell / PowerShell 循环串起来）。

## 4. 从快照恢复

把快照文件交还 Qdrant，二选一：

- **上传恢复接口**（适合远程 Qdrant）：
  ```bash
  curl -X PUT "http://<QDRANT_HOST>:6333/collections/<COLLECTION>/snapshots/recover" \
    -H "api-key: <QDRANT_API_KEY>" -H "Content-Type: application/json" \
    -d '{"location": "file:///qdrant/snapshots/<COLLECTION>/<SNAPSHOT_NAME>"}'
  ```
  （`location` 也可为 Qdrant 可访问的 URL；具体以你部署的 Qdrant 版本文档为准。）

- **放回快照目录**（自托管）：把 `<SNAPSHOT_NAME>` 放回 Qdrant 的 snapshots 目录后用上面的 `recover` 接口加载。

## 5. 不想备份 Qdrant？

完全可行：跳过本文件，恢复时对每个库走 `POST /admin/libraries/<SLUG>/rebuild-collection`
从 PG `chunks.text` 重算向量（docs/27 §七）。代价是恢复需重跑 embedding。

## 6. 自托管：直接备份存储卷（替代方案）

停写后打包 Qdrant 的 storage 数据卷（部署时挂载的目录），恢复即还原该目录后重启 Qdrant。
适合不想逐 collection 操作、且能接受短暂停写的场景。
