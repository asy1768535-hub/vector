from __future__ import annotations

import asyncio

import psycopg2
from psycopg2.extras import register_uuid
from sqlalchemy.engine import URL


SqlRows = list[tuple[object, ...]]


async def execute_sql(url: URL, sql: str) -> SqlRows | None:
    """Execute one or more PostgreSQL statements in a single transaction."""
    return await asyncio.to_thread(_execute_sql, url, sql)


def _execute_sql(url: URL, sql: str) -> SqlRows | None:
    connection = psycopg2.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        dbname=url.database or "postgres",
    )
    try:
        register_uuid(conn_or_curs=connection)
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
                return cursor.fetchall() if cursor.description is not None else None
    finally:
        connection.close()
