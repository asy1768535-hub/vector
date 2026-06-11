"""紧急重置密码：直接更新 sys_users.hashed_password。

绕过 shell 元字符问题：用 getpass.getpass() 从 stdin 读密码（不会被 shell 解释）。

用法：
    python scripts/reset_password.py --email admin@example.com
    # 然后在提示后输入新密码（输入时不回显）
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path

from sqlalchemy import text

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase  # noqa: E402

from app.auth.user_manager import UserManager  # noqa: E402
from app.db import async_session_factory  # noqa: E402
from app.models.user import User  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reset_password")


async def _run(email: str, new_password: str) -> None:
    async with async_session_factory() as session:
        user_db = SQLAlchemyUserDatabase(session, User)
        user_manager = UserManager(user_db)
        # 用 fastapi-users 自己的 password_helper（默认 argon2），保证与登录验证算法一致
        hashed = user_manager.password_helper.hash(new_password)
        result = await session.execute(
            text(
                "UPDATE sys_users "
                "SET hashed_password = :hashed, is_active = true, deleted_at = NULL "
                "WHERE email = :email "
                "RETURNING id, email, is_superuser"
            ),
            {"hashed": hashed, "email": email},
        )
        row = result.first()
        if row is None:
            log.error("no user with email=%s", email)
            sys.exit(2)
        await session.commit()
        log.info(
            "password reset OK: id=%s email=%s is_superuser=%s",
            row[0], row[1], row[2],
        )


def main() -> None:
    p = argparse.ArgumentParser(description="Reset a user's password directly in DB.")
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="If omitted, prompt from stdin (recommended).")
    args = p.parse_args()

    pw = args.password
    if not pw:
        pw = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm:      ")
        if pw != confirm:
            print("Passwords do not match.", file=sys.stderr)
            sys.exit(1)
    if len(pw) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)
    asyncio.run(_run(args.email, pw))


if __name__ == "__main__":
    main()
