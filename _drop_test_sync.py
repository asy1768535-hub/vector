"""Drop test DB via psycopg2 sync — no transaction issues."""
from app.config import settings
import psycopg2

conn = psycopg2.connect(
    host=settings.db_host, port=settings.db_port,
    user=settings.db_user, password=settings.db_password,
    database="postgres", connect_timeout=10
)
conn.autocommit = True
cur = conn.cursor()

targets = ("vkt_hb_328ca459", "vkt_m1_suite_6b8eb523")
for target in targets:
    cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target,))
    if cur.fetchone():
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=%s AND pid != pg_backend_pid()", (target,))
        cur.execute(f'DROP DATABASE "{target}"')
        print(f"DROPPED: {target}")
    else:
        print(f"ABSENT: {target}")

conn.close()
print("DONE")
