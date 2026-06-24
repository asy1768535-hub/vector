# 16 · 测试

## 运行

```bash
pytest tests/
pytest tests/test_dify_contract.py -v        # 跑单个文件
pytest tests/ -k casbin                       # 按关键字过滤
pytest tests/ --cov=app                       # 带覆盖率（需 pytest-cov）
pytest tests/test_pdf_extract.py -q           # PDF 文字层/扫描页 OCR 逐页逻辑（monkeypatch，不需真实模型）
```

## 当前覆盖

23 个测试，纯单元，不依赖 DB / Qdrant / bge-m3：

| 文件 | 覆盖 |
|---|---|
| `test_dify_contract.py` | Dify 请求/响应 schema 严格匹配；最小 / 完整 / 未知字段三档 |
| `test_casbin_model.py` | RBAC 模型逻辑：直接策略 / 库隔离 / 角色继承 |
| `test_retrieval_filter.py` | metadata_condition → Qdrant filter 映射，覆盖 `=` / `!=` / `contains` / `in` / null / 未知算子 |
| `test_splitter.py` | text / markdown / none 三种切分；空输入 |
| `test_api_key_strategy.py` | bcrypt 哈希 / roundtrip / 唯一性 |

## 设计原则

1. **不依赖外部服务**。需要 DB 的测试用 SQLite in-memory 或直接 mock；需要 Qdrant 的测试不存在（只测映射逻辑）。
2. **快**。23 个用例总跑时间 ~17 秒（绝大部分耗时在 bcrypt 的安全 cost factor 上）。
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

推荐用 [pytest-postgresql](https://pytest-postgresql.readthedocs.io/) 起临时 PG，或者用 testcontainers。当前项目还没接，按需添加。

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

## CI 建议

GitHub Actions / GitLab CI 跑：

```yaml
- name: Setup Python
  uses: actions/setup-python@v5
  with:
    python-version: '3.13'

- name: Install
  run: pip install -r requirements-dev.txt

- name: Lint
  run: ruff check app/ tests/

- name: Test
  run: pytest tests/ -v
```
