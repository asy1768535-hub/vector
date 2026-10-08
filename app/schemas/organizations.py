from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.services.organization_authorization import VALID_ACTIONS


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserOrganizationRead(BaseModel):
    organization_id: uuid.UUID
    slug: str
    name: str
    role: str


class AdminUserOrganizationRead(BaseModel):
    membership_id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    role: str
    status: str
    can_manage: bool


class OrganizationMemberCreate(StrictBaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    username: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    role: str = Field(default="member", pattern="^(organization_admin|member)$")


class OrganizationMemberUpdate(StrictBaseModel):
    expected_role: str = Field(pattern="^(organization_admin|member)$")
    expected_status: str = Field(pattern="^(active|disabled)$")
    role: str = Field(pattern="^(organization_admin|member)$")
    status: str = Field(pattern="^(active|disabled)$")


class OrganizationMemberRead(BaseModel):
    membership_id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    username: str | None = None
    display_name: str | None = None
    role: str
    status: str
    is_active: bool
    disabled_at: datetime | None = None
    created_at: datetime


class OrganizationPasswordReset(StrictBaseModel):
    password: str = Field(..., min_length=8, max_length=128)


class OrganizationPermissionGrant(StrictBaseModel):
    user_id: uuid.UUID
    library_slug: str = Field(..., min_length=2, max_length=80)
    actions: list[str] = Field(..., min_length=1, max_length=4)

    @field_validator("actions")
    @classmethod
    def _validate_actions(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(action not in VALID_ACTIONS for action in value):
            raise ValueError("actions must be a unique subset of read, insert, delete, admin")
        return value


class OrganizationPermissionRevoke(StrictBaseModel):
    user_id: uuid.UUID
    library_slug: str = Field(..., min_length=2, max_length=80)
    actions: list[str] | None = Field(default=None, max_length=4)

    @field_validator("actions")
    @classmethod
    def _validate_actions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(set(value)) != len(value) or any(action not in VALID_ACTIONS for action in value):
            raise ValueError("actions must be a unique subset of read, insert, delete, admin")
        return value


class OrganizationPermissionRead(BaseModel):
    organization_id: uuid.UUID
    user_id: uuid.UUID
    library_slug: str
    actions: list[str]


class OrganizationRolloutRead(BaseModel):
    capability: str
    enabled: bool
    version: int


class OrganizationRolloutUpdate(StrictBaseModel):
    enabled: bool
    expected_version: int = Field(ge=0)
