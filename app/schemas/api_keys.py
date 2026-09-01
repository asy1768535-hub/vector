"""API Key 自助接口的 Pydantic schemas。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.organization import DEFAULT_ORGANIZATION_ID


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiKeyCreateRequest(StrictBaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Human-readable label.")
    organization_id: Optional[uuid.UUID] = None
    expires_at: Optional[datetime] = None


class ApiKeyRead(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID = DEFAULT_ORGANIZATION_ID
    name: str
    key_prefix: str
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: datetime

    @field_validator("organization_id", mode="before")
    @classmethod
    def _default_organization_id(cls, value):
        return DEFAULT_ORGANIZATION_ID if value is None else value

    model_config = {"from_attributes": True}


class ApiKeyCreated(ApiKeyRead):
    """新签 Key 的响应：包含明文 key（仅本次返回）。"""

    plaintext_key: str = Field(..., description="The plaintext key. Save it now; it will not be shown again.")
