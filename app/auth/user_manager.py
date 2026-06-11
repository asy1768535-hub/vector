"""fastapi-users UserManager + user DB 依赖。"""
from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator

from fastapi import Depends
from fastapi_users import BaseUserManager, UUIDIDMixin
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models.user import User

log = logging.getLogger(__name__)


async def get_user_db(session: AsyncSession = Depends(get_db)) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    yield SQLAlchemyUserDatabase(session, User)


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = settings.jwt_secret
    verification_token_secret = settings.jwt_secret

    async def on_after_register(self, user: User, request=None):
        log.info("user registered: %s (id=%s)", user.email, user.id)

    async def on_after_forgot_password(self, user: User, token: str, request=None):
        log.info("password reset requested for %s; token=%s", user.email, token)


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db)
