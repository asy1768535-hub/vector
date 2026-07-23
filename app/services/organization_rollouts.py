from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.models.organization import Organization
from app.models.organization_capability_rollout import (
    ORGANIZATION_CAPABILITIES,
    OrganizationCapabilityRollout,
)
from app.services import audit_log


class OrganizationRolloutError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RolloutState:
    capability: str
    enabled: bool
    version: int


async def list_rollouts(db, organization_id: uuid.UUID) -> tuple[RolloutState, ...]:
    organization = await db.get(Organization, organization_id)
    if organization is None or organization.status != "active":
        raise OrganizationRolloutError("organization_not_found")
    rows = (
        await db.execute(
            select(OrganizationCapabilityRollout).where(
                OrganizationCapabilityRollout.organization_id == organization_id
            )
        )
    ).scalars().all()
    by_capability = {row.capability: row for row in rows}
    return tuple(
        RolloutState(
            capability=capability,
            enabled=by_capability[capability].enabled if capability in by_capability else False,
            version=by_capability[capability].version if capability in by_capability else 0,
        )
        for capability in ORGANIZATION_CAPABILITIES
    )


async def require_rollout_enabled(
    db,
    *,
    organization_id: uuid.UUID,
    capability: str,
) -> None:
    """Fail closed in supported profiles; development keeps legacy behavior."""
    from app.config import settings

    if settings.deployment_profile == "development":
        return
    if capability not in ORGANIZATION_CAPABILITIES:
        raise OrganizationRolloutError("capability_invalid")
    enabled = (
        await db.execute(
            select(OrganizationCapabilityRollout.enabled).where(
                OrganizationCapabilityRollout.organization_id == organization_id,
                OrganizationCapabilityRollout.capability == capability,
            )
        )
    ).scalar_one_or_none()
    if enabled is not True:
        raise OrganizationRolloutError("capability_disabled")


async def set_rollout(
    db,
    *,
    organization_id: uuid.UUID,
    capability: str,
    enabled: bool,
    expected_version: int,
    actor_user_id: uuid.UUID,
) -> RolloutState:
    if capability not in ORGANIZATION_CAPABILITIES:
        raise OrganizationRolloutError("capability_invalid")
    organization = (
        await db.execute(
            select(Organization)
            .where(Organization.id == organization_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if organization is None or organization.status != "active":
        raise OrganizationRolloutError("organization_not_found")
    row = (
        await db.execute(
            select(OrganizationCapabilityRollout)
            .where(
                OrganizationCapabilityRollout.organization_id == organization_id,
                OrganizationCapabilityRollout.capability == capability,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    actual_version = row.version if row is not None else 0
    if expected_version != actual_version:
        raise OrganizationRolloutError("rollout_version_conflict")
    if row is None:
        row = OrganizationCapabilityRollout(
            id=uuid.uuid4(),
            organization_id=organization_id,
            capability=capability,
            enabled=enabled,
            version=1,
            changed_by_user_id=actor_user_id,
        )
        db.add(row)
    else:
        row.enabled = enabled
        row.version += 1
        row.changed_by_user_id = actor_user_id
    await db.flush()
    await audit_log.record(
        db,
        actor_user_id,
        "organization.rollout_change",
        {
            "organization_id": str(organization_id),
            "capability": capability,
            "enabled": enabled,
            "version": row.version,
        },
    )
    return RolloutState(row.capability, row.enabled, row.version)
