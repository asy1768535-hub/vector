# 验证与验收矩阵

> 本页定义“本次做了什么才能声称什么”，不记录永久通过数。历史测试数字只能作为排查线索；完成、修复、发布或安全结论必须附本窗口的新鲜命令与结果。

## 先做的三件事

1. `git status --short`：识别用户已有的脏改动和未跟踪文件；
2. 修改函数、类或方法前执行 `node .gitnexus/run.cjs impact <symbol> --direction upstream --repo vectorDatabase`；
3. 为本次变更选择最窄、因果相关的检查，不以全量历史结果替代。

## 证据矩阵

| 变更或结论 | 最低新鲜证据 | 仍然不能证明 |
| --- | --- | --- |
| 文档、配置说明、任务记录 | 本地链接检查、`git diff --check`、与代码/配置的交叉阅读 | API、Worker 或部署实际可用 |
| Python 纯逻辑 | `\.venv\Scripts\python.exe -m pytest -q <受影响测试>` | PostgreSQL、Qdrant、对象存储或浏览器行为 |
| FastAPI 契约 / 权限 / 上传 | 受影响 API 测试及相关负向用例 | 真实 provider、目标环境凭据或并发负载 |
| PDF 解析 / 路由 | `tests/test_pdf_*.py`、`tests/test_mineru_pdf.py`、同步/异步导入契约测试 | 真正 HTTP 上传、Worker、真实 PDF provider 或生产质量 |
| 管理后台纯状态或 API 映射 | 对应 `admin-ui/**/*.test.mjs` 的 `node <test-file>` | 浏览器布局、网络时序、真实鉴权 |
| 前后端交互 | 后端契约测试 + 前端测试 + 已授权的真实浏览器路径 | 目标环境发布成功 |
| 迁移 / PostgreSQL 语义 | `\.venv\Scripts\alembic.exe heads` + 仅连接可丢弃库的 PG 集成测试 | 目标数据库已升级或数据迁移无误 |
| 发布 / 外部 provider | 经授权的目标环境健康、迁移 current、feature gate、worker、回滚与观察证据 | 仅凭本地 pytest 或 Dockerfile |

## 常用命令

```powershell
# 静态与结构
git diff --check
.\.venv\Scripts\python.exe -m ruff check app tests scripts
.\.venv\Scripts\alembic.exe heads
python scripts/check_release_safety.py

# 受影响后端测试（替换为真实测试文件）
.\.venv\Scripts\python.exe -m pytest -q tests/test_pdf_preflight.py tests/test_mineru_pdf.py

# 受影响前端测试（替换为真实文件）
node admin-ui/catalog_console.test.mjs

# 提交前的图谱变更审查；当前工作树很脏时须解释无关影响，不能把风险忽略掉
node .gitnexus/run.cjs detect-changes --scope all --repo vectorDatabase
```

`status_local.ps1` 是本地服务状态的读操作；`start_local.ps1`、迁移、重试队列、删除、外部 provider 调用和部署都改变状态，必须有相应授权和恢复路径。

## 分层验收记录

每个跨 owner 的任务在计划或 handoff 中记录以下最小表，而不是只写“测试通过”。

| 平面 | 要证明什么 | 证据 / 未验证项 |
| --- | --- | --- |
| 行为 | 用户或调用方得到的结果 | 测试、浏览器步骤或 API 响应 |
| 负向路径 | 无权限、坏输入、超限、provider 失败不会误报成功 | 对应测试或明确未测 |
| 数据 | revision、任务、向量、文件或审计状态符合预期 | 可丢弃测试库或目标环境只读核验 |
| 回归 | 受影响调用方/执行流没有被意外改变 | GitNexus impact、目标回归测试、detect-changes |
| 运行 | feature gate、worker、provider、迁移和端口满足预期 | 本地或目标环境当次输出 |

未运行的平面必须写 `未验证`；模拟和 mock 只能证明它们覆盖的契约，不能代替真实运行证据。
