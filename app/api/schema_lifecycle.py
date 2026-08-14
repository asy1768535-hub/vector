from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.models.library import Library
from app.models.user import User
from app.schemas.schema_lifecycle import (
    SchemaActivationRequest,
    SchemaAttributeCreateRequest,
    SchemaAttributeUpdateRequest,
    SchemaCloneRequest,
    SchemaCommandResultRead,
    SchemaConstraintCreateRequest,
    SchemaConstraintUpdateRequest,
    SchemaDraftDeleteRequest,
    SchemaEntityTypeCreateRequest,
    SchemaEntityTypeUpdateRequest,
    SchemaImpactRead,
    SchemaImportFileRequest,
    SchemaImportRequest,
    SchemaItemDisableRequest,
    SchemaRelationTypeCreateRequest,
    SchemaRelationTypeUpdateRequest,
    SchemaValidationRead,
    SchemaVersionDeletionResultRead,
    SchemaVersionDetailRead,
    SchemaVersionDisableRequest,
    SchemaVersionListRead,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    load_active_library,
    resolve_loaded_library_management,
)
from app.services.schema_lifecycle_actions import (
    SchemaLifecycleActionResult,
    SchemaVersionDeletionResult,
    activate_schema_version,
    apply_schema_item_command,
    clone_schema_version,
    delete_schema_draft,
    disable_schema_version,
    import_schema_version,
)
from app.services.schema_lifecycle_contracts import (
    SchemaLifecycleCommand,
    SchemaLifecycleError,
    deterministic_schema_import_id,
    deterministic_schema_item_id,
)
from app.services.schema_lifecycle_impact import preview_schema_impact
from app.services.schema_lifecycle_read import (
    list_schema_versions,
    load_schema_version_bundle,
    schema_version_detail,
)
from app.services.schema_lifecycle_validation import validate_schema_draft_bundle
from app.services.schema_import_file import parse_schema_import_file


router = APIRouter(
    prefix="/libraries/{slug}/schema-lifecycle",
    tags=["schema-lifecycle"],
)


@dataclass(frozen=True, slots=True)
class SchemaLifecycleContext:
    user: User
    library: Library


def _require_enabled() -> None:
    if not settings.schema_lifecycle_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


async def _load_context(
    slug: str,
    user: User,
    db: AsyncSession,
) -> SchemaLifecycleContext:
    _require_enabled()
    library = await load_active_library(slug, db)
    if library is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    try:
        await resolve_loaded_library_management(db, user=user, library=library)
    except OrganizationAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    return SchemaLifecycleContext(user=user, library=library)


async def require_schema_lifecycle_context(
    slug: str,
    user: User = Depends(current_cookie_user),
    db: AsyncSession = Depends(get_db),
) -> SchemaLifecycleContext:
    return await _load_context(slug, user, db)


def _http_error(exc: SchemaLifecycleError) -> HTTPException:
    if exc.code == "schema_lifecycle_forbidden":
        return HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if exc.code == "schema_lifecycle_not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if exc.code == "schema_lifecycle_request_invalid":
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)
    if exc.code in {
        "schema_lifecycle_state_changed",
        "schema_lifecycle_idempotency_conflict",
        "schema_lifecycle_invalid_draft",
        "schema_lifecycle_dependency_conflict",
    }:
        return HTTPException(status.HTTP_409_CONFLICT, exc.code)
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "schema_lifecycle_unavailable",
    )


async def _read(operation: Awaitable):
    try:
        return await operation
    except SchemaLifecycleError as exc:
        raise _http_error(exc) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "schema_lifecycle_unavailable",
        ) from exc


def _command_response(
    context: SchemaLifecycleContext,
    result: SchemaLifecycleActionResult,
) -> SchemaCommandResultRead:
    return SchemaCommandResultRead(
        action_id=result.action.id,
        reused=result.reused,
        version=schema_version_detail(context.library, result.bundle),
    )


def _read_value(operation: Callable[[], object]):
    try:
        return operation()
    except SchemaLifecycleError as exc:
        raise _http_error(exc) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "schema_lifecycle_unavailable",
        ) from exc


async def _mutate(
    db: AsyncSession,
    context: SchemaLifecycleContext,
    operation: Callable[[], Awaitable[SchemaLifecycleActionResult]],
) -> SchemaCommandResultRead:
    try:
        result = await operation()
        response = _command_response(context, result)
        await db.commit()
        return response
    except SchemaLifecycleError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "schema_lifecycle_unavailable",
        ) from exc


async def _mutate_deletion(
    db: AsyncSession,
    context: SchemaLifecycleContext,
    operation: Callable[[], Awaitable[SchemaVersionDeletionResult]],
) -> SchemaVersionDeletionResultRead:
    try:
        result = await operation()
        response = SchemaVersionDeletionResultRead(
            action_id=result.action.id,
            reused=result.reused,
            library_id=context.library.id,
            ontology_version_id=result.version_id,
            status="deleted",
        )
        await db.commit()
        return response
    except SchemaLifecycleError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "schema_lifecycle_unavailable",
        ) from exc


def _payload(body, *, exclude: set[str]) -> dict:
    return body.model_dump(mode="json", exclude=exclude, exclude_unset=True)


def _command(
    context: SchemaLifecycleContext,
    version_id: uuid.UUID,
    *,
    action_kind: str,
    target_kind: str,
    target_id: uuid.UUID,
    expected_state_hash: str,
    idempotency_key: str,
    payload: dict,
) -> SchemaLifecycleCommand:
    return SchemaLifecycleCommand(
        library_id=context.library.id,
        ontology_version_id=version_id,
        actor_user_id=context.user.id,
        action_kind=action_kind,
        target_kind=target_kind,
        target_id=target_id,
        expected_state_hash=expected_state_hash,
        idempotency_key=idempotency_key,
        payload=payload,
    )


async def _import_version_request(
    body: SchemaImportRequest,
    context: SchemaLifecycleContext,
    db: AsyncSession,
) -> SchemaCommandResultRead:
    version_id = deterministic_schema_import_id(
        context.library.id, body.idempotency_key
    )
    command = _command(
        context,
        version_id,
        action_kind="import_version",
        target_kind="ontology_version",
        target_id=version_id,
        expected_state_hash="0" * 64,
        idempotency_key=body.idempotency_key,
        payload=body.model_dump(mode="json", exclude={"idempotency_key"}),
    )
    return await _mutate(
        db,
        context,
        lambda: import_schema_version(db, context.library, command, body),
    )


@router.post("/import", response_model=SchemaCommandResultRead)
async def import_version(
    body: SchemaImportRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _import_version_request(body, context, db)


@router.post("/import-file", response_model=SchemaCommandResultRead)
async def import_file(
    body: SchemaImportFileRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    parsed = _read_value(lambda: parse_schema_import_file(body))
    return await _import_version_request(parsed, context, db)


@router.get("/versions", response_model=SchemaVersionListRead)
async def list_versions(
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaVersionListRead:
    return await _read(list_schema_versions(db, context.library))


@router.get("/versions/{version_id}", response_model=SchemaVersionDetailRead)
async def get_version(
    version_id: uuid.UUID,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaVersionDetailRead:
    bundle = await _read(load_schema_version_bundle(db, context.library, version_id))
    return _read_value(lambda: schema_version_detail(context.library, bundle))


@router.post(
    "/versions/{version_id}/clone",
    response_model=SchemaCommandResultRead,
)
async def clone_version(
    version_id: uuid.UUID,
    body: SchemaCloneRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    command = _command(
        context,
        version_id,
        action_kind="clone_version",
        target_kind="ontology_version",
        target_id=version_id,
        expected_state_hash=body.expected_version_state_hash,
        idempotency_key=body.idempotency_key,
        payload=_payload(
            body,
            exclude={"expected_version_state_hash", "idempotency_key"},
        ),
    )
    return await _mutate(
        db,
        context,
        lambda: clone_schema_version(db, context.library, command),
    )


@router.post(
    "/versions/{version_id}/validate",
    response_model=SchemaValidationRead,
)
async def validate_version(
    version_id: uuid.UUID,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaValidationRead:
    bundle = await _read(load_schema_version_bundle(db, context.library, version_id))
    return _read_value(lambda: validate_schema_draft_bundle(context.library, bundle))


@router.get(
    "/versions/{version_id}/impact",
    response_model=SchemaImpactRead,
)
async def get_version_impact(
    version_id: uuid.UUID,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaImpactRead:
    bundle = await _read(load_schema_version_bundle(db, context.library, version_id))
    return await _read(
        preview_schema_impact(
            db,
            user=context.user,
            library=context.library,
            draft=bundle,
        )
    )


@router.post(
    "/versions/{version_id}/activate",
    response_model=SchemaCommandResultRead,
)
async def activate_version(
    version_id: uuid.UUID,
    body: SchemaActivationRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    command = _command(
        context,
        version_id,
        action_kind="activate_version",
        target_kind="ontology_version",
        target_id=version_id,
        expected_state_hash=body.expected_version_state_hash,
        idempotency_key=body.idempotency_key,
        payload=_payload(
            body,
            exclude={"expected_version_state_hash", "idempotency_key"},
        ),
    )
    return await _mutate(
        db,
        context,
        lambda: activate_schema_version(db, context.library, command),
    )


@router.post(
    "/versions/{version_id}/delete-draft",
    response_model=SchemaVersionDeletionResultRead,
)
async def delete_draft_version(
    version_id: uuid.UUID,
    body: SchemaDraftDeleteRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaVersionDeletionResultRead:
    command = _command(
        context,
        version_id,
        action_kind="delete_version",
        target_kind="ontology_version",
        target_id=version_id,
        expected_state_hash=body.expected_version_state_hash,
        idempotency_key=body.idempotency_key,
        payload=_payload(
            body,
            exclude={"expected_version_state_hash", "idempotency_key"},
        ),
    )
    return await _mutate_deletion(
        db,
        context,
        lambda: delete_schema_draft(db, context.library, command),
    )


@router.post(
    "/versions/{version_id}/disable",
    response_model=SchemaCommandResultRead,
)
async def disable_version(
    version_id: uuid.UUID,
    body: SchemaVersionDisableRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    command = _command(
        context,
        version_id,
        action_kind="disable_version",
        target_kind="ontology_version",
        target_id=version_id,
        expected_state_hash=body.expected_version_state_hash,
        idempotency_key=body.idempotency_key,
        payload=_payload(
            body,
            exclude={"expected_version_state_hash", "idempotency_key"},
        ),
    )
    return await _mutate(
        db,
        context,
        lambda: disable_schema_version(db, context.library, command),
    )


async def _create_item(
    version_id: uuid.UUID,
    body,
    target_kind: str,
    context: SchemaLifecycleContext,
    db: AsyncSession,
) -> SchemaCommandResultRead:
    target_id = deterministic_schema_item_id(
        version_id, target_kind, body.idempotency_key
    )
    command = _command(
        context,
        version_id,
        action_kind="create_item",
        target_kind=target_kind,
        target_id=target_id,
        expected_state_hash=body.expected_version_state_hash,
        idempotency_key=body.idempotency_key,
        payload=_payload(
            body,
            exclude={"expected_version_state_hash", "idempotency_key"},
        ),
    )
    return await _mutate(
        db,
        context,
        lambda: apply_schema_item_command(db, context.library, command),
    )


async def _change_item(
    version_id: uuid.UUID,
    item_id: uuid.UUID,
    body,
    target_kind: str,
    action_kind: str,
    context: SchemaLifecycleContext,
    db: AsyncSession,
) -> SchemaCommandResultRead:
    command = _command(
        context,
        version_id,
        action_kind=action_kind,
        target_kind=target_kind,
        target_id=item_id,
        expected_state_hash=body.expected_version_state_hash,
        idempotency_key=body.idempotency_key,
        payload=_payload(
            body,
            exclude={"expected_version_state_hash", "idempotency_key"},
        ),
    )
    return await _mutate(
        db,
        context,
        lambda: apply_schema_item_command(db, context.library, command),
    )


@router.post("/versions/{version_id}/entity-types", response_model=SchemaCommandResultRead)
async def create_entity_type(
    version_id: uuid.UUID,
    body: SchemaEntityTypeCreateRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _create_item(version_id, body, "entity_type", context, db)


@router.patch(
    "/versions/{version_id}/entity-types/{item_id}",
    response_model=SchemaCommandResultRead,
)
async def update_entity_type(
    version_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SchemaEntityTypeUpdateRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _change_item(
        version_id, item_id, body, "entity_type", "update_item", context, db
    )


@router.post(
    "/versions/{version_id}/entity-types/{item_id}/disable",
    response_model=SchemaCommandResultRead,
)
async def disable_entity_type(
    version_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SchemaItemDisableRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _change_item(
        version_id, item_id, body, "entity_type", "disable_item", context, db
    )


@router.post("/versions/{version_id}/relation-types", response_model=SchemaCommandResultRead)
async def create_relation_type(
    version_id: uuid.UUID,
    body: SchemaRelationTypeCreateRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _create_item(version_id, body, "relation_type", context, db)


@router.patch(
    "/versions/{version_id}/relation-types/{item_id}",
    response_model=SchemaCommandResultRead,
)
async def update_relation_type(
    version_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SchemaRelationTypeUpdateRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _change_item(
        version_id, item_id, body, "relation_type", "update_item", context, db
    )


@router.post(
    "/versions/{version_id}/relation-types/{item_id}/disable",
    response_model=SchemaCommandResultRead,
)
async def disable_relation_type(
    version_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SchemaItemDisableRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _change_item(
        version_id, item_id, body, "relation_type", "disable_item", context, db
    )


@router.post("/versions/{version_id}/attributes", response_model=SchemaCommandResultRead)
async def create_attribute(
    version_id: uuid.UUID,
    body: SchemaAttributeCreateRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _create_item(version_id, body, "attribute", context, db)


@router.patch(
    "/versions/{version_id}/attributes/{item_id}",
    response_model=SchemaCommandResultRead,
)
async def update_attribute(
    version_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SchemaAttributeUpdateRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _change_item(
        version_id, item_id, body, "attribute", "update_item", context, db
    )


@router.post(
    "/versions/{version_id}/attributes/{item_id}/disable",
    response_model=SchemaCommandResultRead,
)
async def disable_attribute(
    version_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SchemaItemDisableRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _change_item(
        version_id, item_id, body, "attribute", "disable_item", context, db
    )


@router.post("/versions/{version_id}/constraints", response_model=SchemaCommandResultRead)
async def create_constraint(
    version_id: uuid.UUID,
    body: SchemaConstraintCreateRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _create_item(version_id, body, "constraint", context, db)


@router.patch(
    "/versions/{version_id}/constraints/{item_id}",
    response_model=SchemaCommandResultRead,
)
async def update_constraint(
    version_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SchemaConstraintUpdateRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _change_item(
        version_id, item_id, body, "constraint", "update_item", context, db
    )


@router.post(
    "/versions/{version_id}/constraints/{item_id}/disable",
    response_model=SchemaCommandResultRead,
)
async def disable_constraint(
    version_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SchemaItemDisableRequest,
    context: SchemaLifecycleContext = Depends(require_schema_lifecycle_context),
    db: AsyncSession = Depends(get_db),
) -> SchemaCommandResultRead:
    return await _change_item(
        version_id, item_id, body, "constraint", "disable_item", context, db
    )
