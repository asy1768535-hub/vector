from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.admin import AdminResetPassword, AdminUserCreate, AdminUserUpdate, PermissionGrant, PermissionRevoke
from app.schemas.api_keys import ApiKeyCreateRequest


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (AdminUserCreate, {"email": "u@example.com", "password": "password123", "role": "admin"}),
        (AdminUserUpdate, {"display_name": "User", "password": "new-password"}),
        (AdminResetPassword, {"password": "password123", "is_superuser": True}),
        (ApiKeyCreateRequest, {"name": "dify", "user_id": str(uuid.uuid4())}),
        (PermissionGrant, {"user_id": str(uuid.uuid4()), "library_slug": "medical", "actions": ["read"], "role": "admin"}),
        (PermissionRevoke, {"user_id": str(uuid.uuid4()), "library_slug": "medical", "actions": ["read"], "all": True}),
    ],
)
def test_phase1_request_schemas_reject_extra_fields(schema, payload):
    """Phase 1 rule: request schemas must reject extra fields."""
    with pytest.raises(ValidationError):
        schema.model_validate(payload)
