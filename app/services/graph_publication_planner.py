from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models.document import Document
from app.models.document_revision import REVISION_STATUS_READY, DocumentRevision
from app.models.entity import (
    GRAPH_FACT_STATUS_ACTIVE,
    GRAPH_FACT_STATUS_DRAFT,
    GRAPH_SOURCE_EXTRACTED,
    Entity,
)
from app.models.entity_mention import EntityMention
from app.models.entity_type import SCHEMA_STATUS_ACTIVE, EntityType
from app.models.evidence_unit import EVIDENCE_STATUS_ACTIVE, EvidenceUnit
from app.models.graph_publication import (
    GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE,
    GRAPH_PUBLICATION_SOURCE_INITIAL_SEED,
    GRAPH_PUBLICATION_SOURCE_MANUAL_PLAN,
    GraphPublication,
)
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import ONTOLOGY_STATUS_ACTIVE, OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services.graph_schema_validator import (
    EntityTypeRule,
    RelationConstraintRule,
    RelationTypeRule,
    validate_entity_shape,
    validate_relation_shape,
)


PUBLICATION_CURRENT_STATUSES = ("active", "degraded")
PUBLICATION_REUSABLE_STATUSES = ("planned", "activating", "active", "degraded")
PUBLISHABLE_FACT_STATUSES = {GRAPH_FACT_STATUS_ACTIVE, GRAPH_FACT_STATUS_DRAFT}
SUPPORT_TYPE_SUPPORTS = "supports"
SUPPORT_TYPE_CONTRADICTS = "contradicts"


class GraphPublicationPlanError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GraphPublicationPlanResult:
    publication: GraphPublication
    items: tuple[GraphPublicationItem, ...]
    manifest_hash: str
    policy_snapshot_hash: str
    blocked_counts: dict[str, int]
    dry_run: bool = False
    reused: bool = False


@dataclass(frozen=True, slots=True)
class GraphPublicationSnapshot:
    items: tuple[GraphPublicationItem, ...]
    blocked_counts: dict[str, int]
    policy_snapshot: dict[str, Any]
    policy_snapshot_hash: str
    governance_action_set_hash: str | None = None


@dataclass(frozen=True, slots=True)
class GraphPublicationProjection:
    entity_states: dict[uuid.UUID, dict[str, Any]]
    relation_states: dict[uuid.UUID, dict[str, Any]]
    action_ids: tuple[uuid.UUID, ...] = ()
    action_set_hash: str | None = None

    def __post_init__(self) -> None:
        if (
            any(not isinstance(key, uuid.UUID) or not isinstance(value, dict) for key, value in self.entity_states.items())
            or any(not isinstance(key, uuid.UUID) or not isinstance(value, dict) for key, value in self.relation_states.items())
            or any(not isinstance(value, uuid.UUID) for value in self.action_ids)
            or len(set(self.action_ids)) != len(self.action_ids)
            or (self.action_ids and self.action_set_hash is None)
            or (
                self.action_set_hash is not None
                and (
                    len(self.action_set_hash) != 64
                    or any(character not in "0123456789abcdef" for character in self.action_set_hash)
                )
            )
        ):
            raise GraphPublicationPlanError(
                "governance_projection_invalid",
                "graph governance projection is invalid",
            )


def _project_entity(entity: Entity, state: dict[str, Any]) -> SimpleNamespace:
    allowed = {
        "authority_level",
        "canonical_name",
        "confidence",
        "normalized_name",
        "properties",
        "status",
    }
    if not set(state).issubset(
        allowed
        | {
            "entity_type_id",
            "library_id",
            "ontology_version_id",
            "source_type",
        }
    ):
        raise GraphPublicationPlanError(
            "governance_projection_invalid", "entity projection is invalid"
        )
    identity = {
        "entity_type_id": str(entity.entity_type_id),
        "library_id": str(entity.library_id),
        "ontology_version_id": str(entity.ontology_version_id),
        "source_type": entity.source_type,
    }
    if any(key in state and state[key] != value for key, value in identity.items()):
        raise GraphPublicationPlanError(
            "governance_projection_invalid", "entity projection identity changed"
        )
    values = {
        key: deepcopy(getattr(entity, key))
        for key in {
            "authority_level",
            "canonical_name",
            "confidence",
            "entity_type_id",
            "id",
            "library_id",
            "normalized_name",
            "ontology_version_id",
            "properties",
            "source_type",
            "status",
        }
    }
    values.update({key: deepcopy(state[key]) for key in allowed.intersection(state)})
    return SimpleNamespace(**values)


def _project_relation(
    relation: KnowledgeRelation, state: dict[str, Any]
) -> SimpleNamespace:
    allowed = {
        "authority_level",
        "confidence",
        "properties",
        "review_status",
        "source_entity_id",
        "status",
        "target_entity_id",
    }
    if not set(state).issubset(
        allowed
        | {
            "library_id",
            "ontology_version_id",
            "relation_type_id",
            "source_type",
        }
    ):
        raise GraphPublicationPlanError(
            "governance_projection_invalid", "relation projection is invalid"
        )
    identity = {
        "library_id": str(relation.library_id),
        "ontology_version_id": str(relation.ontology_version_id),
        "relation_type_id": str(relation.relation_type_id),
        "source_type": relation.source_type,
    }
    if any(key in state and state[key] != value for key, value in identity.items()):
        raise GraphPublicationPlanError(
            "governance_projection_invalid", "relation projection identity changed"
        )
    values = {
        key: deepcopy(getattr(relation, key))
        for key in {
            "authority_level",
            "confidence",
            "id",
            "library_id",
            "ontology_version_id",
            "properties",
            "relation_type_id",
            "review_status",
            "source_entity_id",
            "source_type",
            "status",
            "target_entity_id",
        }
    }
    for key in allowed.intersection(state):
        value = state[key]
        if key in {"source_entity_id", "target_entity_id"}:
            try:
                value = uuid.UUID(str(value))
            except (TypeError, ValueError, AttributeError):
                raise GraphPublicationPlanError(
                    "governance_projection_invalid", "relation endpoint is invalid"
                ) from None
        values[key] = deepcopy(value)
    return SimpleNamespace(**values)


def _uuid_text(value: uuid.UUID | None) -> str | None:
    return str(value).lower() if value is not None else None


def _confidence_text(value: float | None) -> str | None:
    if value is None:
        return None
    decimal_value = Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return format(decimal_value, "f")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalized_properties(properties: dict[str, Any] | None) -> dict[str, Any]:
    if properties is None:
        return {}
    return dict(properties)


def build_publication_policy_snapshot(config: Settings = settings) -> dict[str, Any]:
    return {
        "require_entity_evidence": config.graph_publication_require_entity_evidence,
        "extracted_entity_min_confidence": _confidence_text(
            config.graph_publication_extracted_entity_min_confidence
        ),
        "extracted_relation_min_confidence": _confidence_text(
            config.graph_publication_extracted_relation_min_confidence
        ),
        "max_items_per_run": config.graph_publication_max_items_per_run,
        "relation_support_types": [SUPPORT_TYPE_SUPPORTS],
        "manifest_version": config.graph_publication_manifest_version,
        "policy_version": config.graph_publication_policy_version,
    }


def _blocking_add(blocked: dict[str, int], reason: str) -> None:
    blocked[reason] = blocked.get(reason, 0) + 1


async def _active_ontology(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID | None,
) -> OntologyVersion:
    if ontology_version_id is not None:
        ontology = await db.get(OntologyVersion, ontology_version_id)
        if ontology is None or ontology.library_id != library.id:
            raise GraphPublicationPlanError("ontology_not_found", "ontology version not found")
        if ontology.status != ONTOLOGY_STATUS_ACTIVE:
            raise GraphPublicationPlanError("ontology_not_active", "ontology version is not active")
        return ontology
    result = await db.execute(
        select(OntologyVersion)
        .where(
            OntologyVersion.library_id == library.id,
            OntologyVersion.status == ONTOLOGY_STATUS_ACTIVE,
        )
        .order_by(OntologyVersion.created_at.desc())
        .limit(1)
    )
    ontology = result.scalars().first()
    if ontology is None:
        raise GraphPublicationPlanError("active_ontology_not_found", "active ontology version not found")
    return ontology


async def _list_rows(db: AsyncSession, statement) -> list[Any]:
    result = await db.execute(statement)
    return list(result.scalars().all())


async def _lock_publication_scope(db: AsyncSession, library_id: uuid.UUID) -> None:
    await db.execute(select(Library.id).where(Library.id == library_id).with_for_update())


async def _load_current_publication(
    db: AsyncSession,
    *,
    library_id: uuid.UUID,
    ontology_version_id: uuid.UUID,
) -> GraphPublication | None:
    result = await db.execute(
        select(GraphPublication)
        .where(
            GraphPublication.library_id == library_id,
            GraphPublication.ontology_version_id == ontology_version_id,
            GraphPublication.status.in_(PUBLICATION_CURRENT_STATUSES),
        )
        .limit(1)
    )
    return result.scalars().first()


async def _load_reusable_publication(
    db: AsyncSession,
    *,
    library_id: uuid.UUID,
    ontology_version_id: uuid.UUID,
    manifest_hash: str,
) -> GraphPublication | None:
    result = await db.execute(
        select(GraphPublication)
        .where(
            GraphPublication.library_id == library_id,
            GraphPublication.ontology_version_id == ontology_version_id,
            GraphPublication.manifest_hash == manifest_hash,
            GraphPublication.status.in_(PUBLICATION_REUSABLE_STATUSES),
        )
        .limit(1)
    )
    return result.scalars().first()


async def _load_idempotent_publication(
    db: AsyncSession,
    *,
    library_id: uuid.UUID,
    ontology_version_id: uuid.UUID,
    idempotency_key: str,
) -> GraphPublication | None:
    result = await db.execute(
        select(GraphPublication)
        .where(
            GraphPublication.library_id == library_id,
            GraphPublication.ontology_version_id == ontology_version_id,
            GraphPublication.idempotency_key == idempotency_key,
            GraphPublication.status.in_(("planned", "activating")),
        )
        .limit(1)
    )
    return result.scalars().first()


def _entity_type_rule(row: EntityType) -> EntityTypeRule:
    return EntityTypeRule(
        id=row.id,
        ontology_version_id=row.ontology_version_id,
        key=row.key,
        properties_schema=row.properties_schema,
    )


def _relation_type_rule(row: RelationType) -> RelationTypeRule:
    return RelationTypeRule(
        id=row.id,
        ontology_version_id=row.ontology_version_id,
        key=row.key,
        direction=row.direction,
        requires_evidence=row.requires_evidence,
        default_review_policy=row.default_review_policy,
        properties_schema=row.properties_schema,
    )


def _relation_constraint_rule(row: RelationTypeConstraint | None) -> RelationConstraintRule | None:
    if row is None:
        return None
    return RelationConstraintRule(
        source_entity_type_id=row.source_entity_type_id,
        target_entity_type_id=row.target_entity_type_id,
        cardinality=row.cardinality,
        requires_review=row.requires_review,
    )


def _support_ids(rows: Iterable[RelationEvidence | EntityMention]) -> list[str]:
    return sorted(_uuid_text(row.evidence_id) for row in rows if row.evidence_id is not None)


async def _current_revision_safe(db: AsyncSession, library: Library, row: RelationEvidence | EntityMention) -> bool:
    evidence = await db.get(EvidenceUnit, row.evidence_id)
    if evidence is None or evidence.library_id != library.id or evidence.status != EVIDENCE_STATUS_ACTIVE:
        return False
    if evidence.document_id != row.document_id or evidence.document_revision_id != row.document_revision_id:
        return False
    document = await db.get(Document, row.document_id)
    if (
        document is None
        or document.library_id != library.id
        or document.deleted_at is not None
        or document.status != "ready"
        or document.current_revision_id != row.document_revision_id
    ):
        return False
    revision = await db.get(DocumentRevision, row.document_revision_id)
    return bool(
        revision is not None
        and revision.library_id == library.id
        and revision.document_id == row.document_id
        and revision.status == REVISION_STATUS_READY
        and row.library_id == library.id
    )


def _entity_item_hash(
    *,
    config: Settings,
    library_id: uuid.UUID,
    entity: Entity,
    support_evidence_ids: list[str],
) -> tuple[str, dict[str, Any], str]:
    properties = _normalized_properties(entity.properties)
    properties_hash = _sha256_json(properties)
    snapshot = {
        "manifest_version": config.graph_publication_manifest_version,
        "item_kind": "entity",
        "library_id": _uuid_text(library_id),
        "ontology_version_id": _uuid_text(entity.ontology_version_id),
        "entity_id": _uuid_text(entity.id),
        "entity_type_id": _uuid_text(entity.entity_type_id),
        "canonical_name": entity.canonical_name,
        "normalized_name": entity.normalized_name,
        "properties_hash": properties_hash,
        "source_type": entity.source_type,
        "confidence": _confidence_text(entity.confidence),
        "support_evidence_ids": support_evidence_ids,
    }
    return _sha256_json(snapshot), snapshot, properties_hash


def _relation_item_hash(
    *,
    config: Settings,
    library_id: uuid.UUID,
    relation: KnowledgeRelation,
    support_evidence_ids: list[str],
) -> tuple[str, dict[str, Any], str]:
    properties = _normalized_properties(relation.properties)
    properties_hash = _sha256_json(properties)
    snapshot = {
        "manifest_version": config.graph_publication_manifest_version,
        "item_kind": "relation",
        "library_id": _uuid_text(library_id),
        "ontology_version_id": _uuid_text(relation.ontology_version_id),
        "relation_id": _uuid_text(relation.id),
        "relation_type_id": _uuid_text(relation.relation_type_id),
        "source_entity_id": _uuid_text(relation.source_entity_id),
        "target_entity_id": _uuid_text(relation.target_entity_id),
        "properties_hash": properties_hash,
        "source_type": relation.source_type,
        "confidence": _confidence_text(relation.confidence),
        "support_evidence_ids": support_evidence_ids,
    }
    return _sha256_json(snapshot), snapshot, properties_hash


async def _eligible_entity_item(
    db: AsyncSession,
    *,
    library: Library,
    entity: Entity,
    entity_types: dict[uuid.UUID, EntityType],
    mentions_by_entity: dict[uuid.UUID, list[EntityMention]],
    config: Settings,
    blocked: dict[str, int],
) -> GraphPublicationItem | None:
    if entity.status not in PUBLISHABLE_FACT_STATUSES:
        _blocking_add(blocked, f"entity_status_{entity.status}")
        return None
    entity_type = entity_types.get(entity.entity_type_id)
    if entity_type is None or entity_type.status != SCHEMA_STATUS_ACTIVE:
        _blocking_add(blocked, "entity_type_not_active")
        return None
    try:
        validate_entity_shape(
            entity_type=_entity_type_rule(entity_type),
            canonical_name=entity.canonical_name,
            properties=_normalized_properties(entity.properties),
        )
    except ValueError:
        _blocking_add(blocked, "entity_schema_invalid")
        return None
    active_mentions = []
    for mention in mentions_by_entity.get(entity.id, []):
        if mention.status == "active" and await _current_revision_safe(db, library, mention):
            active_mentions.append(mention)
    if (
        config.graph_publication_require_entity_evidence
        and entity.source_type == GRAPH_SOURCE_EXTRACTED
        and not active_mentions
    ):
        _blocking_add(blocked, "entity_evidence_incomplete")
        return None
    if (
        entity.source_type == GRAPH_SOURCE_EXTRACTED
        and (
            entity.confidence is None
            or entity.confidence < config.graph_publication_extracted_entity_min_confidence
        )
    ):
        _blocking_add(blocked, "entity_low_confidence")
        return None
    support_evidence_ids = _support_ids(active_mentions)
    item_hash, snapshot, _properties_hash = _entity_item_hash(
        config=config,
        library_id=library.id,
        entity=entity,
        support_evidence_ids=support_evidence_ids,
    )
    return GraphPublicationItem(
        library_id=library.id,
        ontology_version_id=entity.ontology_version_id,
        item_kind="entity",
        entity_id=entity.id,
        item_hash=item_hash,
        status="planned",
        support_evidence_ids=support_evidence_ids,
        support_counts={"active_mentions": len(active_mentions)},
        fact_snapshot=snapshot,
    )


async def _eligible_relation_item(
    db: AsyncSession,
    *,
    library: Library,
    relation: KnowledgeRelation,
    relation_types: dict[uuid.UUID, RelationType],
    relation_constraints: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], RelationTypeConstraint],
    published_entities: dict[uuid.UUID, Entity],
    evidence_by_relation: dict[uuid.UUID, list[RelationEvidence]],
    config: Settings,
    blocked: dict[str, int],
) -> GraphPublicationItem | None:
    if relation.status not in PUBLISHABLE_FACT_STATUSES:
        _blocking_add(blocked, f"relation_status_{relation.status}")
        return None
    if relation.review_status not in {"not_required", "approved"}:
        _blocking_add(blocked, "relation_review_blocked")
        return None
    source_entity = published_entities.get(relation.source_entity_id)
    target_entity = published_entities.get(relation.target_entity_id)
    if source_entity is None or target_entity is None:
        _blocking_add(blocked, "relation_endpoint_not_published")
        return None
    relation_type = relation_types.get(relation.relation_type_id)
    if relation_type is None or relation_type.status != SCHEMA_STATUS_ACTIVE:
        _blocking_add(blocked, "relation_type_not_active")
        return None
    constraint = relation_constraints.get(
        (
            relation.relation_type_id,
            source_entity.entity_type_id,
            target_entity.entity_type_id,
        )
    )
    try:
        shape = validate_relation_shape(
            relation_type=_relation_type_rule(relation_type),
            constraint=_relation_constraint_rule(constraint),
            source_entity_type_id=source_entity.entity_type_id,
            target_entity_type_id=target_entity.entity_type_id,
            properties=_normalized_properties(relation.properties),
        )
    except ValueError:
        _blocking_add(blocked, "relation_schema_invalid")
        return None
    if not shape.valid or not shape.schema_boundary_clear:
        _blocking_add(blocked, "relation_schema_invalid")
        return None
    if shape.requires_review and relation.review_status != "approved":
        _blocking_add(blocked, "relation_review_blocked")
        return None
    supports = []
    contradicts = []
    for row in evidence_by_relation.get(relation.id, []):
        if row.status != "active" or not await _current_revision_safe(db, library, row):
            continue
        if row.support_type == SUPPORT_TYPE_SUPPORTS:
            supports.append(row)
        elif row.support_type == SUPPORT_TYPE_CONTRADICTS:
            contradicts.append(row)
    if contradicts:
        _blocking_add(blocked, "relation_contradicted")
        return None
    if not supports:
        _blocking_add(blocked, "relation_evidence_incomplete")
        return None
    if (
        relation.source_type == GRAPH_SOURCE_EXTRACTED
        and (
            relation.confidence is None
            or relation.confidence < config.graph_publication_extracted_relation_min_confidence
        )
    ):
        _blocking_add(blocked, "relation_low_confidence")
        return None
    support_evidence_ids = _support_ids(supports)
    item_hash, snapshot, _properties_hash = _relation_item_hash(
        config=config,
        library_id=library.id,
        relation=relation,
        support_evidence_ids=support_evidence_ids,
    )
    return GraphPublicationItem(
        library_id=library.id,
        ontology_version_id=relation.ontology_version_id,
        item_kind="relation",
        relation_id=relation.id,
        item_hash=item_hash,
        status="planned",
        support_evidence_ids=support_evidence_ids,
        support_counts={
            SUPPORT_TYPE_SUPPORTS: len(supports),
            SUPPORT_TYPE_CONTRADICTS: len(contradicts),
        },
        fact_snapshot=snapshot,
    )


def _manifest_hash(
    *,
    config: Settings,
    policy_snapshot_hash: str,
    library_id: uuid.UUID,
    ontology_version_id: uuid.UUID,
    source_mode: str,
    parent_publication_id: uuid.UUID | None,
    include_drafts: bool,
    items: list[GraphPublicationItem],
    blocked_counts: dict[str, int],
    governance_action_set_hash: str | None = None,
) -> str:
    entity_hashes = sorted(item.item_hash for item in items if item.item_kind == "entity")
    relation_hashes = sorted(item.item_hash for item in items if item.item_kind == "relation")
    payload = {
            "manifest_version": config.graph_publication_manifest_version,
            "policy_version": config.graph_publication_policy_version,
            "policy_snapshot_hash": policy_snapshot_hash,
            "library_id": _uuid_text(library_id),
            "ontology_version_id": _uuid_text(ontology_version_id),
            "source_mode": source_mode,
            "parent_publication_id": _uuid_text(parent_publication_id),
            "include_drafts": include_drafts,
            "sorted_entity_item_hashes": entity_hashes,
            "sorted_relation_item_hashes": relation_hashes,
            "blocked_count_by_reason": dict(sorted(blocked_counts.items())),
        }
    if governance_action_set_hash is not None:
        payload["governance_action_set_hash"] = governance_action_set_hash
    return _sha256_json(payload)


async def build_graph_publication_snapshot(
    db: AsyncSession,
    library: Library,
    ontology: OntologyVersion,
    *,
    include_drafts: bool,
    enforce_item_limit: bool = True,
    projection: GraphPublicationProjection | None = None,
    config: Settings = settings,
) -> GraphPublicationSnapshot:
    if ontology.library_id != library.id or ontology.status != ONTOLOGY_STATUS_ACTIVE:
        raise GraphPublicationPlanError("ontology_not_active", "ontology version is not active")

    entity_types = {
        row.id: row
        for row in await _list_rows(
            db,
            select(EntityType).where(
                EntityType.library_id == library.id,
                EntityType.ontology_version_id == ontology.id,
            ),
        )
    }
    relation_types = {
        row.id: row
        for row in await _list_rows(
            db,
            select(RelationType).where(
                RelationType.library_id == library.id,
                RelationType.ontology_version_id == ontology.id,
            ),
        )
    }
    relation_constraints = {
        (row.relation_type_id, row.source_entity_type_id, row.target_entity_type_id): row
        for row in await _list_rows(
            db,
            select(RelationTypeConstraint).where(
                RelationTypeConstraint.library_id == library.id,
                RelationTypeConstraint.ontology_version_id == ontology.id,
                RelationTypeConstraint.status == SCHEMA_STATUS_ACTIVE,
            ),
        )
    }
    entities = await _list_rows(
        db,
        select(Entity).where(
            Entity.library_id == library.id,
            Entity.ontology_version_id == ontology.id,
        ),
    )
    relations = await _list_rows(
        db,
        select(KnowledgeRelation).where(
            KnowledgeRelation.library_id == library.id,
            KnowledgeRelation.ontology_version_id == ontology.id,
        ),
    )
    if projection is not None:
        entity_by_id = {row.id: row for row in entities}
        relation_by_id = {row.id: row for row in relations}
        if not set(projection.entity_states).issubset(entity_by_id) or not set(
            projection.relation_states
        ).issubset(relation_by_id):
            raise GraphPublicationPlanError(
                "governance_projection_invalid",
                "graph governance projection target is unavailable",
            )
        entities = [
            _project_entity(row, projection.entity_states.get(row.id, {}))
            for row in entities
        ]
        relations = [
            _project_relation(row, projection.relation_states.get(row.id, {}))
            for row in relations
        ]
    mentions = await _list_rows(
        db,
        select(EntityMention).where(
            EntityMention.library_id == library.id,
            EntityMention.status == "active",
        ),
    )
    relation_evidence = await _list_rows(
        db,
        select(RelationEvidence).where(
            RelationEvidence.library_id == library.id,
            RelationEvidence.status == "active",
        ),
    )
    mentions_by_entity: dict[uuid.UUID, list[EntityMention]] = {}
    for mention in mentions:
        mentions_by_entity.setdefault(mention.entity_id, []).append(mention)
    evidence_by_relation: dict[uuid.UUID, list[RelationEvidence]] = {}
    for row in relation_evidence:
        evidence_by_relation.setdefault(row.relation_id, []).append(row)

    blocked: dict[str, int] = {}
    items: list[GraphPublicationItem] = []
    for entity in sorted(entities, key=lambda row: str(row.id)):
        if entity.status == GRAPH_FACT_STATUS_DRAFT and not include_drafts:
            _blocking_add(blocked, "entity_draft_not_included")
            continue
        item = await _eligible_entity_item(
            db,
            library=library,
            entity=entity,
            entity_types=entity_types,
            mentions_by_entity=mentions_by_entity,
            config=config,
            blocked=blocked,
        )
        if item is not None:
            items.append(item)
    published_entities = {
        entity.id: entity
        for entity in entities
        if any(item.entity_id == entity.id for item in items if item.item_kind == "entity")
    }
    for relation in sorted(relations, key=lambda row: str(row.id)):
        if relation.status == GRAPH_FACT_STATUS_DRAFT and not include_drafts:
            _blocking_add(blocked, "relation_draft_not_included")
            continue
        item = await _eligible_relation_item(
            db,
            library=library,
            relation=relation,
            relation_types=relation_types,
            relation_constraints=relation_constraints,
            published_entities=published_entities,
            evidence_by_relation=evidence_by_relation,
            config=config,
            blocked=blocked,
        )
        if item is not None:
            items.append(item)
    if enforce_item_limit and len(items) > config.graph_publication_max_items_per_run:
        raise GraphPublicationPlanError("publication_item_limit_exceeded", "publication item limit exceeded")

    policy_snapshot = build_publication_policy_snapshot(config)
    return GraphPublicationSnapshot(
        items=tuple(items),
        blocked_counts=dict(sorted(blocked.items())),
        policy_snapshot=policy_snapshot,
        policy_snapshot_hash=_sha256_json(policy_snapshot),
        governance_action_set_hash=(
            projection.action_set_hash if projection is not None else None
        ),
    )


async def plan_initial_publication(
    db: AsyncSession,
    library: Library,
    *,
    ontology_version_id: uuid.UUID | None = None,
    include_drafts: bool = False,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    expected_parent_publication_id: uuid.UUID | None = None,
    requested_by_user_id: uuid.UUID | None = None,
    config: Settings = settings,
) -> GraphPublicationPlanResult:
    return await plan_graph_publication(
        db,
        library,
        ontology_version_id=ontology_version_id,
        source_mode=GRAPH_PUBLICATION_SOURCE_INITIAL_SEED,
        include_drafts=include_drafts,
        dry_run=dry_run,
        idempotency_key=idempotency_key,
        expected_parent_publication_id=expected_parent_publication_id,
        requested_by_user_id=requested_by_user_id,
        config=config,
    )


async def plan_graph_publication(
    db: AsyncSession,
    library: Library,
    *,
    ontology_version_id: uuid.UUID | None = None,
    source_mode: str = GRAPH_PUBLICATION_SOURCE_MANUAL_PLAN,
    include_drafts: bool = False,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    expected_parent_publication_id: uuid.UUID | None = None,
    requested_by_user_id: uuid.UUID | None = None,
    projection: GraphPublicationProjection | None = None,
    config: Settings = settings,
) -> GraphPublicationPlanResult:
    if source_mode not in {
        GRAPH_PUBLICATION_SOURCE_INITIAL_SEED,
        GRAPH_PUBLICATION_SOURCE_MANUAL_PLAN,
        "rollback",
    }:
        raise GraphPublicationPlanError("invalid_source_mode", "invalid graph publication source mode")
    if not idempotency_key:
        idempotency_key = f"plan:{uuid.uuid4()}"
    ontology = await _active_ontology(db, library, ontology_version_id)
    if not dry_run:
        await _lock_publication_scope(db, library.id)
        idempotent = await _load_idempotent_publication(
            db,
            library_id=library.id,
            ontology_version_id=ontology.id,
            idempotency_key=idempotency_key,
        )
        if idempotent is not None:
            if projection is not None:
                governance = dict(idempotent.plan_options or {}).get("graph_governance")
                expected = {
                    "action_ids": [str(value) for value in projection.action_ids],
                    "action_set_hash": projection.action_set_hash,
                    "contract_version": "graph-governance-v1",
                }
                if governance != expected:
                    raise GraphPublicationPlanError(
                        "idempotency_conflict", "idempotency key conflicts"
                    )
            return GraphPublicationPlanResult(
                publication=idempotent,
                items=(),
                manifest_hash=idempotent.manifest_hash,
                policy_snapshot_hash=_sha256_json(idempotent.policy_snapshot),
                blocked_counts=dict(idempotent.blocked_counts or {}),
                reused=True,
            )
    parent = await _load_current_publication(
        db,
        library_id=library.id,
        ontology_version_id=ontology.id,
    )
    if (
        expected_parent_publication_id is not None
        and getattr(parent, "id", None) != expected_parent_publication_id
    ):
        raise GraphPublicationPlanError(
            "expected_parent_mismatch",
            "current publication does not match the expected parent",
        )
    snapshot = await build_graph_publication_snapshot(
        db,
        library,
        ontology,
        include_drafts=include_drafts,
        projection=projection,
        config=config,
    )
    return await _persist_graph_publication_snapshot(
        db,
        library,
        ontology,
        parent=parent,
        snapshot=snapshot,
        source_mode=source_mode,
        include_drafts=include_drafts,
        dry_run=dry_run,
        idempotency_key=idempotency_key,
        requested_by_user_id=requested_by_user_id,
        plan_options=(
            {
                "graph_governance": {
                    "action_ids": [str(value) for value in projection.action_ids],
                    "action_set_hash": projection.action_set_hash,
                    "contract_version": "graph-governance-v1",
                }
            }
            if projection is not None
            else None
        ),
        config=config,
    )


def graph_publication_snapshot_manifest_hash(
    *,
    library_id: uuid.UUID,
    ontology_version_id: uuid.UUID,
    source_mode: str,
    parent_publication_id: uuid.UUID,
    include_drafts: bool,
    snapshot: GraphPublicationSnapshot,
    config: Settings = settings,
) -> str:
    return _manifest_hash(
        config=config,
        policy_snapshot_hash=snapshot.policy_snapshot_hash,
        library_id=library_id,
        ontology_version_id=ontology_version_id,
        source_mode=source_mode,
        parent_publication_id=parent_publication_id,
        include_drafts=include_drafts,
        items=list(snapshot.items),
        blocked_counts=snapshot.blocked_counts,
        governance_action_set_hash=snapshot.governance_action_set_hash,
    )


async def _persist_graph_publication_snapshot(
    db: AsyncSession,
    library: Library,
    ontology: OntologyVersion,
    *,
    parent: GraphPublication | None,
    snapshot: GraphPublicationSnapshot,
    source_mode: str,
    include_drafts: bool,
    dry_run: bool,
    idempotency_key: str,
    requested_by_user_id: uuid.UUID | None,
    plan_options: dict[str, Any] | None = None,
    allow_manifest_reuse: bool = True,
    config: Settings,
) -> GraphPublicationPlanResult:
    items = list(snapshot.items)
    blocked = snapshot.blocked_counts
    policy_snapshot = snapshot.policy_snapshot
    policy_snapshot_hash = snapshot.policy_snapshot_hash
    manifest_hash = _manifest_hash(
        config=config,
        policy_snapshot_hash=policy_snapshot_hash,
        library_id=library.id,
        ontology_version_id=ontology.id,
        source_mode=source_mode,
        parent_publication_id=getattr(parent, "id", None),
        include_drafts=include_drafts,
        items=items,
        blocked_counts=blocked,
        governance_action_set_hash=snapshot.governance_action_set_hash,
    )
    if not dry_run and allow_manifest_reuse:
        reusable = await _load_reusable_publication(
            db,
            library_id=library.id,
            ontology_version_id=ontology.id,
            manifest_hash=manifest_hash,
        )
        if reusable is not None:
            return GraphPublicationPlanResult(
                publication=reusable,
                items=(),
                manifest_hash=manifest_hash,
                policy_snapshot_hash=policy_snapshot_hash,
                blocked_counts=dict(reusable.blocked_counts or {}),
                reused=True,
            )
    publication = GraphPublication(
        library_id=library.id,
        ontology_version_id=ontology.id,
        status="planned",
        source_mode=source_mode,
        manifest_version=config.graph_publication_manifest_version,
        policy_version=config.graph_publication_policy_version,
        policy_snapshot=policy_snapshot,
        manifest_hash=manifest_hash,
        idempotency_key=idempotency_key,
        include_drafts=include_drafts,
        plan_options={
            "include_drafts": include_drafts,
            "dry_run": dry_run,
            **dict(plan_options or {}),
        },
        parent_publication_id=getattr(parent, "id", None),
        planned_by_user_id=requested_by_user_id,
        entity_count=sum(1 for item in items if item.item_kind == "entity"),
        relation_count=sum(1 for item in items if item.item_kind == "relation"),
        blocked_counts=dict(sorted(blocked.items())),
        blocked_diagnostics={},
        item_hashes_summary={
            "entity_hashes": sorted(item.item_hash for item in items if item.item_kind == "entity"),
            "relation_hashes": sorted(item.item_hash for item in items if item.item_kind == "relation"),
        },
    )
    if publication.id is None:
        publication.id = uuid.uuid4()
    for item in items:
        item.publication_id = publication.id
    if not dry_run:
        db.add(publication)
        await db.flush()
        for item in items:
            item.publication_id = publication.id
            db.add(item)
        await db.flush()
    return GraphPublicationPlanResult(
        publication=publication,
        items=tuple(items),
        manifest_hash=manifest_hash,
        policy_snapshot_hash=policy_snapshot_hash,
        blocked_counts=dict(sorted(blocked.items())),
        dry_run=dry_run,
    )


async def plan_explicit_graph_publication_snapshot(
    db: AsyncSession,
    library: Library,
    *,
    ontology_version_id: uuid.UUID,
    snapshot: GraphPublicationSnapshot,
    source_mode: str,
    include_drafts: bool,
    idempotency_key: str,
    expected_parent_publication_id: uuid.UUID,
    requested_by_user_id: uuid.UUID | None = None,
    plan_options: dict[str, Any] | None = None,
    config: Settings = settings,
) -> GraphPublicationPlanResult:
    if source_mode != GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE:
        raise GraphPublicationPlanError(
            "invalid_source_mode", "explicit graph snapshot source mode is invalid"
        )
    if not idempotency_key:
        raise GraphPublicationPlanError(
            "idempotency_key_required", "idempotency key is required"
        )
    ontology = await _active_ontology(db, library, ontology_version_id)
    await _lock_publication_scope(db, library.id)
    parent = await _load_current_publication(
        db,
        library_id=library.id,
        ontology_version_id=ontology.id,
    )
    if parent is None or parent.id != expected_parent_publication_id:
        raise GraphPublicationPlanError(
            "expected_parent_mismatch",
            "current publication does not match the expected parent",
        )
    expected_manifest_hash = graph_publication_snapshot_manifest_hash(
        library_id=library.id,
        ontology_version_id=ontology.id,
        source_mode=source_mode,
        parent_publication_id=parent.id,
        include_drafts=include_drafts,
        snapshot=snapshot,
        config=config,
    )
    idempotent = await _load_idempotent_publication(
        db,
        library_id=library.id,
        ontology_version_id=ontology.id,
        idempotency_key=idempotency_key,
    )
    if idempotent is not None:
        if (
            idempotent.source_mode != source_mode
            or idempotent.parent_publication_id != parent.id
            or idempotent.manifest_hash != expected_manifest_hash
        ):
            raise GraphPublicationPlanError(
                "idempotency_conflict", "idempotency key conflicts"
            )
        return GraphPublicationPlanResult(
            publication=idempotent,
            items=(),
            manifest_hash=idempotent.manifest_hash,
            policy_snapshot_hash=_sha256_json(idempotent.policy_snapshot),
            blocked_counts=dict(idempotent.blocked_counts or {}),
            reused=True,
        )
    return await _persist_graph_publication_snapshot(
        db,
        library,
        ontology,
        parent=parent,
        snapshot=snapshot,
        source_mode=source_mode,
        include_drafts=include_drafts,
        dry_run=False,
        idempotency_key=idempotency_key,
        requested_by_user_id=requested_by_user_id,
        plan_options=plan_options,
        allow_manifest_reuse=False,
        config=config,
    )
