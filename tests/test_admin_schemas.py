from datetime import datetime, timezone
import uuid

import pytest
from pydantic import ValidationError

from app.schemas.admin import AdminUserCreate, AdminUserRead


def test_admin_user_read_allows_existing_non_deliverable_email() -> None:
    email = "reader@example.invalid"

    user = AdminUserRead(
        id=uuid.uuid4(),
        email=email,
        is_active=True,
        is_superuser=False,
        is_verified=False,
        created_at=datetime.now(timezone.utc),
    )

    assert user.email == email
    with pytest.raises(ValidationError):
        AdminUserCreate(email=email, password="valid-password")
