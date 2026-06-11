# 18 · 使用说明（操作手册）

任务导向。需要做什么找对应小节即可。


> 📌 本服务**没有写死的默认账号**。第一位超级管理员由你自己通过 `bootstrap_admin.py` 脚本创建，邮箱和密码自定。

---
创建数据库 
alembic upgrade head

## 0 · 第一次部署（创建超管）
python scripts/bootstrap_admin.py --email minddewo@outlook.com --password j$%^**flkm*!^ --username admin

```bash
cd D:/work_space/vectorDatabase

# 用你自己想要的邮箱 + 密码
python scripts/bootstrap_admin.py \
  --email admin@example.com \
  --password "<你的强密码>" \
  --username admin
```

成功输出：

```
superuser created: id=xxxx-xxxx-... email=admin@example.com username=admin
```

> 这条命令只用跑**一次**。同 email 再跑会报 `user already exists` 退出。
>
> **忘记密码？** 见 §11。
>
> **不想让它在命令行里看密码？** 可以把脚本改成 `getpass.getpass()` 提示输入，或环境变量传入。

---

## 1 · 登录后台

启动服务（两个终端）：

```bash
# 终端 A（host/port 读 .env 里的 API_HOST / API_PORT）
python -m app.main

# 终端 B
python -m app.workers.embedder --watch
```

浏览器打开：

```
http://localhost:8100/console/
```

> 不要用 `http://0.0.0.0:8100/`，那是服务**监听地址**，不是浏览器目标。用 `localhost` 或机器 IP。

填邮箱 + 密码 → 「登录」→ 进入「概览」页。

---

## 2 · 概览（Dashboard）

进入后第一屏，看：

- **当前用户卡**：自己的角色（超管 / 普通）、已授权库列表
- **服务状态卡**：DB / Qdrant / Embedding 模型探活
- **使用提示卡**：基本路径指引

服务全部 OK → 卡片状态都是绿色「ok」。
DB / Qdrant 红色 → 检查 `.env` 配置 / 对应服务是否启动。

---

## 3 · 建库（管理员）

侧边栏 → 「库管理」→ 右上角「新建库」。

| 字段 | 说明 |
|---|---|
| **Slug** | URL-safe 标识，**也是 Dify 的 `knowledge_id`**。小写字母 / 数字 / `_-`，长度 2-80。例：`medical`, `legal-cn`, `internal_2024` |
| **名称** | 显示用，例 `医学库` |
| **描述** | 选填 |
| **chunk_size** | 单个分片字符数，默认 1000，范围 200-8000 |
| **chunk_overlap** | 分片重叠字符数，默认 120，范围 0-2000 |

点「创建」→ 服务端会**同步**在 Qdrant 建一个 collection（命名 `lib_<slug>`，例如 slug=`medical` → collection=`lib_medical`）。建失败则整个事务回滚，PG 也不会留垃圾记录。

成功后表格里会出现这条库，状态「正常」。

### 改库

只能改：name / description / chunk_size / chunk_overlap
**不可改**：slug / embedding_model / embedding_dim / vector_distance（这些定了 Qdrant 维度，改了会乱）

### 删库

点「删除」→ 确认弹窗 → 软删（PG 留记录方便审计）+ 后台异步清 Qdrant collection。

---

## 4 · 建用户（管理员）

侧边栏 → 「用户管理」→ 「新建用户」。

| 字段 | 说明 |
|---|---|
| **邮箱** | 登录用，唯一 |
| **初始密码** | 至少 8 字符。用户自己以后可以改 |
| **用户名** | 选填，唯一 |
| **显示名** | 选填 |
| **超级管理员** | 开关。开 → 该账号也是超管，所有库直通；关 → 普通用户，要按库授权 |

提交 → 表格里新增一行。

### 操作

- **禁用 / 启用**：临时停用，不丢数据
- **设超管 / 取消超管**：切换 `is_superuser`
- **软删**：标 `is_active=false` + `deleted_at`，账号无法登录但记录保留

---

## 5 · 授权（管理员）

侧边栏 → 「权限矩阵」。

1. 顶部下拉**选用户**
2. 表格列出所有库，每行 3 个 checkbox：**read** / **insert** / **delete**
3. 勾选 / 取消 → 内存改动
4. 右上角「保存变更」→ 系统自动 diff 当前 vs 加载时快照，分别调授权 / 撤权

| 动作 | 含义 |
|---|---|
| **read** | 能调 `/retrieval` 检索；能在「文档」页看到该库 |
| **insert** | 能向该库提交文档 |
| **delete** | 能删除该库的文档 |

> 超管用户**不需要**走授权——所有库自动直通。在权限矩阵选超管时会有提示。

### 反查谁有权限

```bash
# 看某用户在哪些库上有什么权限
curl -b cookies.txt http://localhost:8100/admin/permissions?user_id=<uuid>

# 看某库被授权给哪些用户
curl -b cookies.txt http://localhost:8100/admin/permissions/library/medical
```

---

## 6 · 自助签发 API Key（任意用户）

适用场景：要把这个库接给 Dify / 第三方应用 / 自动化脚本。

侧边栏 → 「我的 API Key」→ 「生成新 Key」。

填名称（例 `dify-prod`）→ 点「生成」→ 弹窗显示明文。

⚠️ **明文仅本次返回，关闭弹窗后只能看前缀，不能再看明文。**

- 「复制到剪贴板」→ 立刻贴到你要用的地方
- 撤销：在列表点「撤销」→ 立即失效，不可恢复

> 「最近使用」字段会在每次调用 `/retrieval` 时刷新，可用来排查 key 是不是真在用。

---

## 7 · 上传文档（有 insert 权限的用户）

侧边栏 → 「文档」→ 顶部下拉**选库**（只显示你有 read 权限的库）→ 「提交文档」。

| 字段 | 说明 |
|---|---|
| **标题** | 可选，会进 Qdrant payload，检索结果里回显 |
| **external_id** | 可选，调用方自己的 ID，用于追踪。文档幂等是按文本 hash，不是按这个 |
| **切分方式** | `text`（默认，递归字符切分，中英文友好）/ `markdown`（先按 `#` 标题切再细分）/ `none`（不切，整段当一片）|
| **metadata** | 选填 JSON，会展开进 Qdrant payload，可用于 Dify `metadata_condition` 过滤。例：`{"author":"WHO","year":2024}` |
| **正文** | 必填，粘 plain text / Markdown / JSON 字符串 |

点「提交（异步 embed）」→ 立即返回 `pending` 状态。worker 在后台处理。

等几秒（取决于 embed 服务速度），刷新页面：状态变 `ready` 表示向量已写 Qdrant，可被检索。

### 摄入流程

1. 计算 `sha256(text)` → 命中已有文档则跳过（幂等）
2. 切分成 N 个 chunk
3. 写 `documents` / `chunks` / `embedding_jobs` (status=pending)
4. 返回 `{document_id, status:"pending", chunk_count, job_id}`
5. worker 抢锁 → 批量调 bge-m3 → upsert Qdrant → 状态变 `ready`

### 删除

文档表格右侧「删除」→ 确认 → 软删 PG + 后台异步清 Qdrant 对应 points。

---

## 8 · 用 API Key 调 Dify 检索

最常用场景：在外部应用 / Dify / 脚本里查询。

```bash
curl -X POST http://localhost:8100/retrieval \
  -H "Authorization: Bearer vk_xxxxxxxxxxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "knowledge_id": "medical",
    "query": "高血压怎么办",
    "retrieval_setting": {"top_k": 5, "score_threshold": 0.3}
  }'
```

响应：

```json
{
  "records": [
    {
      "content": "高血压用药遵循阶梯治疗原则……",
      "score": 0.87,
      "title": "高血压用药指南",
      "metadata": {"document_id":"…","chunk_id":"…","author":"WHO","year":2024}
    },
    ...
  ]
}
```

### Dify 配置

在 Dify「设置」→「知识库」→「外部知识库」：

| 字段 | 填什么 |
|---|---|
| **API Endpoint** | `http://<你的服务器>:8100/retrieval` |
| **API Key** | 上面 §6 生成的明文 |
| **Knowledge ID** | 库的 slug，例 `medical` |

接好后在 Dify 应用里试问答即可。

---

## 9 · 监控异步任务（管理员）

侧边栏 → 「任务监控」。

表格显示 `embedding_jobs` 所有任务。

| 状态 | 含义 |
|---|---|
| `pending` | 排队中，等 worker 抢 |
| `processing` | worker 正在处理 |
| `done` | 已完成，Qdrant 已写入 |
| `failed` | 多次失败被弃；`last_error` 列有原因 |

可按状态过滤。

**重试**：失败 / 卡死的 job 点「重试」→ 状态重置为 pending，下轮 worker 会重新抢。

---

## 10 · 查审计日志（管理员）

侧边栏 → 「审计日志」。

记录所有管理员操作：建库 / 改用户 / 授权 / 撤权 / 删库 等。

可按 action 字符串过滤，例 `library.create` / `permission.grant` / `user.disable`。

---

## 11 · 忘记密码 / 重置超管

### 方案 A：自己改（推荐）

如果还能登录：浏览器后台 → 「我的用户」（fastapi-users 内置 `PATCH /users/me`）改密码。

### 方案 B：bootstrap 一个新超管

直接 SQL 软删旧的，建新的：

```bash
# 1. 在 PG 上软删旧超管
psql -h <host> -p <port> -U postgres -d vector_kb -c \
  "UPDATE sys_users SET is_active=false, deleted_at=now() WHERE email='old-admin@…';"

# 2. bootstrap 新的
python scripts/bootstrap_admin.py \
  --email new-admin@example.com \
  --password "<新密码>"
```

### 方案 C：直接改密码哈希（紧急）

```python
# Python 一行生成 bcrypt 哈希
python -c "import bcrypt; print(bcrypt.hashpw(b'<新密码>', bcrypt.gensalt()).decode())"
```

```sql
-- 把哈希贴进 SQL
UPDATE sys_users
SET hashed_password = '$2b$12$...上面输出...',
    is_active = true, deleted_at = NULL
WHERE email = 'admin@example.com';
```

---

## 12 · 常见使用 Q&A

| 问 | 答 |
|---|---|
| 普通用户能不能看到「库管理」？ | 不能。`is_superuser=false` 的用户菜单只有「概览」「文档」「我的 API Key」 |
| 普通用户在「文档」页能看到哪些库？ | 只能看到自己有 `read` 权限的库（来自 `/me/permissions`） |
| 文档上传后多久能查到？ | embedding 服务正常的话 2-10 秒；看「任务监控」状态变 `done` 即可查 |
| 同样文本上传两次会重复入库吗？ | 不会。系统按 `sha256(text)` 去重，第二次直接返回已有 document_id |
| 删除一个库会自动清向量吗？ | 会。库 DELETE 时后台异步清整个 Qdrant collection |
| 删除一个文档会自动清向量吗？ | 会。文档 DELETE 时后台异步按 `payload.document_id` 删对应 points |
| API Key 撤销后还能用吗？ | 不能。`revoked_at` 一旦设置，下次调用立即 401 |
| 同一个 user 能签多把 API Key 吗？ | 能，任意多把。每把独立撤销 |
| 改 chunk_size 会重切已有文档吗？ | **不会**。只影响之后摄入的。要重切 → 删文档再重新上传 |
| 能换 embedding 模型吗？ | 已建库不能改维度；要换模型 → 新建库 + 重导数据 → Dify 切到新 slug |
| 多副本 worker 会重复抢同一个 job 吗？ | 不会。SQL 用 `FOR UPDATE SKIP LOCKED` 保证 |
| 后台白屏怎么办？ | F12 看 console；按 Ctrl+F5 硬刷新。详见 [17 FAQ](./17-faq.md) |

---

## 13 · 推荐工作流模板

### 接入一个新业务方（典型 5 步）

1. **超管**「库管理」→ 建库 `<biz>-<env>`（例 `marketing-prod`）
2. **超管**「用户管理」→ 给业务方建账号 `<biz>-bot@company.com`
3. **超管**「权限矩阵」→ 给该账号在该库勾 `read` + `insert`（也按需 `delete`）
4. **业务方**登录 → 「我的 API Key」生成 `<biz>-dify` Key → 复制明文
5. **业务方**把 Key + slug 配到 Dify「外部知识库」

完成。后续业务方自管文档，互不干扰。

### 双库切换（如 A/B 测试不同 embedding）

1. 建库 `kb-bge-m3`（默认 model）
2. 建库 `kb-qwen3-embed`（指定新 model + dim）
3. 同一份文档分别向两个库 POST
4. 在 Dify 配两个外部知识库，对比检索效果
5. 确定后留一个，删另一个

---

## 14 · 相关文档

- [04 快速开始](./04-quickstart.md) —— 第一次跑起来
- [06 认证系统](./06-authentication.md) —— cookie / API Key 双通道细节
- [07 权限系统](./07-permissions.md) —— Casbin 模型
- [10 检索接口](./10-retrieval-api.md) —— Dify spec 详细
- [13 API 参考](./13-api-reference.md) —— 全部 endpoint
- [17 FAQ](./17-faq.md) —— 踩坑速查
