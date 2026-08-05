"""Verify database baseline after cleanup."""
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.config import settings

async def main():
    dsn = (f"postgresql+asyncpg://{settings.db_user}:{settings.db_password}"
           f"@{settings.db_host}:{settings.db_port}/{settings.db_name}")
    eng = create_async_engine(dsn, connect_args={"timeout": 10})

    async with eng.connect() as conn:
        ver = (await conn.execute(
            text("SELECT version_num FROM alembic_version"))).fetchone()[0]
        print(f"Alembic version: {ver}")

        r = await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name = ANY(:names)"),
            {"names": ["graph_extraction_jobs", "graph_extraction_units",
                       "extraction_context_snapshots", "extraction_raw_output_attempts"]})
        remaining = [row[0] for row in r.fetchall()]
        print(f"M1 tables: {remaining if remaining else 'NONE'}")

        chk = (await conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='ck_heartbeat_service_type'"))).fetchone()
        print(f"Heartbeat CHECK has graph_extractor: {'graph_extractor' in (chk[0] or '')}")

    await eng.dispose()

    admin_dsn = (f"postgresql+asyncpg://{settings.db_user}:{settings.db_password}"
                 f"@{settings.db_host}:{settings.db_port}/postgres")
    admin = create_async_engine(admin_dsn, connect_args={"timeout": 10})
    async with admin.connect() as conn:
        r = await conn.execute(text(
            "SELECT datname FROM pg_database WHERE datname LIKE 'vkt_%'"))
        dbs = [row[0] for row in r.fetchall()]
        print(f"vkt_* test DBs: {dbs if dbs else 'NONE'}")
    await admin.dispose()

    ok = (ver == "0020" and not remaining and "graph_extractor" not in (chk[0] or ""))
    print(f"\nBaseline matches Alembic 0020: {ok}")

asyncio.run(main())
