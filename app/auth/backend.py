"""注册两个 AuthenticationBackend：

1. cookie_backend  —— JWTStrategy + CookieTransport，Web 后台登录
2. api_key_backend —— APIKeyStrategy + BearerTransport，Dify/外部 API 调用
"""
from __future__ import annotations

import uuid

from fastapi import Depends
from fastapi_users import FastAPIUsers
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    CookieTransport,
    JWTStrategy,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import APIKeyStrategy
from app.auth.user_manager import get_user_manager
from app.config import settings
from app.db import get_db
from app.models.user import User


# ── Cookie / JWT (Web 后台) ────────────────────────────────────────────
cookie_transport = CookieTransport(
    cookie_name=settings.cookie_name,
    cookie_max_age=settings.jwt_lifetime_seconds,
    cookie_secure=settings.cookie_secure,
    cookie_httponly=True,
    cookie_samesite="lax",
)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=settings.jwt_secret, lifetime_seconds=settings.jwt_lifetime_seconds)


cookie_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)


# ── Bearer / API Key (Dify) ────────────────────────────────────────────
bearer_transport = BearerTransport(tokenUrl="auth/jwt/login")


def get_api_key_strategy(session: AsyncSession = Depends(get_db)) -> APIKeyStrategy:
    return APIKeyStrategy(session)


api_key_backend = AuthenticationBackend(
    name="apikey",
    transport=bearer_transport,
    get_strategy=get_api_key_strategy,
)


# ── FastAPIUsers 实例（同时挂两个 backend）──────────────────────────────
fastapi_users = FastAPIUsers[User, uuid.UUID](
    get_user_manager,
    [cookie_backend, api_key_backend],
)

# 通用 Depends
current_active_user = fastapi_users.current_user(active=True)
current_superuser = fastapi_users.current_user(active=True, superuser=True)
optional_current_user = fastapi_users.current_user(active=True, optional=True)
