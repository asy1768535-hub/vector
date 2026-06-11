# 17 · 常见问题（FAQ）

## 安装 / 启动

### Q：`pip install` 卡在某个包很久 / 失败

用国内镜像：

```bash
pip install -r requirements-dev.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### Q：`alembic upgrade head` 报 `database "vector_kb" does not exist`

先用 `createdb` 在 PG 上建库：

```bash
createdb -h 10.0.10.114 -p 5434 -U postgres vector_kb
```

或 SQL：`CREATE DATABASE vector_kb;`

### Q：`alembic` 报 `relation "casbin_rule" already exists`

数据库里有旧表，先 `DROP DATABASE vector_kb; CREATE DATABASE vector_kb;` 再 `alembic upgrade head`。

### Q：`python -m app.main` 或 `uvicorn app.main:app` 启动后 `ImportError: cannot import name xxx`

把项目根目录加到 PYTHONPATH，或者直接 `cd D:/work_space/vectorDatabase` 后启动。

### Q：怎么改启动 host / port

改 `.env`：
```
API_HOST=0.0.0.0
API_PORT=8100
```
然后 `python -m app.main` 启动即可（它会读 settings）。命令行临时覆盖用 `uvicorn app.main:app --host ... --port ...`。

### Q：`bootstrap_admin.py` 报 `user already exists`

同 email 已存在；要重置密码用 SQL：

```sql
UPDATE sys_users SET hashed_password = '<新 bcrypt 哈希>' WHERE email = 'admin@example.com';
```

或先 `DELETE FROM sys_users WHERE email = 'admin@example.com'` 再 bootstrap。

## 认证 / 权限

### Q：登录 401 / 提示「用户名或密码错误」

- 用户存在吗：`SELECT id, email, is_active FROM sys_users WHERE email = 'xxx'`
- `is_active=true` 吗
- 密码对吗（如果忘了重新跑 bootstrap_admin 或用 SQL 改密码哈希）

### Q：登录成功但下一秒就 401

- `JWT_SECRET` 在 API 启动后被改过 → 旧 JWT 解不开。清浏览器 cookie 重新登录
- 多副本部署但 `JWT_SECRET` 不一致 → 各副本统一

### Q：调 `/retrieval` 报 403，但 user 明明授权了

- 用对了 user 的 API Key 吗？（前缀 vk_xxx）
- 该 user 在 **这个具体 slug** 的库上有 `read` 权限吗？查：
  ```sql
  SELECT v1, v2 FROM casbin_rule WHERE ptype='p' AND v0='<user_id>';
  ```
- 库被软删了？`SELECT slug, deleted_at FROM sys_libraries WHERE slug='xxx'`
- 多副本：可能在 A 副本授了权，但 B 副本还没 `load_policy`。重启 B 或等 30s

### Q：admin 在前端看不到「权限矩阵」「用户管理」等菜单

- 你登录的 user `is_superuser=true` 吗？查：
  ```sql
  SELECT email, is_superuser FROM sys_users WHERE email='你的';
  ```
- 不是 → bootstrap 一个真超管，或用 SQL `UPDATE sys_users SET is_superuser=true WHERE …`

### Q：API Key 撤销后还能用？

- 撤销立即生效（`revoked_at IS NOT NULL` 的 key 不会被 `APIKeyStrategy` 接受）
- 如果还能用，可能是另一把 key 在用，或者请求被 CDN 缓存（极少见）

## 摄入 / Worker

### Q：摄入后文档一直 `pending`

99% 是 worker 没起。

```bash
python -m app.workers.embedder --watch
```

或 systemd：`systemctl status vector-kb-worker@1`

### Q：worker 报 `dim mismatch: expected 1024`

库的 `embedding_dim` 跟 bge-m3 实际输出不一致。可能是：

- `.env` 里 `EMBEDDING_DIM=1024` 但服务返回 512 维 → 改 `.env` 重启
- 库建的时候 dim 写错了 → 已建的库不可改，只能新建库重导

### Q：worker 报 `connection refused`（连不上 bge-m3 / Qdrant）

- `curl http://10.0.10.2:8111/v1/embeddings` 自己手测
- `curl http://10.0.10.2:6333/healthz`
- 网络 / 防火墙 / 服务挂了 三选一

### Q：文档变 `failed` 怎么办

```sql
SELECT id, document_id, last_error FROM embedding_jobs
WHERE status='failed' ORDER BY finished_at DESC LIMIT 10;
```

修问题后在「任务监控」点重试，或：

```sql
UPDATE embedding_jobs
SET status='pending', worker_id=NULL, last_error=NULL,
    finished_at=NULL, claimed_at=NULL
WHERE id='<job_id>';
```

## 检索

### Q：`/retrieval` 返回 `{"records": []}`

- 文档摄入完了吗？status=`ready` 吗？
- 文档实际数：`/libraries/<slug>/stats`
- `score_threshold` 是不是设太高了？先设 0 试
- 直接查 Qdrant：`curl http://qdrant/collections/lib_<slug>/points/count`

### Q：检索结果跟期望差很远

- bge-m3 对中文 / 英文都有效；但小语种 / 代码 / 表格效果一般
- 试试改 `splitter`（短文用 `none`，长文 markdown 用 `markdown`）
- 试试调 `chunk_size`：领域文本长就放大
- 终极：把召回结果用 reranker 精排（P5 路线图）

### Q：Dify 一直显示「无法连接外部知识库」

- API Endpoint 是 `/retrieval` 不是 `/admin/...`
- 用了 HTTPS 但证书有问题？用 `curl --insecure` 自测
- API Key 写错（注意 `vk_` 前缀）

## 后台 UI

### Q：浏览器打开 `/console/` 白屏 / 报错

- 看浏览器 DevTools Console
- 常见：CDN 不通 → 把 unpkg.com 加白名单 / 用国内代理
- ES Module 在老浏览器不支持 → 升级浏览器（Chrome 90+）

### Q：登录后菜单不刷新

`store.user` 没更新。手动刷新页面或 `console.log(store.user)` 看是不是 null。可能 cookie 没设上（HTTPS 问题）。

### Q：「文档」页面看到空库列表

- 你的 user 没在任何库上有 read 权限（普通用户）；超管在「权限矩阵」勾上
- 库列表的来源：超管 → `/admin/libraries`；普通用户 → `/me/permissions`

## 性能

### Q：检索 P95 > 1s

定位瓶颈：

```bash
# 单独测 embedding 延迟
time curl -X POST http://10.0.10.2:8111/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"bge-m3","input":["test query"]}'

# 单独测 Qdrant search 延迟
time curl -X POST http://10.0.10.2:6333/collections/lib_xxx/points/search \
  -H 'Content-Type: application/json' \
  -d '{"vector":[…1024 维…],"limit":5}'
```

通常 bge-m3 是大头。横向扩 bge-m3 服务即可。

### Q：worker 吞吐太低

- 增大 `EMBED_WORKER_BATCH_DOCS`（一次抢更多）
- 增大 `EMBED_BATCH_SIZE`（一次 HTTP 多 chunk）
- 起多个 worker 进程（`systemctl start vector-kb-worker@{1..4}`）
- bge-m3 服务限制：看其自身指标

## 升级

### Q：改了 ORM 模型怎么生成 migration

```bash
alembic revision --autogenerate -m "describe change"
# 检查 alembic/versions/xxxx.py，必要时手改
alembic upgrade head
```

### Q：换 embedding 模型

不能直接改现有库的 model（维度不对）。流程：

1. 新建库 `<old-slug>-v2`，建库时指定新 `embedding_model` + `embedding_dim`
2. 重新摄入数据到新库
3. 给目标 user 授权新库
4. Dify 切换 `knowledge_id` 到新库
5. 软删旧库

## 其他

### Q：能不能给所有用户默认权限（比如所有用户都能读 `public` 库）

加一条 Casbin 角色策略：

```sql
INSERT INTO casbin_rule (ptype, v0, v1, v2) VALUES
('p', 'role:public', 'library:public', 'read');
```

然后新建用户时给他加上 `g, <user_id>, role:public`：

```sql
INSERT INTO casbin_rule (ptype, v0, v1) VALUES
('g', '<user_id>', 'role:public');
```

或在 `app/auth/user_manager.py:on_after_register` 里自动加。

### Q：能不能加新的 action（比如 `summarize`）

可以：

1. `app/casbin/service.py:VALID_ACTIONS` 加 `summarize`
2. 在业务 handler 用 `Depends(require_lib('summarize'))`
3. 后台「权限矩阵」前端表格加一列（`admin-ui/src/views/Permissions.js:ACTIONS`）

### Q：能 self-host bge-m3 吗

可以。推荐用 `infinity` / `tei` / `ollama`。本项目只要求服务端实现 OpenAI 兼容的 `/v1/embeddings`：

```json
POST /v1/embeddings
{"model": "bge-m3", "input": ["text1", "text2"]}
↓
{"data": [{"embedding": [...]}, {"embedding": [...]}]}
```

把 `.env` 的 `EMBEDDING_BASE_URL` 指过去就行。
