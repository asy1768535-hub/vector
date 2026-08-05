"""Step 2: Drop temp test database."""
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.config import settings

async def main():
    dsn = (f"postgresql+asyncpg://{settings.db_user}:{settings.db_password}"
           f"@{settings.db_host}:{settings.db_port}/postgres")
    eng = create_async_engine(dsn, connect_args={"timeout": 10})

    async with eng.connect() as conn:
        await conn.execute(text("COMMIT"))
        for target in ("vkt_hb_328ca459", "vkt_m1_suite_6b8eb523"):
            r = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname=:db"), {"db": target})
            if r.fetchone():
                await conn.execute(
                    text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE datname=:db AND pid <> pg_backend_pid()"),
                    {"db": target})
                await conn.execute(text(f'DROP DATABASE "{target}"'))
                print(f"DROPPED: {target}")
            else:
                print(f"ABSENT: {target}")

    await eng.dispose()
    print("DONE")

asyncio.run(main())
