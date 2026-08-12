"""源数据补全（cross-DB enrichment）。

场景：某些 Qdrant collection 的 payload 里只存了外键（例如 case_chunks_000
只有 {case_id, section_id}），真正的全文存在另一个业务库的大表里
（cpwsdata.case_full_texts.full_text，6000w+ 行，case_id 为主键）。

检索时：Qdrant 命中 → 拿到一批外键 → 一次批量 `WHERE key = ANY($1)`
回查源库 → 把 full_text 拼回每条结果。

设计要点：
  - 库级配置 `sys_libraries.source_config`（JSONB），为空则不做补全（走默认 payload.text）。
  - 源库连接默认复用主库的 host/port/user/password，只改 db_name（同机不同库）；
    也支持显式 `dsn` 覆盖（跨机时用）。
  - 表名/列名是 SQL 标识符，无法参数化 → 用严格白名单正则校验，杜绝注入；
    真正的查询值（外键列表）走 $1 参数化。
  - asyncpg 连接池按连接目标缓存复用，避免每次检索重连。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

import asyncpg

from app.config import settings

log = logging.getLogger(__name__)

# SQL 标识符（表名 / 列名 / 库名）白名单：字母或下划线开头，后续字母/数字/下划线
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 允许的外键 PG 类型（用于 ANY 数组 cast）
_ALLOWED_KEY_TYPES = {"bigint", "integer", "int", "int4", "int8", "text", "varchar", "uuid"}
# 整型 key 的范围边界（防止超界值毒化整个批量 bind）
_INT4_MIN, _INT4_MAX = -(2**31), 2**31 - 1
_INT8_MIN, _INT8_MAX = -(2**63), 2**63 - 1
_REDACTED = "[REDACTED]"
_SECRET_CONFIG_KEYS = {
    "access_key",
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "dsn",
    "key",
    "password",
    "passwd",
    "private_key",
    "secret",
    "secret_key",
    "token",
    "user",
    "username",
}
_SECRET_CONFIG_SUFFIXES = (
    "_access_key",
    "_api_key",
    "_authorization",
    "_key",
    "_credential",
    "_credentials",
    "_password",
    "_passwd",
    "_private_key",
    "_secret",
    "_secret_key",
    "_token",
    "_user",
    "_username",
)
_NON_SECRET_CONFIG_KEYS = {"key_field", "key_column", "key_type"}

# db_name -> asyncpg.Pool 缓存；按目标库复用连接池
_pools: dict[str, asyncpg.Pool] = {}
# 建池中的 future（single-flight）：同一目标并发首次只建一个池
_pool_inflight: dict[str, "asyncio.Future[asyncpg.Pool]"] = {}
_pool_lock = asyncio.Lock()


class SourceConfigError(ValueError):
    """source_config 配置非法（标识符不合法 / 缺字段等）。运维配置问题，应硬失败。"""


class SourceEnrichmentRuntimeError(RuntimeError):
    """补全运行期失败（源库不可达 / 超时 / 查询出错）。调用方应优雅降级回退 payload.text。"""


def _is_secret_config_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().casefold().replace("-", "_")
    return (
        normalized in _SECRET_CONFIG_KEYS
        or (
            normalized.endswith(_SECRET_CONFIG_SUFFIXES)
            and normalized not in _NON_SECRET_CONFIG_KEYS
        )
    )


def _looks_like_dsn(value: object) -> bool:
    return isinstance(value, str) and value.strip().casefold().startswith(
        ("postgres://", "postgresql://")
    )


def redact_source_config(value: Any) -> Any:
    """Recursively redact connection credentials while preserving diagnostics."""
    if isinstance(value, dict):
        return {
            key: _REDACTED if _is_secret_config_key(key) else redact_source_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_source_config(item) for item in value]
    if _looks_like_dsn(value):
        return _REDACTED
    return value


def _validate_ident(value: str, field: str) -> str:
    if not isinstance(value, str) or not _IDENT_RE.match(value):
        raise SourceConfigError(f"invalid SQL identifier for {field!r}")
    return value


def parse_source_config(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    """校验并标准化库级 source_config。返回 None 表示「不补全」。

    期望结构：
        {
          "db_name":     "cpwsdata",          # 源库名（同机）；或用 "dsn" 覆盖
          "dsn":         "postgresql://...",  # 可选，跨机连接串（优先级高于 db_name）
          "table":       "case_full_texts",   # 源表
          "key_field":   "case_id",           # Qdrant payload 里的外键字段名
          "key_column":  "case_id",           # 源表里用于匹配的列（默认 = key_field）
          "text_column": "full_text",         # 源表里作为正文返回的列
          "key_type":    "bigint",            # 外键 PG 类型（默认 bigint）
          "extra_columns": ["court", "year"]  # 可选，额外带回到 metadata 的列
        }
    """
    if not cfg:
        return None
    if not isinstance(cfg, dict):
        raise SourceConfigError(f"source_config must be an object, got {type(cfg).__name__}")

    table = _validate_ident(cfg.get("table", ""), "table")
    text_column = _validate_ident(cfg.get("text_column", ""), "text_column")
    key_field = cfg.get("key_field") or cfg.get("key_column")
    if not key_field:
        raise SourceConfigError("source_config missing key_field/key_column")
    key_column = _validate_ident(cfg.get("key_column") or key_field, "key_column")
    if not isinstance(key_field, str) or not key_field:
        raise SourceConfigError("invalid key_field")

    key_type = (cfg.get("key_type") or "bigint").lower()
    if key_type not in _ALLOWED_KEY_TYPES:
        raise SourceConfigError(f"key_type {key_type!r} not in allowlist {_ALLOWED_KEY_TYPES}")

    extra_columns = cfg.get("extra_columns") or []
    if not isinstance(extra_columns, list):
        raise SourceConfigError("extra_columns must be a list")
    extra_columns = [_validate_ident(c, "extra_columns[]") for c in extra_columns]
    # 去掉与 key_column / text_column 重复的列：避免 SELECT 重复列、避免把正文再塞进 metadata
    extra_columns = [c for c in extra_columns if c not in {key_column, text_column}]

    dsn = cfg.get("dsn")
    db_name = cfg.get("db_name")
    if dsn is not None:
        # dsn 直接作为连接目标，必须是 postgres 连接串字符串（防止 SSRF / 非串在 connect 时才崩）
        if not isinstance(dsn, str) or not dsn.strip():
            raise SourceConfigError("dsn must be a non-empty string")
        if not (dsn.startswith("postgres://") or dsn.startswith("postgresql://")):
            raise SourceConfigError("dsn must start with postgres:// or postgresql://")
    elif not db_name:
        # 未指定 db_name → 用 .env 的默认外部正文库（案件库走这条：SOURCE_DB_NAME=cpwsdata）
        db_name = settings.source_db_name_effective
    if db_name is not None:
        _validate_ident(db_name, "db_name")

    return {
        "dsn": dsn,
        "db_name": db_name,
        "table": table,
        "key_field": key_field,
        "key_column": key_column,
        "text_column": text_column,
        "key_type": key_type,
        "extra_columns": extra_columns,
    }


def slug_to_table(slug: str) -> str:
    """库 slug → 源表名：把 URL-safe 的 '-' 换成 SQL 合法的 '_'。"""
    return slug.replace("-", "_")


def conventional_config(slug: str) -> dict[str, Any]:
    """按「约定」生成一个 source_config（用于 UI 勾选「PGSQL 全文源」时）。

    约定（全部来自 settings，可在 .env 覆盖）：
      - db_name     = SOURCE_DB_NAME 或主库 DB_NAME
      - table       = slug（'-'→'_'）
      - key_field   = SOURCE_KEY_FIELD（默认 text_id）
      - text_column = SOURCE_TEXT_COLUMN（默认 content）
      - key_type    = SOURCE_KEY_TYPE（默认 bigint）

    注意：返回的是「原始」dict，仍需经 parse_source_config 校验（表名等可能非法）。
    """
    return {
        "db_name": settings.db_name,  # 约定库走主库 vector_kb；案件库另配 cpwsdata
        "table": slug_to_table(slug),
        "key_field": settings.source_key_field,
        "key_column": settings.source_key_field,
        "text_column": settings.source_text_column,
        "key_type": settings.source_key_type,
    }


def _pool_cache_key(parsed: dict[str, Any]) -> str:
    dsn = parsed.get("dsn")
    if dsn:
        digest = hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]
        return f"dsn:{digest}"
    return f"db:{parsed['db_name']}"


async def _create_pool(parsed: dict[str, Any]) -> asyncpg.Pool:
    """建池（带连接超时）。不持锁执行，避免源库挂起时阻塞所有并发请求。"""
    connect_timeout = settings.source_enrich_connect_timeout
    common = dict(min_size=1, max_size=5, command_timeout=30, timeout=connect_timeout)
    if parsed.get("dsn"):
        coro = asyncpg.create_pool(dsn=parsed["dsn"], **common)
    else:
        # 连接到「全文源 PG」（SOURCE_DB_*，留空回退主库）；库名来自 source_config
        coro = asyncpg.create_pool(
            host=settings.source_db_host_effective,
            port=settings.source_db_port_effective,
            user=settings.source_db_user_effective,
            password=settings.source_db_password_effective,
            database=parsed["db_name"],
            **common,
        )
    # 双保险：asyncpg 的 timeout 仅约束单连接握手；用 wait_for 兜住整体建池耗时
    return await asyncio.wait_for(coro, timeout=connect_timeout + 5)


async def _get_pool(parsed: dict[str, Any]) -> asyncpg.Pool:
    """按目标库懒加载 + 缓存 asyncpg 连接池。

    建池在锁外执行（可能耗时 / 挂起），仅用锁保护「单飞」语义：
    同一目标并发首次请求只建一个池，其余复用。
    """
    key = _pool_cache_key(parsed)
    pool = _pools.get(key)
    if pool is not None:
        return pool

    # 用 per-key 的 future 实现 single-flight，避免持锁 await 建池
    async with _pool_lock:
        pool = _pools.get(key)  # double-check
        if pool is not None:
            return pool
        existing = _pool_inflight.get(key)
        if existing is None:
            existing = asyncio.ensure_future(_create_pool(parsed))
            _pool_inflight[key] = existing
            owner = True
        else:
            owner = False

    try:
        pool = await existing
    except Exception:
        if owner:
            async with _pool_lock:
                _pool_inflight.pop(key, None)
        raise
    if owner:
        async with _pool_lock:
            _pools[key] = pool
            _pool_inflight.pop(key, None)
        log.info("source_enrichment: created pool for %s", key)
    return pool


def _coerce_key(raw: Any, key_type: str) -> Any:
    """把 Qdrant payload 里的外键值转成匹配源表列类型的 Python 值。

    超出目标整型范围 → 返回 None（当作查无此键），而不是让超界值毒化整批 bind。
    """
    if raw is None:
        return None
    if key_type in {"bigint", "integer", "int", "int4", "int8"}:
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return None
        if key_type in {"integer", "int", "int4"}:
            lo, hi = _INT4_MIN, _INT4_MAX
        else:
            lo, hi = _INT8_MIN, _INT8_MAX
        if val < lo or val > hi:
            return None
        return val
    # text / varchar / uuid 等保持字符串
    return str(raw)


async def fetch_source_rows(
    parsed: dict[str, Any], keys: list[Any]
) -> dict[Any, dict[str, Any]]:
    """一次批量回查源表。返回 {key_value: {text_column: ..., extra...}}。

    keys 已经过类型 coerce 与去重。空 → 返回空 dict（不触库）。
    """
    if not keys:
        return {}

    table = parsed["table"]
    key_column = parsed["key_column"]
    text_column = parsed["text_column"]
    key_type = parsed["key_type"]
    extra = parsed["extra_columns"]

    select_cols = [key_column, text_column, *extra]
    # 标识符已白名单校验过，可安全拼接；值走 $1 参数化
    col_sql = ", ".join(select_cols)
    sql = (
        f"SELECT {col_sql} FROM {table} "
        f"WHERE {key_column} = ANY($1::{key_type}[])"
    )

    budget = settings.source_enrich_timeout
    try:
        async def _run() -> list[Any]:
            pool = await _get_pool(parsed)
            # acquire 也加超时：池满（max_size=5）时不无限排队
            async with pool.acquire(timeout=budget) as conn:
                return await conn.fetch(sql, keys)

        # 整体预算兜底：连接/排队/查询任何一段挂起都不会拖死请求
        rows = await asyncio.wait_for(_run(), timeout=budget)
    except (asyncio.TimeoutError, OSError, asyncpg.PostgresError) as exc:
        # Driver messages can include the configured DSN; expose only a stable error.
        raise SourceEnrichmentRuntimeError("source fetch failed") from exc

    out: dict[Any, dict[str, Any]] = {}
    for row in rows:
        d = dict(row)
        # 关键：用与查询侧一致的 coerce 规范化 map key
        # （asyncpg 把 uuid 列解码成 uuid.UUID，而查询侧用 str，不规范化会全部 miss）
        out[_coerce_key(d[key_column], key_type)] = d
    return out


class EnrichmentResult:
    """enrich_payloads 的返回：与 payloads 等长对齐，零暴露内部实现。

    属性：
      - parsed：标准化后的配置（None 表示未启用补全）
      - texts：list[str|None]，每条 payload 对应的源库正文（缺失/未配置 → None）
      - rows ：list[dict|None]，每条 payload 对应的完整源库行（含 extra_columns）
    """

    __slots__ = ("parsed", "texts", "rows")

    def __init__(
        self,
        parsed: dict[str, Any] | None,
        texts: list[str | None],
        rows: list[dict[str, Any] | None],
    ) -> None:
        self.parsed = parsed
        self.texts = texts
        self.rows = rows

    @property
    def enabled(self) -> bool:
        return self.parsed is not None


async def enrich_payloads(
    source_config: dict[str, Any] | None,
    payloads: list[dict[str, Any]],
    *,
    degrade_on_runtime_error: bool = True,
) -> EnrichmentResult:
    """对一批 Qdrant payload 做源库补全，返回与 payloads 等长对齐的结果。

    错误语义：
      - 配置非法（SourceConfigError）→ 直接抛（运维问题，应硬失败）。
      - 运行期失败（源库不可达 / 超时）→ 默认优雅降级：返回 parsed + 全 None 的
        texts/rows，调用方据此回退 payload.text，检索结果不至于整体 500。
        传 degrade_on_runtime_error=False 则把 SourceEnrichmentRuntimeError 抛出。
      - 未配置补全 → parsed=None，texts/rows 全 None。
    """
    parsed = parse_source_config(source_config)  # 可能抛 SourceConfigError（硬失败）
    n = len(payloads)
    if parsed is None:
        return EnrichmentResult(None, [None] * n, [None] * n)

    key_field = parsed["key_field"]
    key_type = parsed["key_type"]
    text_column = parsed["text_column"]

    # 收集每条 payload 的外键值（coerce），保留原始顺序，同时去重批量回查
    coerced: list[Any] = [
        _coerce_key((p or {}).get(key_field), key_type) for p in payloads
    ]
    unique_keys = sorted({k for k in coerced if k is not None}, key=lambda x: (str(type(x)), str(x)))

    try:
        row_map = await fetch_source_rows(parsed, unique_keys)
    except SourceEnrichmentRuntimeError:
        if not degrade_on_runtime_error:
            raise
        log.warning(
            "source enrichment degraded (source DB error); falling back to payload.text. "
            "source=%s table=%s", _pool_cache_key(parsed), parsed["table"]
        )
        return EnrichmentResult(parsed, [None] * n, [None] * n)

    texts: list[str | None] = []
    rows: list[dict[str, Any] | None] = []
    for k in coerced:
        row = row_map.get(k) if k is not None else None
        rows.append(row)
        texts.append(row.get(text_column) if row else None)
    return EnrichmentResult(parsed, texts, rows)


async def close_all_pools() -> None:
    """关闭所有源库连接池（用于优雅退出 / 测试清理）。"""
    async with _pool_lock:
        for key, pool in list(_pools.items()):
            try:
                await pool.close()
            except Exception:  # noqa: BLE001
                log.exception("failed closing pool %s", key)
            _pools.pop(key, None)
