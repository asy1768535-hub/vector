"""创建首位超级管理员。仅在首次部署运行一次。

用法（推荐：stdin 输入密码，避免 shell 元字符如 $ * ! ^ 被解释）：
  python scripts/bootstrap_admin.py --email admin@example.com --username admin
  # 然后在提示后输入密码（不回显）

也可以传 --password，但**密码含特殊字符时必须单引号包起来**：
  python scripts/bootstrap_admin.py --email admin@example.com --password 'My$Pass!'

若同 email 已存在：报错退出，不覆盖已有用户。需要重置密码用 reset_password.py。
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from contextlib import suppress

from fastapi_users.exceptions import UserAlreadyExists

# 让脚本可独立运行
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.auth.user_manager import UserManager, get_user_db  # noqa: E402
from app.db import async_session_factory  # noqa: E402
from app.schemas.users import UserCreate  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bootstrap_admin")


async def _run(email: str, password: str, username: str | None) -> None:
    async with async_session_factory() as session:
        # 直接构造 user_db / user_manager（脚本里不走 FastAPI Depends 链）
        from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
        from app.models.user import User
        user_db = SQLAlchemyUserDatabase(session, User)
        user_manager = UserManager(user_db)
        try:
            user = await user_manager.create(
                UserCreate(
                    email=email,
                    password=password,
                    is_active=True,
                    is_superuser=True,
                    is_verified=True,
                    username=username,
                ),
                safe=False,
            )
        except UserAlreadyExists:
            log.error("user with email %s already exists; aborting.", email)
            sys.exit(2)
        log.info("superuser created: id=%s email=%s username=%s", user.id, user.email, user.username)


def main() -> None:
    p = argparse.ArgumentParser(description="Create the initial superuser.")
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="If omitted, prompt from stdin (recommended).")
    p.add_argument("--username", default=None)
    args = p.parse_args()

    pw = args.password
    if not pw:
        pw = getpass.getpass("Password:      ")
        confirm = getpass.getpass("Confirm:       ")
        if pw != confirm:
            print("Passwords do not match.", file=sys.stderr)
            sys.exit(1)
    if len(pw) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    with suppress(KeyboardInterrupt):
        asyncio.run(_run(args.email, pw, args.username))


if __name__ == "__main__":
    main()
