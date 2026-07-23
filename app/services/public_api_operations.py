from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from sqlalchemy import and_, delete, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.db import async_session_factory
from app.models.api_key import ApiKey
from app.models.organization import Organization
from app.models.public_api_operations import (
    PublicAPIAnswerLease,
    PublicAPIRateWindow,
    PublicAPIRequestRecord,
)
from app.services.public_api_operations_contracts import (
    PublicAdmissionScope,
    PublicAnswerLeaseGrant,
    PublicEndpointKey,
    PublicOperationRecord,
    PublicOperationsMaintenanceResult,
    PublicRateAdmission,
)


log = logging.getLogger(__name__)
T = TypeVar("T")


class PublicOperationsError(RuntimeError):
    code = "public_operations_unavailable"

    def __init__(self, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(self.code)


class PublicRateLimitExceeded(PublicOperationsError):
    code = "rate_limited"

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, min(7_200, int(retry_after_seconds)))
        super().__init__(self.code)


class PublicAnswerTimedOut(PublicOperationsError):
    code = "answer_timed_out"


def organization_limits_apply(
    deployment_profile: str,
    *,
    config: Settings = settings,
) -> bool:
    if not config.public_api_limits_enabled:
        return False
    if deployment_profile == "hosted":
        return True
    return (
        deployment_profile == "private"
        and config.public_api_limit_private_organizations
    )


async def run_with_public_answer_timeout(
    awaitable: Awaitable[T],
    *,
    config: Settings = settings,
) -> T:
    try:
        return await asyncio.wait_for(
            awaitable,
            timeout=config.public_api_answer_max_seconds,
        )
    except TimeoutError:
        raise PublicAnswerTimedOut() from None


async def _database_now(db: AsyncSession, at: datetime | None) -> datetime:
    if at is not None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise PublicOperationsError("public_operations_time_invalid")
        return at.astimezone(timezone.utc)
    value = (await db.execute(select(func.clock_timestamp()))).scalar_one()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _locked_organization_profiles(
    db: AsyncSession,
    organization_ids: tuple[uuid.UUID, ...],
) -> dict[uuid.UUID, str]:
    profiles: dict[uuid.UUID, str] = {}
    for organization_id in sorted(organization_ids, key=str):
        row = (
            await db.execute(
                select(Organization.id, Organization.deployment_profile)
                .where(
                    Organization.id == organization_id,
                    Organization.status == "active",
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise PublicOperationsError("public_operations_scope_invalid")
        profiles[row.id] = row.deployment_profile
    return profiles


def _window_retry_after(current_time: datetime) -> int:
    window_start = current_time.replace(second=0, microsecond=0)
    return max(1, math.ceil((window_start + timedelta(minutes=1) - current_time).total_seconds()))


async def _increment_rate_window(
    db: AsyncSession,
    *,
    scope_kind: str,
    scope_id: uuid.UUID,
    window_started_at: datetime,
    limit: int,
    current_time: datetime,
) -> bool:
    statement = (
        pg_insert(PublicAPIRateWindow)
        .values(
            scope_kind=scope_kind,
            scope_id=scope_id,
            window_started_at=window_started_at,
            request_count=1,
            created_at=current_time,
            updated_at=current_time,
        )
        .on_conflict_do_update(
            index_elements=(
                PublicAPIRateWindow.scope_kind,
                PublicAPIRateWindow.scope_id,
                PublicAPIRateWindow.window_started_at,
            ),
            set_={
                "request_count": PublicAPIRateWindow.request_count + 1,
                "updated_at": current_time,
            },
            where=PublicAPIRateWindow.request_count < limit,
        )
        .returning(PublicAPIRateWindow.request_count)
    )
    return (await db.execute(statement)).scalar_one_or_none() is not None


async def admit_public_request(
    scope: PublicAdmissionScope,
    *,
    session_factory: Callable[[], Any] = async_session_factory,
    config: Settings = settings,
    at: datetime | None = None,
) -> PublicRateAdmission:
    if not config.public_api_limits_enabled:
        return PublicRateAdmission(scope.organization_ids, scope.api_key_id)
    try:
        async with session_factory() as db:
            async with db.begin():
                current_time = await _database_now(db, at)
                profiles = await _locked_organization_profiles(
                    db, scope.organization_ids
                )
                enforced = tuple(
                    organization_id
                    for organization_id in scope.organization_ids
                    if organization_limits_apply(
                        profiles[organization_id], config=config
                    )
                )
                if not enforced:
                    return PublicRateAdmission(scope.organization_ids, scope.api_key_id)
                window_start = current_time.replace(second=0, microsecond=0)
                for organization_id in enforced:
                    admitted = await _increment_rate_window(
                        db,
                        scope_kind="organization",
                        scope_id=organization_id,
                        window_started_at=window_start,
                        limit=config.public_api_organization_requests_per_minute,
                        current_time=current_time,
                    )
                    if not admitted:
                        raise PublicRateLimitExceeded(
                            _window_retry_after(current_time)
                        )
                if scope.api_key_id is not None:
                    admitted = await _increment_rate_window(
                        db,
                        scope_kind="api_key",
                        scope_id=scope.api_key_id,
                        window_started_at=window_start,
                        limit=config.public_api_api_key_requests_per_minute,
                        current_time=current_time,
                    )
                    if not admitted:
                        raise PublicRateLimitExceeded(
                            _window_retry_after(current_time)
                        )
        return PublicRateAdmission(
            scope.organization_ids,
            scope.api_key_id,
            window_started_at=window_start,
        )
    except PublicRateLimitExceeded:
        raise
    except PublicOperationsError:
        raise
    except Exception:  # noqa: BLE001
        raise PublicOperationsError() from None


async def acquire_public_answer_lease(
    *,
    request_id: str,
    endpoint_key: PublicEndpointKey,
    organization_id: uuid.UUID,
    api_key_id: uuid.UUID | None,
    session_factory: Callable[[], Any] = async_session_factory,
    config: Settings = settings,
    at: datetime | None = None,
) -> PublicAnswerLeaseGrant | None:
    if not config.public_api_limits_enabled:
        return None
    if endpoint_key not in {"answers.create", "answers.stream"}:
        raise PublicOperationsError("public_operations_endpoint_invalid")
    try:
        async with session_factory() as db:
            async with db.begin():
                current_time = await _database_now(db, at)
                organization = (
                    await db.execute(
                        select(Organization)
                        .where(
                            Organization.id == organization_id,
                            Organization.status == "active",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if organization is None:
                    raise PublicOperationsError("public_operations_scope_invalid")
                if not organization_limits_apply(
                    organization.deployment_profile, config=config
                ):
                    return None
                if api_key_id is not None:
                    key_id = (
                        await db.execute(
                            select(ApiKey.id)
                            .where(
                                ApiKey.id == api_key_id,
                                ApiKey.organization_id == organization_id,
                                ApiKey.revoked_at.is_(None),
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if key_id is None:
                        raise PublicOperationsError(
                            "public_operations_api_key_invalid"
                        )

                await db.execute(
                    delete(PublicAPIAnswerLease).where(
                        PublicAPIAnswerLease.expires_at <= current_time,
                        or_(
                            PublicAPIAnswerLease.organization_id == organization_id,
                            and_(
                                api_key_id is not None,
                                PublicAPIAnswerLease.api_key_id == api_key_id,
                            ),
                        ),
                    )
                )
                existing = (
                    await db.execute(
                        select(PublicAPIAnswerLease).where(
                            PublicAPIAnswerLease.request_id == request_id,
                            PublicAPIAnswerLease.expires_at > current_time,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if (
                        existing.organization_id != organization_id
                        or existing.api_key_id != api_key_id
                        or existing.endpoint_key != endpoint_key
                    ):
                        raise PublicOperationsError(
                            "public_operations_lease_conflict"
                        )
                    return PublicAnswerLeaseGrant(
                        request_id=existing.request_id,
                        organization_id=existing.organization_id,
                        api_key_id=existing.api_key_id,
                        expires_at=existing.expires_at,
                    )
                organization_count = (
                    await db.execute(
                        select(func.count())
                        .select_from(PublicAPIAnswerLease)
                        .where(
                            PublicAPIAnswerLease.organization_id == organization_id,
                            PublicAPIAnswerLease.expires_at > current_time,
                        )
                    )
                ).scalar_one()
                blocked_expiries: list[datetime] = []
                if (
                    organization_count
                    >= config.public_api_organization_concurrent_answers
                ):
                    blocked_expiries.append(
                        (
                            await db.execute(
                                select(func.min(PublicAPIAnswerLease.expires_at)).where(
                                    PublicAPIAnswerLease.organization_id
                                    == organization_id,
                                    PublicAPIAnswerLease.expires_at > current_time,
                                )
                            )
                        ).scalar_one()
                    )
                if api_key_id is not None:
                    api_key_count = (
                        await db.execute(
                            select(func.count())
                            .select_from(PublicAPIAnswerLease)
                            .where(
                                PublicAPIAnswerLease.api_key_id == api_key_id,
                                PublicAPIAnswerLease.expires_at > current_time,
                            )
                        )
                    ).scalar_one()
                    if api_key_count >= config.public_api_api_key_concurrent_answers:
                        blocked_expiries.append(
                            (
                                await db.execute(
                                    select(func.min(PublicAPIAnswerLease.expires_at)).where(
                                        PublicAPIAnswerLease.api_key_id == api_key_id,
                                        PublicAPIAnswerLease.expires_at > current_time,
                                    )
                                )
                            ).scalar_one()
                        )
                if blocked_expiries:
                    retry_at = max(blocked_expiries)
                    raise PublicRateLimitExceeded(
                        max(1, math.ceil((retry_at - current_time).total_seconds()))
                    )

                expires_at = current_time + timedelta(
                    seconds=config.public_api_answer_lease_seconds
                )
                db.add(
                    PublicAPIAnswerLease(
                        id=uuid.uuid4(),
                        request_id=request_id,
                        organization_id=organization_id,
                        api_key_id=api_key_id,
                        endpoint_key=endpoint_key,
                        acquired_at=current_time,
                        expires_at=expires_at,
                    )
                )
        return PublicAnswerLeaseGrant(
            request_id=request_id,
            organization_id=organization_id,
            api_key_id=api_key_id,
            expires_at=expires_at,
        )
    except PublicRateLimitExceeded:
        raise
    except PublicOperationsError:
        raise
    except Exception:  # noqa: BLE001
        raise PublicOperationsError() from None


async def release_public_answer_lease(
    request_id: str,
    *,
    session_factory: Callable[[], Any] = async_session_factory,
) -> bool:
    try:
        async with session_factory() as db:
            async with db.begin():
                result = await db.execute(
                    delete(PublicAPIAnswerLease).where(
                        PublicAPIAnswerLease.request_id == request_id
                    )
                )
        return bool(result.rowcount)
    except Exception:  # noqa: BLE001
        log.warning(
            "public API answer lease release failed: request_id=%s",
            request_id,
        )
        return False


async def record_public_operation(
    record: PublicOperationRecord,
    *,
    session_factory: Callable[[], Any] = async_session_factory,
    config: Settings = settings,
) -> bool:
    if not config.public_api_operations_enabled:
        return False
    values = record.model_dump(mode="python")
    values["id"] = uuid.uuid4()
    values["library_ids"] = list(record.library_ids)
    statement = (
        pg_insert(PublicAPIRequestRecord)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=(PublicAPIRequestRecord.request_id,)
        )
        .returning(PublicAPIRequestRecord.id)
    )
    try:
        async with session_factory() as db:
            async with db.begin():
                inserted = (await db.execute(statement)).scalar_one_or_none()
        return inserted is not None
    except Exception:  # noqa: BLE001
        log.warning(
            "public API terminal record failed: request_id=%s endpoint=%s "
            "failure_code=record_write_failed",
            record.request_id,
            record.endpoint_key,
        )
        return False


async def _delete_request_records(
    db: AsyncSession,
    *,
    cutoff: datetime,
    batch_size: int,
) -> int:
    ids = tuple(
        (
            await db.execute(
                select(PublicAPIRequestRecord.id)
                .where(PublicAPIRequestRecord.finished_at < cutoff)
                .order_by(
                    PublicAPIRequestRecord.finished_at.asc(),
                    PublicAPIRequestRecord.id.asc(),
                )
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            )
        ).scalars()
    )
    if not ids:
        return 0
    result = await db.execute(
        delete(PublicAPIRequestRecord).where(PublicAPIRequestRecord.id.in_(ids))
    )
    return result.rowcount or 0


async def _delete_rate_windows(
    db: AsyncSession,
    *,
    cutoff: datetime,
    batch_size: int,
) -> int:
    keys = tuple(
        (
            await db.execute(
                select(
                    PublicAPIRateWindow.scope_kind,
                    PublicAPIRateWindow.scope_id,
                    PublicAPIRateWindow.window_started_at,
                )
                .where(PublicAPIRateWindow.window_started_at < cutoff)
                .order_by(
                    PublicAPIRateWindow.window_started_at.asc(),
                    PublicAPIRateWindow.scope_kind.asc(),
                    PublicAPIRateWindow.scope_id.asc(),
                )
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            )
        ).tuples()
    )
    if not keys:
        return 0
    result = await db.execute(
        delete(PublicAPIRateWindow).where(
            tuple_(
                PublicAPIRateWindow.scope_kind,
                PublicAPIRateWindow.scope_id,
                PublicAPIRateWindow.window_started_at,
            ).in_(keys)
        )
    )
    return result.rowcount or 0


async def _delete_answer_leases(
    db: AsyncSession,
    *,
    cutoff: datetime,
    batch_size: int,
) -> int:
    ids = tuple(
        (
            await db.execute(
                select(PublicAPIAnswerLease.id)
                .where(PublicAPIAnswerLease.expires_at <= cutoff)
                .order_by(
                    PublicAPIAnswerLease.expires_at.asc(),
                    PublicAPIAnswerLease.id.asc(),
                )
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            )
        ).scalars()
    )
    if not ids:
        return 0
    result = await db.execute(
        delete(PublicAPIAnswerLease).where(PublicAPIAnswerLease.id.in_(ids))
    )
    return result.rowcount or 0


async def run_public_api_operations_maintenance(
    db: AsyncSession,
    *,
    at: datetime | None = None,
    batch_size: int | None = None,
    config: Settings = settings,
) -> PublicOperationsMaintenanceResult:
    if not config.public_api_operations_enabled:
        return PublicOperationsMaintenanceResult()
    current_time = await _database_now(db, at)
    limit = batch_size or config.public_api_cleanup_batch_size
    if not 1 <= limit <= 10_000:
        raise PublicOperationsError("public_operations_cleanup_limit_invalid")
    request_count = await _delete_request_records(
        db,
        cutoff=current_time
        - timedelta(days=config.public_api_request_retention_days),
        batch_size=limit,
    )
    window_count = await _delete_rate_windows(
        db,
        cutoff=current_time.replace(second=0, microsecond=0)
        - timedelta(minutes=2),
        batch_size=limit,
    )
    lease_count = await _delete_answer_leases(
        db,
        cutoff=current_time,
        batch_size=limit,
    )
    return PublicOperationsMaintenanceResult(
        request_records_deleted=request_count,
        rate_windows_deleted=window_count,
        answer_leases_deleted=lease_count,
    )
