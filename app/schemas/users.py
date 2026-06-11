"""fastapi-users 用的 Pydantic schemas（读/写/更新）。"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi_users import schemas


class UserRead(schemas.BaseUser[uuid.UUID]):
    username: Optional[str] = None
    display_name: Optional[str] = None


class UserCreate(schemas.BaseUserCreate):
    username: Optional[str] = None
    display_name: Optional[str] = None


class UserUpdate(schemas.BaseUserUpdate):
    username: Optional[str] = None
    display_name: Optional[str] = None
