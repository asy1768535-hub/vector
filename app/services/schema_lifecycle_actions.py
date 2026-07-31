from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attribute_definition import AttributeDefinition
from app.models.entity import Entity
from app.models.entity_type import EntityType
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_governance_action import GraphGovernanceAction
from app.models.graph_publication import GraphPublication
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.models.schema_lifecycle_action import SchemaLifecycleAction
from app.services import audit_log, ontology
from app.services.schema_lifecycle_contracts import (
    SchemaLifecycleCommand,
    SchemaLifecycleError,
    deterministic_schema_child_id,
    deterministic_schema_clone_id,
)
from app.services.schema_lifecycle_read import (
    SchemaVersionBundle,
    load_schema_version_bundle,
    schema_version_state_hash,
)
from app.services.schema_lifecycle_validation import validate_schema_draft_bundle


_DRAFT = "draft"
_ACTIVE = "active"
_DISABLED = "disabled"
_DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class SchemaLifecycleActionResult:
    action: SchemaLifecycleAction
    bundle: SchemaVersionBundle
    reused: bool


@dataclass(frozen=True, slots=True)
class SchemaVersionDeletionResult:
    action: SchemaLifecycleAction
    version_id: uuid.UUID
    reused: bool


def _clone_status(status: str) -> str:
    if status == _ACTIVE:
        return _DRAFT
    if status == _DISABLED:
        return _DISABLED
    raise SchemaLifecycleError(
        "schema_lifecycle_unavailable", "Active Schema contains invalid child status"
    )


def clone_schema_bundle_rows(
    source: SchemaVersionBundle,
    *,
    draft_version_id: uuid.UUID,
    version_no: int,
    description: str | None,
) -> SchemaVersionBundle:
    if source.version.status != _ACTIVE or version_no <= source.version.version_no:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema clone source is invalid"
        )
    draft = OntologyVersion(
        id=draft_version_id,
        library_id=source.version.library_id,
        version_key=source.version.version_key,
        version_no=version_no,
        status=_DRAFT,
        description=description,
        parent_version_id=source.version.id,
    )
    entity_ids = {
        row.id: deterministic_schema_child_id(
            draft_version_id, row.id, "entity_type"
        )
        for row in source.entity_types
    }
    relation_ids = {
        row.id: deterministic_schema_child_id(
            draft_version_id, row.id, "relation_type"
        )
        for row in source.relation_types
    }
    entities = tuple(
        EntityType(
            id=entity_ids[row.id],
            library_id=draft.library_id,
            ontology_version_id=draft.id,
            key=row.key,
            label=row.label,
            description=row.description,
            properties_schema=deepcopy(row.properties_schema),
            is_seeded=row.is_seeded,
            status=_clone_status(row.status),
        )
        for row in source.entity_types
    )
    relations = tuple(
        RelationType(
            id=relation_ids[row.id],
            library_id=draft.library_id,
            ontology_version_id=draft.id,
            key=row.key,
            label=row.label,
            description=row.description,
            direction=row.direction,
            requires_evidence=row.requires_evidence,
            default_review_policy=row.default_review_policy,
            properties_schema=deepcopy(row.properties_schema),
            is_seeded=row.is_seeded,
            status=_clone_status(row.status),
        )
        for row in source.relation_types
    )
    attributes: list[AttributeDefinition] = []
    for row in source.attributes:
        owner_ids = entity_ids if row.owner_kind == "entity_type" else relation_ids
        owner_id = owner_ids.get(row.owner_type_id)
        if owner_id is None:
            raise SchemaLifecycleError(
                "schema_lifecycle_unavailable", "Schema attribute owner is invalid"
            )
        attributes.append(
            AttributeDefinition(
                id=deterministic_schema_child_id(draft.id, row.id, "attribute"),
                library_id=draft.library_id,
                ontology_version_id=draft.id,
                owner_kind=row.owner_kind,
                owner_type_id=owner_id,
                key=row.key,
                label=row.label,
                value_type=row.value_type,
                required=row.required,
                enum_values=deepcopy(row.enum_values),
                validation_schema=deepcopy(row.validation_schema),
                indexed=row.indexed,
                status=_clone_status(row.status),
            )
        )
    constraints: list[RelationTypeConstraint] = []
    for row in source.constraints:
        relation_id = relation_ids.get(row.relation_type_id)
        source_id = entity_ids.get(row.source_entity_type_id)
        target_id = entity_ids.get(row.target_entity_type_id)
        if relation_id is None or source_id is None or target_id is None:
            raise SchemaLifecycleError(
                "schema_lifecycle_unavailable", "Schema constraint endpoint is invalid"
            )
        constraints.append(
            RelationTypeConstraint(
                id=deterministic_schema_child_id(draft.id, row.id, "constraint"),
                library_id=draft.library_id,
                ontology_version_id=draft.id,
                relation_type_id=relation_id,
                source_entity_type_id=source_id,
                target_entity_type_id=target_id,
                cardinality=row.cardinality,
                requires_review=row.requires_review,
                status=_clone_status(row.status),
            )
        )
    return SchemaVersionBundle(
        version=draft,
        entity_types=entities,
        relation_types=relations,
        attributes=tuple(attributes),
        constraints=tuple(constraints),
    )


async def _lock_library(db: AsyncSession, library: Library) -> None:
    row = (
        await db.execute(
            select(Library).where(Library.id == library.id).with_for_update()
        )
    ).scalars().first()
    if row is None or row.deleted_at is not None:
        raise SchemaLifecycleError(
            "schema_lifecycle_not_found", "Library not found"
        )


async def _find_action(
    db: AsyncSession,
    command: SchemaLifecycleCommand,
) -> SchemaLifecycleAction | None:
    return (
        await db.execute(
            select(SchemaLifecycleAction).where(
                SchemaLifecycleAction.library_id == command.library_id,
                SchemaLifecycleAction.idempotency_key == command.idempotency_key,
            )
        )
    ).scalars().first()


async def _replay(
    db: AsyncSession,
    library: Library,
    command: SchemaLifecycleCommand,
) -> SchemaLifecycleActionResult | None:
    action = await _replay_action(db, command)
    if action is None:
        return None
    version_id = action.result_payload.get("version_id")
    try:
        parsed_version_id = uuid.UUID(str(version_id))
    except (TypeError, ValueError) as exc:
        raise SchemaLifecycleError(
            "schema_lifecycle_unavailable", "Stored Schema action is invalid"
        ) from exc
    bundle = await load_schema_version_bundle(db, library, parsed_version_id)
    return SchemaLifecycleActionResult(action=action, bundle=bundle, reused=True)


async def _replay_action(
    db: AsyncSession,
    command: SchemaLifecycleCommand,
) -> SchemaLifecycleAction | None:
    action = await _find_action(db, command)
    if action is not None and action.command_hash != command.command_hash:
        raise SchemaLifecycleError(
            "schema_lifecycle_idempotency_conflict", "Idempotency key is in use"
        )
    return action


async def _record_action(
    db: AsyncSession,
    *,
    command: SchemaLifecycleCommand,
    version: OntologyVersion,
    target_id: uuid.UUID,
    state_hash: str,
    item_count: int,
) -> SchemaLifecycleAction:
    action = SchemaLifecycleAction(
        library_id=command.library_id,
        ontology_version_id=version.id,
        action_kind=command.action_kind,
        target_kind=command.target_kind,
        target_id=target_id,
        expected_state_hash=command.expected_state_hash,
        command_hash=command.command_hash,
        idempotency_key=command.idempotency_key,
        result_payload={
            "item_count": item_count,
            "state_hash": state_hash,
            "target_id": str(target_id),
            "target_kind": command.target_kind,
            "version_id": str(version.id),
            "version_no": version.version_no,
        },
        actor_user_id=command.actor_user_id,
    )
    db.add(action)
    await db.flush()
    await audit_log.record(
        db,
        command.actor_user_id,
        f"schema_lifecycle.{command.action_kind}",
        {
            "action_id": str(action.id),
            "item_count": item_count,
            "library_id": str(command.library_id),
            "ontology_version_id": str(version.id),
            "state_hash": state_hash,
            "target_id": str(target_id),
            "target_kind": command.target_kind,
            "version_no": version.version_no,
        },
    )
    return action


async def clone_schema_version(
    db: AsyncSession,
    library: Library,
    command: SchemaLifecycleCommand,
) -> SchemaLifecycleActionResult:
    if (
        command.library_id != library.id
        or command.action_kind != "clone_version"
        or command.target_kind != "ontology_version"
        or command.target_id != command.ontology_version_id
    ):
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema clone command is invalid"
        )
    await _lock_library(db, library)
    replay = await _replay(db, library, command)
    if replay is not None:
        return replay
    source = await load_schema_version_bundle(
        db, library, command.ontology_version_id, for_update=True
    )
    if source.version.status != _ACTIVE:
        raise SchemaLifecycleError(
            "schema_lifecycle_state_changed", "Schema clone source changed"
        )
    if schema_version_state_hash(source) != command.expected_state_hash:
        raise SchemaLifecycleError(
            "schema_lifecycle_state_changed", "Schema clone source changed"
        )
    family = (
        await db.execute(
            select(OntologyVersion)
            .where(
                OntologyVersion.library_id == library.id,
                OntologyVersion.version_key == source.version.version_key,
            )
            .with_for_update()
        )
    ).scalars().all()
    version_no = max((row.version_no for row in family), default=0) + 1
    draft_id = deterministic_schema_clone_id(
        library.id,
        source.version.id,
        command.idempotency_key,
        command.command_hash,
    )
    description = command.payload.get("description", source.version.description)
    draft = clone_schema_bundle_rows(
        source,
        draft_version_id=draft_id,
        version_no=version_no,
        description=description,
    )
    db.add(draft.version)
    await db.flush()
    db.add_all([*draft.entity_types, *draft.relation_types])
    await db.flush()
    db.add_all([*draft.attributes, *draft.constraints])
    await db.flush()
    state_hash = schema_version_state_hash(draft)
    item_count = sum(
        len(rows)
        for rows in (
            draft.entity_types,
            draft.relation_types,
            draft.attributes,
            draft.constraints,
        )
    )
    action = await _record_action(
        db,
        command=command,
        version=draft.version,
        target_id=draft.version.id,
        state_hash=state_hash,
        item_count=item_count,
    )
    return SchemaLifecycleActionResult(action=action, bundle=draft, reused=False)


def _require_draft(bundle: SchemaVersionBundle, expected_hash: str) -> None:
    if bundle.version.status != _DRAFT:
        raise SchemaLifecycleError(
            "schema_lifecycle_state_changed", "Only a draft Schema can be edited"
        )
    if schema_version_state_hash(bundle) != expected_hash:
        raise SchemaLifecycleError(
            "schema_lifecycle_state_changed", "Schema draft changed"
        )


def _row_for_command(bundle: SchemaVersionBundle, command: SchemaLifecycleCommand):
    collection = {
        "entity_type": bundle.entity_types,
        "relation_type": bundle.relation_types,
        "attribute": bundle.attributes,
        "constraint": bundle.constraints,
    }.get(command.target_kind)
    if collection is None:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema item kind is invalid"
        )
    row = next((item for item in collection if item.id == command.target_id), None)
    if row is None:
        raise SchemaLifecycleError(
            "schema_lifecycle_not_found", "Schema item not found"
        )
    return row


def _require_exact_payload(
    payload: dict[str, Any],
    *,
    required: set[str],
    optional: set[str],
) -> None:
    keys = set(payload)
    if not required <= keys or not keys <= required | optional:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema item payload is invalid"
        )


def _require_create_unique(bundle: SchemaVersionBundle, command: SchemaLifecycleCommand) -> None:
    payload = command.payload
    if command.target_kind == "entity_type":
        conflict = any(row.key == payload["key"] for row in bundle.entity_types)
    elif command.target_kind == "relation_type":
        conflict = any(row.key == payload["key"] for row in bundle.relation_types)
    elif command.target_kind == "attribute":
        conflict = any(
            row.owner_kind == payload["owner_kind"]
            and str(row.owner_type_id) == str(payload["owner_type_id"])
            and row.key == payload["key"]
            for row in bundle.attributes
        )
    else:
        conflict = any(
            str(row.relation_type_id) == str(payload["relation_type_id"])
            and str(row.source_entity_type_id)
            == str(payload["source_entity_type_id"])
            and str(row.target_entity_type_id)
            == str(payload["target_entity_type_id"])
            for row in bundle.constraints
        )
    if conflict:
        raise SchemaLifecycleError(
            "schema_lifecycle_dependency_conflict", "Schema item already exists"
        )


def _payload_uuid(payload: dict[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload[key]))
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema reference is invalid"
        ) from exc


def _require_create_references(
    bundle: SchemaVersionBundle,
    command: SchemaLifecycleCommand,
) -> None:
    payload = command.payload
    if command.target_kind == "attribute":
        owner_kind = payload["owner_kind"]
        if owner_kind == "entity_type":
            owners = bundle.entity_types
        elif owner_kind == "relation_type":
            owners = bundle.relation_types
        else:
            raise SchemaLifecycleError(
                "schema_lifecycle_request_invalid", "Schema owner kind is invalid"
            )
        owner_id = _payload_uuid(payload, "owner_type_id")
        if not any(row.id == owner_id and row.status == _DRAFT for row in owners):
            raise SchemaLifecycleError(
                "schema_lifecycle_dependency_conflict",
                "Schema attribute owner is unavailable",
            )
        return
    if command.target_kind != "constraint":
        return
    references = (
        (bundle.relation_types, _payload_uuid(payload, "relation_type_id")),
        (bundle.entity_types, _payload_uuid(payload, "source_entity_type_id")),
        (bundle.entity_types, _payload_uuid(payload, "target_entity_type_id")),
    )
    if any(
        not any(row.id == target_id and row.status == _DRAFT for row in rows)
        for rows, target_id in references
    ):
        raise SchemaLifecycleError(
            "schema_lifecycle_dependency_conflict",
            "Schema constraint endpoint is unavailable",
        )


async def _create_item(
    db: AsyncSession,
    library: Library,
    bundle: SchemaVersionBundle,
    command: SchemaLifecycleCommand,
):
    payload = command.payload
    if command.target_kind == "entity_type":
        _require_exact_payload(
            payload,
            required={"key", "label"},
            optional={"description", "properties_schema"},
        )
        _require_create_unique(bundle, command)
        return await ontology.create_entity_type(
            db,
            library,
            bundle.version.id,
            object_id=command.target_id,
            key=payload["key"],
            label=payload["label"],
            description=payload.get("description"),
            properties_schema=payload.get("properties_schema"),
        )
    if command.target_kind == "relation_type":
        _require_exact_payload(
            payload,
            required={"key", "label", "direction", "default_review_policy"},
            optional={
                "description",
                "requires_evidence",
                "properties_schema",
            },
        )
        _require_create_unique(bundle, command)
        return await ontology.create_relation_type(
            db,
            library,
            bundle.version.id,
            object_id=command.target_id,
            key=payload["key"],
            label=payload["label"],
            direction=payload["direction"],
            default_review_policy=payload["default_review_policy"],
            description=payload.get("description"),
            requires_evidence=payload.get("requires_evidence", True),
            properties_schema=payload.get("properties_schema"),
        )
    if command.target_kind == "attribute":
        _require_exact_payload(
            payload,
            required={
                "owner_kind",
                "owner_type_id",
                "key",
                "label",
                "value_type",
            },
            optional={
                "required",
                "enum_values",
                "validation_schema",
                "indexed",
            },
        )
        _require_create_references(bundle, command)
        _require_create_unique(bundle, command)
        return await ontology.create_attribute_definition(
            db,
            library,
            bundle.version.id,
            object_id=command.target_id,
            owner_kind=payload["owner_kind"],
            owner_type_id=uuid.UUID(str(payload["owner_type_id"])),
            key=payload["key"],
            label=payload["label"],
            value_type=payload["value_type"],
            required=payload.get("required", False),
            enum_values=payload.get("enum_values"),
            validation_schema=payload.get("validation_schema"),
            indexed=payload.get("indexed", False),
        )
    _require_exact_payload(
        payload,
        required={
            "relation_type_id",
            "source_entity_type_id",
            "target_entity_type_id",
        },
        optional={"cardinality", "requires_review"},
    )
    _require_create_references(bundle, command)
    _require_create_unique(bundle, command)
    return await ontology.create_relation_type_constraint(
        db,
        library,
        bundle.version.id,
        object_id=command.target_id,
        relation_type_id=uuid.UUID(str(payload["relation_type_id"])),
        source_entity_type_id=uuid.UUID(str(payload["source_entity_type_id"])),
        target_entity_type_id=uuid.UUID(str(payload["target_entity_type_id"])),
        cardinality=payload.get("cardinality"),
        requires_review=payload.get("requires_review", False),
    )


def _require_disable_dependencies(
    bundle: SchemaVersionBundle,
    command: SchemaLifecycleCommand,
) -> None:
    if command.target_kind == "entity_type":
        dependent = any(
            row.status == _DRAFT
            and command.target_id
            in {row.source_entity_type_id, row.target_entity_type_id}
            for row in bundle.constraints
        ) or any(
            row.status == _DRAFT
            and row.owner_kind == "entity_type"
            and row.owner_type_id == command.target_id
            for row in bundle.attributes
        )
    elif command.target_kind == "relation_type":
        dependent = any(
            row.status == _DRAFT and row.relation_type_id == command.target_id
            for row in bundle.constraints
        ) or any(
            row.status == _DRAFT
            and row.owner_kind == "relation_type"
            and row.owner_type_id == command.target_id
            for row in bundle.attributes
        )
    else:
        dependent = False
    if dependent:
        raise SchemaLifecycleError(
            "schema_lifecycle_dependency_conflict",
            "Disable dependent Schema items first",
        )


async def _update_or_disable_item(
    db: AsyncSession,
    library: Library,
    bundle: SchemaVersionBundle,
    command: SchemaLifecycleCommand,
):
    row = _row_for_command(bundle, command)
    if row.status != _DRAFT:
        raise SchemaLifecycleError(
            "schema_lifecycle_state_changed", "Only enabled draft items can change"
        )
    if command.action_kind == "disable_item":
        if command.payload:
            raise SchemaLifecycleError(
                "schema_lifecycle_request_invalid", "Disable payload must be empty"
            )
        _require_disable_dependencies(bundle, command)
        changes = {"status": _DISABLED}
    else:
        changes = dict(command.payload)
    if command.target_kind == "entity_type":
        allowed = {"label", "description", "properties_schema"}
        operation = ontology.update_entity_type
    elif command.target_kind == "relation_type":
        allowed = {
            "label",
            "description",
            "direction",
            "requires_evidence",
            "default_review_policy",
            "properties_schema",
        }
        operation = ontology.update_relation_type
    elif command.target_kind == "attribute":
        allowed = {
            "label",
            "value_type",
            "required",
            "enum_values",
            "validation_schema",
            "indexed",
        }
        operation = ontology.update_attribute_definition
    else:
        allowed = {"cardinality", "requires_review"}
        operation = ontology.update_relation_type_constraint
    if command.action_kind != "disable_item" and (
        not changes or not set(changes) <= allowed
    ):
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema item update is invalid"
        )
    return await operation(db, library, command.target_id, **changes)


async def apply_schema_item_command(
    db: AsyncSession,
    library: Library,
    command: SchemaLifecycleCommand,
) -> SchemaLifecycleActionResult:
    if (
        command.library_id != library.id
        or command.ontology_version_id != command.target_id
        and command.target_kind == "ontology_version"
        or command.action_kind
        not in {"create_item", "update_item", "disable_item"}
        or command.target_kind == "ontology_version"
    ):
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema item command is invalid"
        )
    await _lock_library(db, library)
    replay = await _replay(db, library, command)
    if replay is not None:
        return replay
    bundle = await load_schema_version_bundle(
        db, library, command.ontology_version_id, for_update=True
    )
    _require_draft(bundle, command.expected_state_hash)
    if command.action_kind == "create_item":
        await _create_item(db, library, bundle, command)
    else:
        await _update_or_disable_item(db, library, bundle, command)
    await db.flush()
    updated = await load_schema_version_bundle(db, library, bundle.version.id)
    state_hash = schema_version_state_hash(updated)
    action = await _record_action(
        db,
        command=command,
        version=updated.version,
        target_id=command.target_id,
        state_hash=state_hash,
        item_count=1,
    )
    return SchemaLifecycleActionResult(action=action, bundle=updated, reused=False)


async def activate_schema_version(
    db: AsyncSession,
    library: Library,
    command: SchemaLifecycleCommand,
) -> SchemaLifecycleActionResult:
    if (
        command.library_id != library.id
        or command.action_kind != "activate_version"
        or command.target_kind != "ontology_version"
        or command.target_id != command.ontology_version_id
    ):
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema activation command is invalid"
        )
    _require_exact_payload(
        command.payload,
        required={"confirmation", "expected_active_version_id"},
        optional=set(),
    )
    if command.payload["confirmation"] != "activate_schema_version":
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema activation is not confirmed"
        )
    expected_active_id = command.payload["expected_active_version_id"]
    if expected_active_id is not None:
        try:
            expected_active_id = uuid.UUID(str(expected_active_id))
        except (TypeError, ValueError) as exc:
            raise SchemaLifecycleError(
                "schema_lifecycle_request_invalid", "Expected active Schema is invalid"
            ) from exc

    await _lock_library(db, library)
    replay = await _replay(db, library, command)
    if replay is not None:
        return replay
    draft = await load_schema_version_bundle(
        db, library, command.ontology_version_id, for_update=True
    )
    _require_draft(draft, command.expected_state_hash)
    versions = (
        await db.execute(
            select(OntologyVersion)
            .where(
                OntologyVersion.library_id == library.id,
                OntologyVersion.version_key == draft.version.version_key,
                OntologyVersion.status.in_((_ACTIVE, _DRAFT)),
            )
            .order_by(OntologyVersion.id)
            .with_for_update()
        )
    ).scalars().all()
    active_versions = [row for row in versions if row.status == _ACTIVE]
    if len(active_versions) > 1:
        raise SchemaLifecycleError(
            "schema_lifecycle_invalid_draft", "Active Schema identity is ambiguous"
        )
    active = active_versions[0] if active_versions else None
    if (active.id if active is not None else None) != expected_active_id:
        raise SchemaLifecycleError(
            "schema_lifecycle_state_changed", "Active Schema version changed"
        )
    if active is not None and active.version_key != draft.version.version_key:
        raise SchemaLifecycleError(
            "schema_lifecycle_invalid_draft", "Schema version family is incompatible"
        )
    validation = validate_schema_draft_bundle(library, draft)
    if not validation.valid:
        raise SchemaLifecycleError(
            "schema_lifecycle_invalid_draft", "Schema draft validation failed"
        )

    if active is not None:
        active.status = _DISABLED
        await db.flush()
    for row in (
        *draft.entity_types,
        *draft.relation_types,
        *draft.attributes,
        *draft.constraints,
    ):
        if row.status == _DRAFT:
            row.status = _ACTIVE
    draft.version.status = _ACTIVE
    draft.version.published_at = datetime.now(timezone.utc)
    await db.flush()
    activated = await load_schema_version_bundle(db, library, draft.version.id)
    state_hash = schema_version_state_hash(activated)
    action = await _record_action(
        db,
        command=command,
        version=activated.version,
        target_id=activated.version.id,
        state_hash=state_hash,
        item_count=sum(
            len(rows)
            for rows in (
                activated.entity_types,
                activated.relation_types,
                activated.attributes,
                activated.constraints,
            )
        ),
    )
    return SchemaLifecycleActionResult(
        action=action,
        bundle=activated,
        reused=False,
    )


async def _require_unreferenced_draft(
    db: AsyncSession,
    version_id: uuid.UUID,
) -> None:
    reference_models = (
        Entity,
        KnowledgeRelation,
        GraphExtractionJob,
        GraphPublication,
        GraphGovernanceAction,
    )
    for model in reference_models:
        reference_id = (
            await db.execute(
                select(model.id)
                .where(model.ontology_version_id == version_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if reference_id is not None:
            raise SchemaLifecycleError(
                "schema_lifecycle_dependency_conflict",
                "Schema draft is referenced and cannot be deleted",
            )
    child_version_id = (
        await db.execute(
            select(OntologyVersion.id)
            .where(
                OntologyVersion.parent_version_id == version_id,
                OntologyVersion.status != _DELETED,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if child_version_id is not None:
        raise SchemaLifecycleError(
            "schema_lifecycle_dependency_conflict",
            "Schema draft has a child version and cannot be deleted",
        )


async def delete_schema_draft(
    db: AsyncSession,
    library: Library,
    command: SchemaLifecycleCommand,
) -> SchemaVersionDeletionResult:
    if (
        command.library_id != library.id
        or command.action_kind != "delete_version"
        or command.target_kind != "ontology_version"
        or command.target_id != command.ontology_version_id
    ):
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema deletion command is invalid"
        )
    _require_exact_payload(command.payload, required={"confirmation"}, optional=set())
    if command.payload["confirmation"] != "delete_schema_draft":
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema deletion is not confirmed"
        )

    await _lock_library(db, library)
    replay = await _replay_action(db, command)
    if replay is not None:
        return SchemaVersionDeletionResult(
            action=replay,
            version_id=command.ontology_version_id,
            reused=True,
        )
    draft = await load_schema_version_bundle(
        db, library, command.ontology_version_id, for_update=True
    )
    _require_draft(draft, command.expected_state_hash)
    await _require_unreferenced_draft(db, draft.version.id)

    rows = (
        *draft.entity_types,
        *draft.relation_types,
        *draft.attributes,
        *draft.constraints,
    )
    for row in rows:
        row.status = _DELETED
    draft.version.status = _DELETED
    await db.flush()
    state_hash = schema_version_state_hash(draft)
    action = await _record_action(
        db,
        command=command,
        version=draft.version,
        target_id=draft.version.id,
        state_hash=state_hash,
        item_count=len(rows),
    )
    return SchemaVersionDeletionResult(
        action=action,
        version_id=draft.version.id,
        reused=False,
    )


async def disable_schema_version(
    db: AsyncSession,
    library: Library,
    command: SchemaLifecycleCommand,
) -> SchemaLifecycleActionResult:
    if (
        command.library_id != library.id
        or command.action_kind != "disable_version"
        or command.target_kind != "ontology_version"
        or command.target_id != command.ontology_version_id
    ):
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema disable command is invalid"
        )
    _require_exact_payload(command.payload, required={"confirmation"}, optional=set())
    if command.payload["confirmation"] != "disable_schema_version":
        raise SchemaLifecycleError(
            "schema_lifecycle_request_invalid", "Schema disable is not confirmed"
        )

    await _lock_library(db, library)
    replay = await _replay(db, library, command)
    if replay is not None:
        return replay
    bundle = await load_schema_version_bundle(
        db, library, command.ontology_version_id, for_update=True
    )
    if (
        bundle.version.status != _ACTIVE
        or schema_version_state_hash(bundle) != command.expected_state_hash
    ):
        raise SchemaLifecycleError(
            "schema_lifecycle_state_changed", "Only the active Schema can be disabled"
        )

    bundle.version.status = _DISABLED
    await db.flush()
    disabled = await load_schema_version_bundle(db, library, bundle.version.id)
    state_hash = schema_version_state_hash(disabled)
    item_count = sum(
        len(rows)
        for rows in (
            disabled.entity_types,
            disabled.relation_types,
            disabled.attributes,
            disabled.constraints,
        )
    )
    action = await _record_action(
        db,
        command=command,
        version=disabled.version,
        target_id=disabled.version.id,
        state_hash=state_hash,
        item_count=item_count,
    )
    return SchemaLifecycleActionResult(
        action=action,
        bundle=disabled,
        reused=False,
    )
