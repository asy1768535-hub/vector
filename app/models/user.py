"""sys_users：继承 fastapi-users 的 UUID 用户基类。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(SQLAlchemyBaseUserTableUUID, Base):
    """email/hashed_password/is_active/is_superuser/is_verified 来自 fastapi-users。"""

    __tablename__ = "sys_users"

    username: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
