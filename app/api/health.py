"""Content-free liveness and readiness probes."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.config import BASE_DIR, settings
from app.db import async_session_factory
from app.schemas.evidence_locator import EVIDENCE_LOCATOR_CONTRACT_VERSION
from app.services import selfcheck
from app.services.evidence_locator_projection import LOCATOR_PROJECTION_VERSION
from app.services.parser_units import PARSER_UNIT_CONTRACT_VERSION

log = logging.getLogger(__name__)
router = APIRouter()


def _migration_heads() -> frozenset[str]:
    """Load the supported Alembic heads from the checked-out migration graph."""
    return frozenset(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())


async def _check_db() -> bool:
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        log.warning("database readiness check failed", exc_info=True)
        return False


async def _check_migrations() -> bool:
    try:
        heads = _migration_heads()
        if not heads:
            return False
        async with async_session_factory() as session:
            revisions = {
                str(revision)
                for revision in (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalars()
            }
        return revisions == heads
    except Exception:  # noqa: BLE001
        log.warning("migration readiness check failed", exc_info=True)
        return False


async def _check_initialization() -> bool:
    if settings.deployment_profile == "development":
        return True
    try:
        async with async_session_factory() as session:
            initialized = (
                await session.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM sys_organizations o
                            JOIN sys_organization_memberships m
                              ON m.organization_id = o.id
                            JOIN sys_users u ON u.id = m.user_id
                            WHERE o.status = 'active'
                              AND m.status = 'active'
                              AND m.role = 'organization_admin'
                              AND u.is_active = true
                              AND u.is_superuser = true
                              AND u.deleted_at IS NULL
                        )
                        """
                    )
                )
            ).scalar_one()
        return bool(initialized)
    except Exception:  # noqa: BLE001
        log.warning("deployment initialization readiness check failed", exc_info=True)
        return False


async def _check_qdrant() -> bool:
    try:
        timeout = settings.readiness_dependency_timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{settings.qdrant_url.rstrip('/')}/healthz")
        return response.status_code == 200
    except Exception:  # noqa: BLE001
        log.warning("qdrant readiness check failed", exc_info=True)
        return False


async def _check_embedding() -> bool:
    try:
        ok, message, _ = await asyncio.wait_for(
            selfcheck.probe_embedding(),
            timeout=settings.readiness_dependency_timeout_seconds,
        )
        return ok and message == "ok"
    except Exception:  # noqa: BLE001
        log.warning("embedding readiness check failed", exc_info=True)
        return False


async def _check_rerank() -> str:
    try:
        state, message = await asyncio.wait_for(
            selfcheck.probe_rerank(),
            timeout=settings.readiness_dependency_timeout_seconds,
        )
        if isinstance(state, bool):
            return "ok" if state else "fail"
        if state == "off":
            return "not_configured" if message == "not configured" else "off"
        return state
    except Exception:  # noqa: BLE001
        log.warning("rerank readiness check failed", exc_info=True)
        return "fail"


async def _check_storage() -> bool:
    if not settings.revision_file_storage_enabled:
        return settings.deployment_profile == "development"
    try:
        from app.services.object_storage import build_object_storage_adapter

        adapter = build_object_storage_adapter(settings)
        if adapter.provider == "local":
            root = Path(settings.document_files_dir)
            root = root if root.is_absolute() else BASE_DIR / root
            return root.is_dir()
        if adapter.provider == "minio":
            return bool(
                await asyncio.to_thread(adapter._client.bucket_exists, adapter.bucket)  # noqa: SLF001
            )
        await asyncio.to_thread(adapter._bucket_client.get_bucket_info)  # noqa: SLF001
        return True
    except Exception:  # noqa: BLE001
        log.warning("object storage readiness check failed", exc_info=True)
        return False


def _check_ocr() -> str:
    """Keep the legacy internal diagnostic helper out of public responses."""
    from app.services import ocr

    if ocr.is_available():
        return "ok"
    return "missing" if settings.ocr_enabled else "off"


def _evidence_locator_payload() -> dict[str, object]:
    """Expose bounded contract versions and rollout state without source data."""
    return {
        "locator_contract_version": EVIDENCE_LOCATOR_CONTRACT_VERSION,
        "parser_unit_contract_version": PARSER_UNIT_CONTRACT_VERSION,
        "projection_version": LOCATOR_PROJECTION_VERSION,
        "read_enabled": settings.enable_evidence_locator_read,
        "projection_enabled": settings.enable_evidence_locator_projection,
        "write_path_enabled": settings.enable_evidence_write_path,
    }


def _payload(components: dict[str, bool | str]) -> dict[str, object]:
    failed = sorted(
        name
        for name, state in components.items()
        if state is False or state in {"fail", "not_configured"}
    )

    def label(state: bool | str) -> str:
        if state is True:
            return "ok"
        if state is False:
            return "fail"
        return state

    return {
        "status": "ready" if not failed else "not_ready",
        "components": {
            name: label(state) for name, state in sorted(components.items())
        },
        "failure_codes": [f"{name}_unavailable" for name in failed],
    }


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "live"}


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, object]:
    (
        db_ok,
        migrations_ok,
        initialization_ok,
        qdrant_ok,
        embedding_ok,
        rerank_state,
        storage_ok,
    ) = (
        await asyncio.gather(
            _check_db(),
            _check_migrations(),
            _check_initialization(),
            _check_qdrant(),
            _check_embedding(),
            _check_rerank(),
            _check_storage(),
        )
    )
    payload = _payload(
        {
            "database": db_ok,
            "migrations": migrations_ok,
            "initialization": initialization_ok,
            "qdrant": qdrant_ok,
            "embedding": embedding_ok,
            "rerank": rerank_state,
            "object_storage": storage_ok,
        }
    )
    if payload["status"] != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload


@router.get("/health")
async def health(response: Response) -> dict[str, object]:
    """Legacy content-free shape; degraded remains HTTP 200 for compatibility."""
    probe_response = Response()
    payload = await readiness(probe_response)
    components = payload["components"]
    assert isinstance(components, dict)
    return {
        "status": "ok" if payload["status"] == "ready" else "degraded",
        "db": components["database"] == "ok",
        "qdrant": components["qdrant"] == "ok",
        "embedding": components["embedding"],
        "rerank": components["rerank"],
        "ocr": _check_ocr(),
        "migrations": components["migrations"],
        "initialization": components["initialization"],
        "object_storage": components["object_storage"],
        "failure_codes": payload["failure_codes"],
        "evidence_locator": _evidence_locator_payload(),
    }
