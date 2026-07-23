from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, text

from app.config import BASE_DIR, Settings
from app.models.library import Library
from app.models.organization import (
    DEFAULT_ORGANIZATION_ID,
    DEFAULT_ORGANIZATION_SLUG,
    ORGANIZATION_PROFILES,
    Organization,
)
from app.models.organization_membership import OrganizationMembership
from app.models.user import User

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}[a-z0-9]$")


class DeploymentBootstrapError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BootstrapCommand:
    organization_slug: str
    organization_name: str
    deployment_profile: str
    admin_email: str
    admin_username: str | None
    password_hash: str

    def __post_init__(self) -> None:
        slug = self.organization_slug.strip().lower()
        name = self.organization_name.strip()
        email = self.admin_email.strip().lower()
        if not _SLUG_RE.fullmatch(slug):
            raise DeploymentBootstrapError("organization slug is invalid")
        if not name or len(name) > 160:
            raise DeploymentBootstrapError("organization name is invalid")
        if self.deployment_profile not in ORGANIZATION_PROFILES:
            raise DeploymentBootstrapError("deployment profile is invalid")
        if not email or len(email) > 320 or "@" not in email:
            raise DeploymentBootstrapError("administrator email is invalid")
        if not self.password_hash:
            raise DeploymentBootstrapError("administrator password hash is required")
        object.__setattr__(self, "organization_slug", slug)
        object.__setattr__(self, "organization_name", name)
        object.__setattr__(self, "admin_email", email)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    organization_id: uuid.UUID
    admin_user_id: uuid.UUID
    created: bool


async def prepare_document_storage(config: Settings) -> None:
    """Create local storage or verify the configured remote bucket."""
    from app.services.object_storage import build_object_storage_adapter

    adapter = build_object_storage_adapter(config)
    if adapter.provider == "local":
        root = Path(config.document_files_dir)
        root = root if root.is_absolute() else BASE_DIR / root
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise DeploymentBootstrapError("local document storage is unavailable")
        return
    try:
        if adapter.provider == "minio":
            available = await asyncio.to_thread(
                adapter._client.bucket_exists, adapter.bucket  # noqa: SLF001
            )
            if not available:
                raise DeploymentBootstrapError("remote document storage bucket is unavailable")
        else:
            await asyncio.to_thread(adapter._bucket_client.get_bucket_info)  # noqa: SLF001
    except DeploymentBootstrapError:
        raise
    except Exception as exc:
        raise DeploymentBootstrapError(
            "remote document storage bucket is unavailable"
        ) from exc


async def initialize_supported_deployment(db, command: BootstrapCommand) -> BootstrapResult:
    """Create or verify the first Organization/admin atomically and idempotently."""
    await db.execute(text("SELECT pg_advisory_xact_lock(94612009)"))
    organization = (
        await db.execute(
            select(Organization).where(Organization.slug == command.organization_slug)
        )
    ).scalar_one_or_none()
    user = (
        await db.execute(
            select(User).where(func.lower(User.email) == command.admin_email)
        )
    ).scalar_one_or_none()

    placeholder = None
    if organization is None and user is None:
        placeholder = await db.get(Organization, DEFAULT_ORGANIZATION_ID)
        user_count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        membership_count = (
            await db.execute(select(func.count()).select_from(OrganizationMembership))
        ).scalar_one()
        library_count = (await db.execute(select(func.count()).select_from(Library))).scalar_one()
        if (
            placeholder is not None
            and placeholder.slug == DEFAULT_ORGANIZATION_SLUG
            and user_count == 0
            and membership_count == 0
            and library_count == 0
        ):
            organization = placeholder

    if organization is not None or user is not None:
        if organization is placeholder and user is None:
            organization.slug = command.organization_slug
            organization.name = command.organization_name
            organization.deployment_profile = command.deployment_profile
        else:
            if organization is None or user is None:
                raise DeploymentBootstrapError("partial bootstrap state requires operator review")
            membership = (
                await db.execute(
                    select(OrganizationMembership).where(
                        OrganizationMembership.organization_id == organization.id,
                        OrganizationMembership.user_id == user.id,
                        OrganizationMembership.role == "organization_admin",
                        OrganizationMembership.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if (
                membership is None
                or organization.name != command.organization_name
                or organization.deployment_profile != command.deployment_profile
                or not user.is_active
                or not user.is_superuser
                or user.deleted_at is not None
            ):
                raise DeploymentBootstrapError("bootstrap identity does not match existing state")
            return BootstrapResult(organization.id, user.id, False)

    if organization is None:
        if (await db.execute(select(func.count()).select_from(Organization))).scalar_one():
            raise DeploymentBootstrapError("initial Organization already exists")
        organization = Organization(
            id=uuid.uuid4(),
            slug=command.organization_slug,
            name=command.organization_name,
            deployment_profile=command.deployment_profile,
            status="active",
        )
        db.add(organization)

    if (await db.execute(select(func.count()).select_from(User))).scalar_one():
        raise DeploymentBootstrapError("initial administrator already exists")

    user = User(
        id=uuid.uuid4(),
        email=command.admin_email,
        hashed_password=command.password_hash,
        is_active=True,
        is_superuser=True,
        is_verified=True,
        username=command.admin_username,
    )
    db.add(user)
    await db.flush()
    organization.created_by_user_id = user.id
    db.add(
        OrganizationMembership(
            id=uuid.uuid4(),
            organization_id=organization.id,
            user_id=user.id,
            role="organization_admin",
            status="active",
            created_by_user_id=user.id,
        )
    )
    await db.flush()
    return BootstrapResult(organization.id, user.id, True)
