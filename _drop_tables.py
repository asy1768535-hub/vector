"""Step 1: Drop 4 empty M1 tables only."""
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.config import settings

async def main():
    dsn = (f"postgresql+asyncpg://{settings.db_user}:{settings.db_password}"
           f"@{settings.db_host}:{settings.db_port}/{settings.db_name}")
    eng = create_async_engine(dsn, connect_args={"timeout": 10})

    async with eng.connect() as conn:
        await conn.execute(text("COMMIT"))
        for tbl in ("extraction_raw_output_attempts",
                     "extraction_context_snapshots",
                     "graph_extraction_units",
                     "graph_extraction_jobs"):
            r = await conn.execute(
                text("SELECT 1 FROM information_schema.tables "
                     "WHERE table_schema='public' AND table_name=:tbl"),
                {"tbl": tbl})
            if r.fetchone():
                await conn.execute(text(f"DROP TABLE {tbl} CASCADE"))
                print(f"DROPPED: {tbl}")
            else:
                print(f"ABSENT: {tbl}")

    await eng.dispose()
    print("DONE")

asyncio.run(main())
