# 16 · 测试

## 运行

```bash
pytest tests/
pytest tests/test_dify_contract.py -v        # 跑单个文件
pytest tests/ -k casbin                       # 按关键字过滤
pytest tests/ --cov=app                       # 带覆盖率（需 pytest-cov）
pytest tests/test_pdf_extract.py -q           # PDF 文字层/扫描页 OCR 逐页逻辑（monkeypatch，不需真实模型）
```

## 当前基线

测试数量随代码变化，本文不把数量写成永久契约。2026-08-12 在文档补丁前的同一代码基线上，使用 `pytest -q` 完成 Python 全量测试；admin-ui 对 `admin-ui/**/*.test.mjs` 逐个执行 `node <test-file>` 完成 standalone Node 测试。本文不记录具体通过数；后续以当次命令输出为准。

覆盖面包括 Python 单元/契约测试、PG 集成测试（设置 `VECTOR_KB_PG_TEST_DSN` 时）、图谱/知识产物/分类/claim shadow 等能力测试，以及 `admin-ui` 下逐个执行的 Node 测试。文档链接和当前部署事实用本次文档校验命令单独验证，不依赖数据库连接。

## 设计原则

1. **不依赖外部服务**。需要 DB 的测试用 SQLite in-memory 或直接 mock；需要 Qdrant 的测试不存在（只测映射逻辑）。
2. **分层**。无 DSN 时 PG 集成测试应 skip；依赖真实外部 provider 的验收脚本不等同于 pytest 收集结果。
3. **覆盖契约 / 边界 / 错误**。错误算子被静默跳过；超长 password 报错；空 chunk 返回空数组。

## 加新测试

### 加一个纯单元测试

```python
# tests/test_my_feature.py
def test_my_function_does_x():
    from app.services.my_module import my_function
    assert my_function("input") == "expected"
```

跑 `pytest tests/test_my_feature.py`。

### 加一个需要 FastAPI app 的测试（TestClient）

```python
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_returns_200():
    r = client.get("/health")
    assert r.status_code == 200
    assert "status" in r.json()
```

注意：TestClient 会真正启动 app（包含 DB engine 初始化等）。如果不想连真 DB，加 `monkeypatch` 或在 conftest 里覆盖 `get_db`。

### 加一个需要 DB 的集成测试

项目已有 PG 集成测试；运行时显式设置 `VECTOR_KB_PG_TEST_DSN`，只指向可丢弃的临时库，不要读取开发机 `.env` 或生产库。

## 现有未覆盖的（可以补的）

- Worker 的整个 process_job 函数（需要 mock embedding + qdrant）
- ingest_text 的幂等行为（需要 DB session）
- /auth/jwt/login 整流程（form-urlencoded body）
- /retrieval 端到端（403 / 401 / 422）

每个都可加但需要 mock 层做完整。

## Mock 模式建议

- `httpx.AsyncClient` → 用 `respx` 或 `pytest-httpx`
- `qdrant.search` / `qdrant.upsert_points` → 直接 `unittest.mock.AsyncMock`
- DB → 用 SQLAlchemy `create_async_engine("sqlite+aiosqlite:///:memory:")`，注意 jsonb 字段在 SQLite 跑不出来时降级 JSON
- Casbin enforcer → 用 file adapter（参考 `tests/test_casbin_model.py`）

## CI（GitHub Actions）

工作流：`.github/workflows/ci.yml`，触发于 `push` / `pull_request` / 手动 `workflow_dispatch`，Python 3.11。
**CI 不下载任何模型，也不调用真实 Embedding / Rerank / OCR / Qdrant / 阿里云服务。**

两个 job：

1. **lint-and-test**（无外部服务）
   - `pip install -e ".[dev,ocr]"`
   - `ruff check app tests scripts`
   - `pytest -q` —— 单测全程 mock（见下），PG 集成测试因未设 `VECTOR_KB_PG_TEST_DSN` 自动 skip
   - `python scripts/check_release_safety.py` —— 扫描已跟踪文件里的真实 .env / 密钥格式 / 个人绝对路径 / 误跟踪目录

2. **pg-integration**（临时 PostgreSQL service）
   - 用 `postgres:16` service + **CI 专用账号/密码/库**（`vkci` / `vector_kb_ci`），与开发机 `.env` 无关
   - 显式设置 `VECTOR_KB_PG_TEST_DSN` 及 `DB_*`（仅指向该临时库）
   - `pytest -q tests/test_batch_a_pg_integration.py tests/test_batch_b_pg_integration.py tests/test_heartbeat_pg_integration.py`
   - 迁移测试以当前代码链和目标 head 为准；部署前/后分别用 `alembic heads` 与 `alembic current` 核对，不再把 0010/0011 当作当前 head。
   - **不**运行依赖真实 Qdrant/Embedding 的 E2E 脚本（`scripts/e2e_*.py`、`scripts/acceptance.py` 等都不是 pytest 用例，不会被收集）

### 哪些测试不访问真实模型/服务

- **全部单元测试**都不连真实服务：embedding/rerank 用 `AsyncMock`，OCR 用 mock `is_available`/`_get_engine`，DB 用贴近真实的 `AsyncMock`（见 `tests/test_query_import_api.py`），PDF 渲染/OCR 在 `tests/test_pdf_extract.py` 全 mock。
- `tests/test_check_local_ai_services.py`、`tests/test_check_release_safety.py` 也是纯 mock / 纯逻辑。
- **运行状态监控（批次 C2，docs/26）**：`tests/test_heartbeat.py` 纯单元——`derive_service_status` 在线/降级/离线判定、stopping 不计入、PID 复用两行并存、metadata ORM 保留名回归、`beat`/`prune` 写失败只记日志不抛；`tests/test_admin_operations.py` 用依赖覆盖 + mock session 验 `GET /admin/operations/status` 鉴权与空库/有数据聚合 shape；DB 端到端（upsert、迁移建表）在 `tests/test_heartbeat_pg_integration.py`（无 DSN 时 skip）。
- 只有 **PG 集成测试**会连真实数据库——但只连 CI 的**临时** Postgres，绝不连 `.env` 里的库。

### 本地跑与 CI 相同的命令

```bash
ruff check app tests scripts
pytest -q                              # 不设 DSN 时 PG 测试自动 skip
python scripts/check_release_safety.py
# admin-ui：逐个执行 admin-ui/**/*.test.mjs 中的 standalone Node 测试
# 可选：有一个可丢弃的本地测试库时，跑 PG 集成 + 迁移测试
#   export VECTOR_KB_PG_TEST_DSN=postgresql+asyncpg://<user>:<pw>@127.0.0.1:5432/<throwaway_db>
#   再设 DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME 指向同一临时库
#   pytest -q tests/test_batch_a_pg_integration.py tests/test_batch_b_pg_integration.py tests/test_heartbeat_pg_integration.py
```

> README 暂不加 CI 徽章，等 GitHub workflow 实际跑通后再决定。
