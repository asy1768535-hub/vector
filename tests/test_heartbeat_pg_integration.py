"""运行状态心跳 PostgreSQL 集成测试（docs/26 / 批次 C2，测试清单 §8.1/§8.3）。

默认 SKIP（需 VECTOR_KB_PG_TEST_DSN 指向可丢弃测试库）。覆盖：
  - Alembic 0001→0011 真正建出 service_heartbeats 表 + 约束/索引。
  - beat() upsert：首插一行；同实例再 beat 只更新 last_seen/status，started_at 不变，仍一行。
  - 同类型两个 instance_id → 两行并存（唯一约束不报错）。

用法（PowerShell）：
    $env:VECTOR_KB_PG_TEST_DSN = "postgresql+asyncpg://postgres:<pw>@127.0.0.1:5434/vector_kb_test"
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  确保全部 ORM 注册
from app.db import Base
from app.models.service_heartbeat import ServiceHeartbeat
from app.services import heartbeat

_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="设置 VECTOR_KB_PG_TEST_DSN 指向可丢弃测试库后运行")


async def _engine():
    eng = create_async_engine(_DSN)
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return eng


def test_alembic_upgrade_builds_heartbeats_table(monkeypatch):
    """真正跑 Alembic 0001→0011（head），验证 service_heartbeats 表 + 唯一约束 + 索引建出。"""
    import asyncpg
    from urllib.parse import urlparse
    from alembic import command
    from alembic.config import Config
    from app.config import settings

    u = urlparse(_DSN.replace("+asyncpg", ""))
    host, port, user, pwd = u.hostname, u.port or 5432, u.username, u.password

    async def _admin(sql):
        conn = await asyncpg.connect(host=host, port=port, user=user, password=pwd, database="postgres")
        await conn.execute(sql)
        await conn.close()

    async def _check(name):
        eng = create_async_engine(f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{name}")
        try:
            async with eng.connect() as c:
                ver = (await c.execute(text("SELECT version_num FROM alembic_version"))).scalar()
                tbl = (await c.execute(text("SELECT to_regclass('public.service_heartbeats')"))).scalar()
                uq = (await c.execute(text(
                    "SELECT count(*) FROM pg_constraint WHERE conname='uq_heartbeat_service_instance'"))).scalar()
                idx = (await c.execute(text(
                    "SELECT count(*) FROM pg_indexes WHERE tablename='service_heartbeats' "
                    "AND indexname='ix_heartbeat_service_lastseen'"))).scalar()
            return ver, tbl, uq, idx
        finally:
            await eng.dispose()

    name = "vkt_hb_" + uuid.uuid4().hex[:8]
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS {name}'))
    asyncio.run(_admin(f'CREATE DATABASE {name}'))
    monkeypatch.setattr(settings, "db_host", host)
    monkeypatch.setattr(settings, "db_port", int(port))
    monkeypatch.setattr(settings, "db_user", user)
    monkeypatch.setattr(settings, "db_password", pwd)
    monkeypatch.setattr(settings, "db_name", name)
    try:
        command.upgrade(Config("alembic.ini"), "0011")
        ver, tbl, uq, idx = asyncio.run(_check(name))
        assert ver == "0011"
        assert tbl is not None        # 表建出
        assert uq == 1                # 唯一约束 (service_type, instance_id)
        assert idx == 1               # (service_type, last_seen_at) 索引
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS {name}'))


def test_beat_upsert_single_row():
    """首次 beat 插一行；同实例再 beat 命中唯一键只更新，started_at 不变，仍一行。"""
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        inst = f"host-1-{uuid.uuid4().hex[:8]}"
        # heartbeat 服务用 app.db.async_session_factory，这里 monkeypatch 到测试引擎
        from app.services import heartbeat as hb
        orig = hb.async_session_factory
        hb.async_session_factory = Session
        try:
            ok1 = await hb.beat("api", inst, hostname="h", pid=1, started_at=heartbeat.STARTED_AT,
                                heartbeat_metadata={"watch": True})
            assert ok1 is True
            async with Session() as s:
                row1 = (await s.execute(select(ServiceHeartbeat).where(
                    ServiceHeartbeat.instance_id == inst))).scalar_one()
                first_seen, started = row1.last_seen_at, row1.started_at
            # 再次 beat：status 改 stopping，metadata 改
            ok2 = await hb.beat("api", inst, hostname="h", pid=1, started_at=heartbeat.STARTED_AT,
                                status="stopping", heartbeat_metadata={"watch": False})
            assert ok2 is True
            async with Session() as s:
                rows = (await s.execute(select(ServiceHeartbeat).where(
                    ServiceHeartbeat.instance_id == inst))).scalars().all()
                assert len(rows) == 1                      # 仍一行（upsert 命中）
                r = rows[0]
                assert r.status == "stopping"
                assert r.heartbeat_metadata == {"watch": False}
                assert r.started_at == started             # started_at 不变
                assert r.last_seen_at >= first_seen        # last_seen 刷新
        finally:
            hb.async_session_factory = orig
            await eng.dispose()
    asyncio.run(run())


def test_same_type_two_instances_two_rows():
    """同 service_type 两个 instance_id → 两行并存，唯一约束不冲突（PID 复用同理）。"""
    async def run():
        eng = await _engine()
        Session = async_sessionmaker(eng, expire_on_commit=False)
        from app.services import heartbeat as hb
        orig = hb.async_session_factory
        hb.async_session_factory = Session
        tag = uuid.uuid4().hex[:8]
        a, b = f"host-100-{tag}a", f"host-100-{tag}b"     # 同 pid=100，不同后缀
        try:
            assert await hb.beat("cleanup_worker", a, hostname="h", pid=100, started_at=heartbeat.STARTED_AT)
            assert await hb.beat("cleanup_worker", b, hostname="h", pid=100, started_at=heartbeat.STARTED_AT)
            async with Session() as s:
                n = (await s.execute(select(func.count()).select_from(ServiceHeartbeat).where(
                    ServiceHeartbeat.instance_id.in_([a, b])))).scalar()
                assert n == 2                              # 两行并存，旧实例不被覆盖
        finally:
            hb.async_session_factory = orig
            await eng.dispose()
    asyncio.run(run())
