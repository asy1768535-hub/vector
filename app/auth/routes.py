"""装配 fastapi-users 自带 router，挂到 /auth 与 /users 前缀。"""
from __future__ import annotations

from fastapi import APIRouter

from app.auth.backend import cookie_backend, fastapi_users
from app.schemas.users import UserCreate, UserRead, UserUpdate


def build_auth_router() -> APIRouter:
    """登录/注销 + 注册 + 当前用户。注意：注册接口主要给 superuser 走 /admin/users；
    生产环境可在 main.py 取消挂载 get_register_router 走 admin-only。"""
    router = APIRouter()

    # POST /auth/jwt/login, /auth/jwt/logout
    router.include_router(
        fastapi_users.get_auth_router(cookie_backend),
        prefix="/auth/jwt",
        tags=["auth"],
    )

    # POST /auth/register —— 默认开放，生产环境若要管控可移除
    router.include_router(
        fastapi_users.get_register_router(UserRead, UserCreate),
        prefix="/auth",
        tags=["auth"],
    )

    # POST /auth/forgot-password, /auth/reset-password
    router.include_router(
        fastapi_users.get_reset_password_router(),
        prefix="/auth",
        tags=["auth"],
    )

    # GET/PATCH /users/me, GET /users/{id}（superuser）
    router.include_router(
        fastapi_users.get_users_router(UserRead, UserUpdate),
        prefix="/users",
        tags=["users"],
    )

    return router
