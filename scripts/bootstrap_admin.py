"""Idempotently initialize the first Organization and administrator.

The password is read from BOOTSTRAP_ADMIN_PASSWORD or an interactive prompt.
It is never accepted as a command-line argument and never logged.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from pydantic import EmailStr, TypeAdapter, ValidationError

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase  # noqa: E402

from app.auth.user_manager import UserManager  # noqa: E402
from app.config import settings, validate_supported_deployment_startup  # noqa: E402
from app.db import async_session_factory  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.deployment_bootstrap import (  # noqa: E402
    BootstrapCommand,
    DeploymentBootstrapError,
    initialize_supported_deployment,
    prepare_document_storage,
)

_EMAIL_ADAPTER = TypeAdapter(EmailStr)


def _validate_admin_email(value: str) -> str:
    try:
        return str(_EMAIL_ADAPTER.validate_python(value))
    except ValidationError as exc:
        raise DeploymentBootstrapError("administrator email is invalid") from exc


async def _run(args: argparse.Namespace, password: str) -> int:
    from app.services.selfcheck import check_consumable

    try:
        await prepare_document_storage(settings)
        dependencies_ok, _ = await check_consumable()
        if not dependencies_ok:
            raise DeploymentBootstrapError("required retrieval dependencies are unavailable")
    except DeploymentBootstrapError as exc:
        print(f"bootstrap rejected: {exc}", file=sys.stderr)
        return 2
    async with async_session_factory() as session:
        manager = UserManager(SQLAlchemyUserDatabase(session, User))
        command = BootstrapCommand(
            organization_slug=args.organization_slug,
            organization_name=args.organization_name,
            deployment_profile=args.organization_profile,
            admin_email=args.email,
            admin_username=args.username,
            password_hash=manager.password_helper.hash(password),
        )
        try:
            result = await initialize_supported_deployment(session, command)
            await session.commit()
        except DeploymentBootstrapError as exc:
            await session.rollback()
            print(f"bootstrap rejected: {exc}", file=sys.stderr)
            return 2
    state = "created" if result.created else "already initialized"
    print(
        f"deployment {state}: organization_id={result.organization_id} "
        f"admin_user_id={result.admin_user_id}"
    )
    return 0


def _read_password() -> str:
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD")
    if password is None:
        password = getpass.getpass("Administrator password: ")
        if password != getpass.getpass("Confirm password:       "):
            raise DeploymentBootstrapError("passwords do not match")
    if len(password) < 12:
        raise DeploymentBootstrapError("administrator password must be at least 12 characters")
    return password


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize the first Organization and administrator"
    )
    parser.add_argument("--organization-slug", required=True)
    parser.add_argument("--organization-name", required=True)
    parser.add_argument(
        "--organization-profile",
        choices=("hosted", "private"),
        default=settings.deployment_profile
        if settings.deployment_profile in {"hosted", "private"}
        else "private",
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--username", default=None)
    args = parser.parse_args()
    try:
        args.email = _validate_admin_email(args.email)
        validate_supported_deployment_startup(settings)
        if settings.deployment_profile in {"hosted", "private"} and (
            args.organization_profile != settings.deployment_profile
        ):
            raise DeploymentBootstrapError(
                "Organization profile must match DEPLOYMENT_PROFILE"
            )
        password = _read_password()
    except (RuntimeError, DeploymentBootstrapError) as exc:
        print(f"bootstrap rejected: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(asyncio.run(_run(args, password)))


if __name__ == "__main__":
    main()
