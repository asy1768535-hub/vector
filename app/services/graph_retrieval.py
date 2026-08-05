from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Sequence

from pydantic import ValidationError
from sqlalchemy import Integer, String, and_, case, cast, column, func, literal, not_, or_, select, true, values
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import Settings, settings
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.entity import Entity
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.schemas.v06_graph_retrieval import (
    GraphRelationDirection,
    GraphRetrievalAmbiguousCandidate,
    GraphRetrievalCounts,
    GraphRetrievalDirection,
    GraphRetrievalEntityTypeRead,
    GraphRetrievalErrorCode,
    GraphRetrievalEvidenceLocator,
    GraphRetrievalNodeRead,
    GraphRetrievalPublicationRead,
    GraphRetrievalQueryRequest,
    GraphRetrievalQueryResponse,
    GraphRetrievalRelationRead,
    GraphRetrievalRelationTypeRead,
    GraphRetrievalSeed,
    GraphRetrievalSeedMatch,
    GraphRetrievalTruncation,
)
from app.services.graph_normalization import normalize_graph_name_v1


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENT_STATUSES = ("active", "degraded")


class GraphRetrievalServiceError(RuntimeError):
    def __init__(
        self,
        code: GraphRetrievalErrorCode,
        *,
        candidates: Sequence[GraphRetrievalAmbiguousCandidate] = (),
    ) -> None:
        self.code = code
        self.candidates = tuple(candidates)
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HealthyGraphSnapshot:
    publication_id: uuid.UUID
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    manifest_version: str
    manifest_hash: str
    activated_at: datetime
    entity_count: int
    relation_count: int


@dataclass(frozen=True, slots=True)
class ResolvedPublishedEntity:
    input_index: int
    entity_id: uuid.UUID
    item_hash: str
    entity_type_id: uuid.UUID
    entity_type_key: str
    entity_type_label: str
    canonical_name: str
    normalized_name: str
    source_type: str
    confidence: float | None


@dataclass(frozen=True, slots=True)
class ResolvedRelationType:
    relation_type_id: uuid.UUID
    key: str
    label: str
    direction: str


@dataclass(frozen=True, slots=True)
class GraphRetrievalResolution:
    snapshot: HealthyGraphSnapshot
    seeds: tuple[ResolvedPublishedEntity, ...]
    relation_types: tuple[ResolvedRelationType, ...]


@dataclass(frozen=True, slots=True)
class TraversedPublishedEntity:
    entity_id: uuid.UUID
    item_hash: str
    entity_type_id: uuid.UUID
    entity_type_key: str
    entity_type_label: str
    canonical_name: str
    normalized_name: str
    source_type: str
    confidence: float | None
    depth: int


@dataclass(frozen=True, slots=True)
class TraversedPublishedRelation:
    relation_id: uuid.UUID
    item_hash: str
    relation_type_id: uuid.UUID
    relation_type_key: str
    relation_type_label: str
    relation_type_direction: GraphRelationDirection
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    source_type: str
    confidence: float | None
    depth: int


@dataclass(frozen=True, slots=True)
class GraphTraversalTruncation:
    nodes: bool
    relations: bool


@dataclass(frozen=True, slots=True)
class PublishedGraphTraversal:
    nodes: tuple[TraversedPublishedEntity, ...]
    relations: tuple[TraversedPublishedRelation, ...]
    truncated: GraphTraversalTruncation


@dataclass(frozen=True, slots=True)
class GraphRetrievalTraversalResolution:
    resolution: GraphRetrievalResolution
    traversal: PublishedGraphTraversal


@dataclass(frozen=True, slots=True)
class SelectedGraphFact:
    output_index: int
    item_kind: Literal["entity", "relation"]
    fact_id: uuid.UUID
    expected_item_hash: str


@dataclass(frozen=True, slots=True)
class HydratedFactEvidence:
    item_kind: Literal["entity", "relation"]
    fact_id: uuid.UUID
    locators: tuple[GraphRetrievalEvidenceLocator, ...]


@dataclass(frozen=True, slots=True)
class GraphEvidenceHydration:
    facts: tuple[HydratedFactEvidence, ...]
    truncated: bool


def _fail(code: GraphRetrievalErrorCode) -> None:
    raise GraphRetrievalServiceError(code)


def _require_datetime(value: datetime | str | None) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            _fail("graph_publication_invariant_failed")
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("graph_publication_invariant_failed")
    return value


def validate_graph_retrieval_request_limits(
    request: GraphRetrievalQueryRequest,
    config: Settings,
) -> None:
    if (
        len(request.seeds) > config.graph_retrieval_max_seeds
        or request.max_hops > config.graph_retrieval_max_hops
        or request.max_nodes > config.graph_retrieval_max_nodes
        or request.max_relations > config.graph_retrieval_max_relations
        or request.max_nodes < len(request.seeds)
    ):
        _fail("graph_retrieval_limit_exceeded")


def _current_publication_statement(library: Library, ontology_version_id: uuid.UUID):
    return (
        select(
            GraphPublication.id.label("publication_id"),
            GraphPublication.library_id.label("library_id"),
            GraphPublication.ontology_version_id.label("ontology_version_id"),
            GraphPublication.status.label("publication_status"),
            GraphPublication.manifest_version.label("manifest_version"),
            GraphPublication.manifest_hash.label("manifest_hash"),
            GraphPublication.activated_at.label("activated_at"),
            GraphPublication.entity_count.label("entity_count"),
            GraphPublication.relation_count.label("relation_count"),
            OntologyVersion.status.label("ontology_status"),
            OntologyVersion.library_id.label("ontology_library_id"),
        )
        .select_from(GraphPublication)
        .outerjoin(OntologyVersion, OntologyVersion.id == GraphPublication.ontology_version_id)
        .where(
            GraphPublication.library_id == library.id,
            GraphPublication.ontology_version_id == ontology_version_id,
            GraphPublication.status.in_(_CURRENT_STATUSES),
        )
    )


async def _current_publication_rows(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
):
    return (
        await db.execute(_current_publication_statement(library, ontology_version_id))
    ).all()


def _snapshot_from_start_row(row, library: Library, ontology_version_id: uuid.UUID):
    if row.publication_status == "degraded":
        _fail("graph_publication_unavailable")
    if (
        row.publication_status != "active"
        or row.library_id != library.id
        or row.ontology_version_id != ontology_version_id
        or row.manifest_version != "v1"
        or row.ontology_status != "active"
        or row.ontology_library_id != library.id
        or not isinstance(row.manifest_hash, str)
        or _SHA256_RE.fullmatch(row.manifest_hash) is None
        or not isinstance(row.entity_count, int)
        or not isinstance(row.relation_count, int)
        or row.entity_count < 0
        or row.relation_count < 0
    ):
        _fail("graph_publication_invariant_failed")
    return HealthyGraphSnapshot(
        publication_id=row.publication_id,
        library_id=row.library_id,
        ontology_version_id=row.ontology_version_id,
        manifest_version=row.manifest_version,
        manifest_hash=row.manifest_hash,
        activated_at=_require_datetime(row.activated_at),
        entity_count=row.entity_count,
        relation_count=row.relation_count,
    )


def _item_aggregate_statement(snapshot: HealthyGraphSnapshot):
    item = GraphPublicationItem
    valid_shape = or_(
        and_(item.item_kind == "entity", item.entity_id.is_not(None), item.relation_id.is_(None)),
        and_(item.item_kind == "relation", item.entity_id.is_(None), item.relation_id.is_not(None)),
    )
    valid_hash = and_(func.length(item.item_hash) == 64, item.item_hash.op("~")(r"^[0-9a-f]{64}$"))
    return select(
        func.count(item.id).label("total_count"),
        func.count().filter(item.item_kind == "entity").label("entity_count"),
        func.count().filter(item.item_kind == "relation").label("relation_count"),
        func.count().filter(item.status == "active").label("active_count"),
        func.count()
        .filter(
            or_(
                item.library_id != snapshot.library_id,
                item.ontology_version_id != snapshot.ontology_version_id,
            )
        )
        .label("wrong_scope_count"),
        func.count().filter(not_(valid_shape)).label("invalid_shape_count"),
        func.count().filter(not_(valid_hash)).label("invalid_hash_count"),
    ).where(item.publication_id == snapshot.publication_id)


def _entity_aggregate_statement(snapshot: HealthyGraphSnapshot):
    item = GraphPublicationItem
    return (
        select(func.count(func.distinct(item.id)).label("healthy_count"))
        .select_from(item)
        .join(
            Entity,
            and_(
                item.entity_id == Entity.id,
                Entity.library_id == snapshot.library_id,
                Entity.ontology_version_id == snapshot.ontology_version_id,
                Entity.status == "active",
            ),
        )
        .join(
            EntityType,
            and_(
                Entity.entity_type_id == EntityType.id,
                EntityType.library_id == snapshot.library_id,
                EntityType.ontology_version_id == snapshot.ontology_version_id,
                EntityType.status == "active",
            ),
        )
        .where(
            item.publication_id == snapshot.publication_id,
            item.library_id == snapshot.library_id,
            item.ontology_version_id == snapshot.ontology_version_id,
            item.item_kind == "entity",
            item.status == "active",
        )
    )


def _relation_aggregate_statement(snapshot: HealthyGraphSnapshot):
    item = GraphPublicationItem
    source_item = aliased(GraphPublicationItem, name="source_item")
    target_item = aliased(GraphPublicationItem, name="target_item")
    endpoint_item_filters = (
        (source_item, KnowledgeRelation.source_entity_id),
        (target_item, KnowledgeRelation.target_entity_id),
    )
    statement = (
        select(func.count(func.distinct(item.id)).label("healthy_count"))
        .select_from(item)
        .join(
            KnowledgeRelation,
            and_(
                item.relation_id == KnowledgeRelation.id,
                KnowledgeRelation.library_id == snapshot.library_id,
                KnowledgeRelation.ontology_version_id == snapshot.ontology_version_id,
                KnowledgeRelation.status == "active",
                KnowledgeRelation.review_status.in_(("not_required", "approved")),
            ),
        )
        .join(
            RelationType,
            and_(
                KnowledgeRelation.relation_type_id == RelationType.id,
                RelationType.library_id == snapshot.library_id,
                RelationType.ontology_version_id == snapshot.ontology_version_id,
                RelationType.status == "active",
            ),
        )
    )
    for endpoint_item, endpoint_id in endpoint_item_filters:
        statement = statement.join(
            endpoint_item,
            and_(
                endpoint_item.publication_id == snapshot.publication_id,
                endpoint_item.library_id == snapshot.library_id,
                endpoint_item.ontology_version_id == snapshot.ontology_version_id,
                endpoint_item.item_kind == "entity",
                endpoint_item.status == "active",
                endpoint_item.entity_id == endpoint_id,
            ),
        )
    return statement.where(
        item.publication_id == snapshot.publication_id,
        item.library_id == snapshot.library_id,
        item.ontology_version_id == snapshot.ontology_version_id,
        item.item_kind == "relation",
        item.status == "active",
    )


async def _validate_membership_invariants(db: AsyncSession, snapshot: HealthyGraphSnapshot) -> None:
    item_rows = (await db.execute(_item_aggregate_statement(snapshot))).all()
    if len(item_rows) != 1:
        _fail("graph_publication_invariant_failed")
    item = item_rows[0]
    if (
        item.total_count != snapshot.entity_count + snapshot.relation_count
        or item.entity_count != snapshot.entity_count
        or item.relation_count != snapshot.relation_count
        or item.active_count != item.total_count
        or item.wrong_scope_count != 0
        or item.invalid_shape_count != 0
        or item.invalid_hash_count != 0
    ):
        _fail("graph_publication_invariant_failed")

    entity_rows = (await db.execute(_entity_aggregate_statement(snapshot))).all()
    if len(entity_rows) != 1 or entity_rows[0].healthy_count != snapshot.entity_count:
        _fail("graph_publication_invariant_failed")

    relation_rows = (await db.execute(_relation_aggregate_statement(snapshot))).all()
    if len(relation_rows) != 1 or relation_rows[0].healthy_count != snapshot.relation_count:
        _fail("graph_publication_invariant_failed")


async def load_healthy_graph_snapshot(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
    *,
    expected_publication_id: uuid.UUID | None = None,
) -> HealthyGraphSnapshot:
    rows = await _current_publication_rows(db, library, ontology_version_id)
    if not rows:
        _fail("graph_publication_unavailable")
    if len(rows) != 1:
        _fail("graph_publication_invariant_failed")
    snapshot = _snapshot_from_start_row(rows[0], library, ontology_version_id)
    if expected_publication_id is not None and snapshot.publication_id != expected_publication_id:
        _fail("publication_changed")
    await _validate_membership_invariants(db, snapshot)
    return snapshot


def _published_entity_projection(
    seed_values,
    snapshot: HealthyGraphSnapshot,
    entity_join_condition,
):
    return (
        select(
            seed_values.c.input_index.label("input_index"),
            Entity.id.label("entity_id"),
            GraphPublicationItem.item_hash.label("item_hash"),
            EntityType.id.label("entity_type_id"),
            EntityType.key.label("entity_type_key"),
            EntityType.label.label("entity_type_label"),
            Entity.canonical_name.label("canonical_name"),
            Entity.normalized_name.label("normalized_name"),
            Entity.source_type.label("source_type"),
            Entity.confidence.label("confidence"),
        )
        .select_from(seed_values)
        .join(Entity, entity_join_condition)
        .join(
            GraphPublicationItem,
            and_(
                GraphPublicationItem.publication_id == snapshot.publication_id,
                GraphPublicationItem.library_id == snapshot.library_id,
                GraphPublicationItem.ontology_version_id == snapshot.ontology_version_id,
                GraphPublicationItem.item_kind == "entity",
                GraphPublicationItem.status == "active",
                GraphPublicationItem.entity_id == Entity.id,
            ),
        )
        .join(
            EntityType,
            and_(
                Entity.entity_type_id == EntityType.id,
                EntityType.library_id == snapshot.library_id,
                EntityType.ontology_version_id == snapshot.ontology_version_id,
                EntityType.status == "active",
            ),
        )
        .where(
            Entity.library_id == snapshot.library_id,
            Entity.ontology_version_id == snapshot.ontology_version_id,
            Entity.status == "active",
        )
    )


def _id_seed_statement(entries, snapshot: HealthyGraphSnapshot):
    seed_values = (
        values(
            column("input_index", Integer),
            column("entity_id", PgUUID(as_uuid=True)),
            name="graph_seed_ids",
        )
        .data(entries)
        .alias("graph_seed_ids")
    )
    return (
        _published_entity_projection(
            seed_values,
            snapshot,
            Entity.id == seed_values.c.entity_id,
        )
        .order_by(seed_values.c.input_index, EntityType.key, Entity.id)
    )


def _name_seed_statement(entries, snapshot: HealthyGraphSnapshot):
    seed_values = (
        values(
            column("input_index", Integer),
            column("normalized_name", String(512)),
            column("entity_type_key", String(128)),
            name="graph_seed_names",
        )
        .data(entries)
        .alias("graph_seed_names")
    )
    return (
        _published_entity_projection(
            seed_values,
            snapshot,
            Entity.normalized_name == seed_values.c.normalized_name,
        )
        .where(
            or_(
                seed_values.c.entity_type_key.is_(None),
                EntityType.key == seed_values.c.entity_type_key,
            )
        )
        .order_by(seed_values.c.input_index, EntityType.key, Entity.id)
    )


def _resolved_entity(row) -> ResolvedPublishedEntity:
    return ResolvedPublishedEntity(
        input_index=row.input_index,
        entity_id=row.entity_id,
        item_hash=row.item_hash,
        entity_type_id=row.entity_type_id,
        entity_type_key=row.entity_type_key,
        entity_type_label=row.entity_type_label,
        canonical_name=row.canonical_name,
        normalized_name=row.normalized_name,
        source_type=row.source_type,
        confidence=row.confidence,
    )


async def resolve_published_seeds(
    db: AsyncSession,
    library: Library,
    snapshot: HealthyGraphSnapshot,
    seeds: Sequence[GraphRetrievalSeed],
) -> tuple[ResolvedPublishedEntity, ...]:
    if snapshot.library_id != library.id:
        _fail("graph_publication_invariant_failed")
    id_entries = []
    name_entries = []
    for input_index, seed in enumerate(seeds):
        if seed.entity_id is not None:
            id_entries.append((input_index, seed.entity_id))
        else:
            name_entries.append(
                (
                    input_index,
                    normalize_graph_name_v1(seed.canonical_name or ""),
                    seed.entity_type_key,
                )
            )

    raw_rows = []
    if id_entries:
        raw_rows.extend((await db.execute(_id_seed_statement(id_entries, snapshot))).all())
    if name_entries:
        raw_rows.extend((await db.execute(_name_seed_statement(name_entries, snapshot))).all())

    matches: dict[int, dict[uuid.UUID, ResolvedPublishedEntity]] = {
        index: {} for index in range(len(seeds))
    }
    for row in raw_rows:
        resolved = _resolved_entity(row)
        if resolved.input_index in matches:
            matches[resolved.input_index][resolved.entity_id] = resolved

    output = []
    for input_index in range(len(seeds)):
        rows = sorted(
            matches[input_index].values(),
            key=lambda row: (row.entity_type_key, str(row.entity_id)),
        )
        if not rows:
            _fail("seed_not_found")
        if len(rows) > 1:
            candidates = tuple(
                GraphRetrievalAmbiguousCandidate(
                    entity_id=row.entity_id,
                    entity_type_key=row.entity_type_key,
                )
                for row in rows[:10]
            )
            raise GraphRetrievalServiceError("seed_ambiguous", candidates=candidates)
        output.append(rows[0])
    return tuple(output)


async def resolve_relation_type_filters(
    db: AsyncSession,
    library: Library,
    snapshot: HealthyGraphSnapshot,
    relation_type_keys: Sequence[str],
) -> tuple[ResolvedRelationType, ...]:
    if snapshot.library_id != library.id:
        _fail("graph_publication_invariant_failed")
    requested = tuple(sorted(set(relation_type_keys)))
    if not requested:
        return ()
    rows = (
        await db.execute(
            select(
                RelationType.id.label("relation_type_id"),
                RelationType.key.label("key"),
                RelationType.label.label("label"),
                RelationType.direction.label("direction"),
            )
            .where(
                RelationType.library_id == snapshot.library_id,
                RelationType.ontology_version_id == snapshot.ontology_version_id,
                RelationType.status == "active",
                RelationType.key.in_(requested),
            )
            .order_by(RelationType.key, RelationType.id)
        )
    ).all()
    if {row.key for row in rows} != set(requested):
        _fail("relation_type_not_found")
    return tuple(
        ResolvedRelationType(
            relation_type_id=row.relation_type_id,
            key=row.key,
            label=row.label,
            direction=row.direction,
        )
        for row in rows
    )


async def assert_graph_snapshot_still_current(
    db: AsyncSession,
    library: Library,
    snapshot: HealthyGraphSnapshot,
) -> None:
    rows = await _current_publication_rows(db, library, snapshot.ontology_version_id)
    if not rows:
        _fail("publication_changed")
    if len(rows) != 1:
        _fail("graph_publication_invariant_failed")
    row = rows[0]
    if row.publication_id != snapshot.publication_id:
        _fail("publication_changed")
    if row.publication_status == "degraded":
        _fail("graph_publication_unavailable")
    if (
        row.publication_status != "active"
        or row.library_id != snapshot.library_id
        or row.ontology_version_id != snapshot.ontology_version_id
        or row.manifest_version != snapshot.manifest_version
        or row.manifest_hash != snapshot.manifest_hash
        or row.ontology_status != "active"
        or row.ontology_library_id != snapshot.library_id
        or _require_datetime(row.activated_at) != snapshot.activated_at
        or row.entity_count != snapshot.entity_count
        or row.relation_count != snapshot.relation_count
    ):
        _fail("graph_publication_invariant_failed")


async def resolve_graph_retrieval_query(
    db: AsyncSession,
    library: Library,
    request: GraphRetrievalQueryRequest,
    *,
    config: Settings = settings,
) -> GraphRetrievalResolution:
    validate_graph_retrieval_request_limits(request, config)
    snapshot = await load_healthy_graph_snapshot(
        db,
        library,
        request.ontology_version_id,
        expected_publication_id=request.expected_publication_id,
    )
    seeds = await resolve_published_seeds(db, library, snapshot, request.seeds)
    relation_types = await resolve_relation_type_filters(
        db,
        library,
        snapshot,
        request.relation_type_keys,
    )
    await assert_graph_snapshot_still_current(db, library, snapshot)
    return GraphRetrievalResolution(
        snapshot=snapshot,
        seeds=seeds,
        relation_types=relation_types,
    )


def _traversed_seed(seed: ResolvedPublishedEntity) -> TraversedPublishedEntity:
    return TraversedPublishedEntity(
        entity_id=seed.entity_id,
        item_hash=seed.item_hash,
        entity_type_id=seed.entity_type_id,
        entity_type_key=seed.entity_type_key,
        entity_type_label=seed.entity_type_label,
        canonical_name=seed.canonical_name,
        normalized_name=seed.normalized_name,
        source_type=seed.source_type,
        confidence=seed.confidence,
        depth=0,
    )


def _traversed_endpoint(row, prefix: str, depth: int) -> TraversedPublishedEntity:
    return TraversedPublishedEntity(
        entity_id=getattr(row, f"{prefix}_entity_id"),
        item_hash=getattr(row, f"{prefix}_item_hash"),
        entity_type_id=getattr(row, f"{prefix}_entity_type_id"),
        entity_type_key=getattr(row, f"{prefix}_entity_type_key"),
        entity_type_label=getattr(row, f"{prefix}_entity_type_label"),
        canonical_name=getattr(row, f"{prefix}_canonical_name"),
        normalized_name=getattr(row, f"{prefix}_normalized_name"),
        source_type=getattr(row, f"{prefix}_source_type"),
        confidence=getattr(row, f"{prefix}_confidence"),
        depth=depth,
    )


def _traversed_relation(row, depth: int) -> TraversedPublishedRelation:
    return TraversedPublishedRelation(
        relation_id=row.relation_id,
        item_hash=row.relation_item_hash,
        relation_type_id=row.relation_type_id,
        relation_type_key=row.relation_type_key,
        relation_type_label=row.relation_type_label,
        relation_type_direction=row.relation_type_direction,
        source_entity_id=row.source_entity_id,
        target_entity_id=row.target_entity_id,
        source_type=row.relation_source_type,
        confidence=row.relation_confidence,
        depth=depth,
    )


def _traversal_row_key(row) -> tuple[str, str, str, str]:
    return (
        row.relation_type_key,
        str(row.source_entity_id),
        str(row.target_entity_id),
        str(row.relation_id),
    )


def _traversal_direction_predicate(
    direction: GraphRetrievalDirection,
    frontier_ids: tuple[uuid.UUID, ...],
):
    source_matches = KnowledgeRelation.source_entity_id.in_(frontier_ids)
    target_matches = KnowledgeRelation.target_entity_id.in_(frontier_ids)
    either_matches = or_(source_matches, target_matches)
    undirected_matches = and_(RelationType.direction == "undirected", either_matches)
    if direction == "outbound":
        directed_matches = and_(RelationType.direction == "directed", source_matches)
    elif direction == "inbound":
        directed_matches = and_(RelationType.direction == "directed", target_matches)
    else:
        directed_matches = and_(RelationType.direction == "directed", either_matches)
    return or_(directed_matches, undirected_matches)


def _traversal_statement(
    snapshot: HealthyGraphSnapshot,
    frontier_ids: tuple[uuid.UUID, ...],
    seen_relation_ids: tuple[uuid.UUID, ...],
    relation_types: Sequence[ResolvedRelationType],
    direction: GraphRetrievalDirection,
    relation_slots: int,
):
    relation_item = aliased(GraphPublicationItem, name="traversal_relation_item")
    source_item = aliased(GraphPublicationItem, name="traversal_source_item")
    target_item = aliased(GraphPublicationItem, name="traversal_target_item")
    source_entity = aliased(Entity, name="traversal_source_entity")
    target_entity = aliased(Entity, name="traversal_target_entity")
    source_type = aliased(EntityType, name="traversal_source_type")
    target_type = aliased(EntityType, name="traversal_target_type")

    relation_candidates = (
        select(
            KnowledgeRelation.id.label("relation_id"),
            relation_item.item_hash.label("relation_item_hash"),
            RelationType.id.label("relation_type_id"),
            RelationType.key.label("relation_type_key"),
            RelationType.label.label("relation_type_label"),
            RelationType.direction.label("relation_type_direction"),
            KnowledgeRelation.source_entity_id.label("source_entity_id"),
            KnowledgeRelation.target_entity_id.label("target_entity_id"),
            KnowledgeRelation.source_type.label("relation_source_type"),
            KnowledgeRelation.confidence.label("relation_confidence"),
        )
        .select_from(relation_item)
        .join(
            KnowledgeRelation,
            and_(
                relation_item.relation_id == KnowledgeRelation.id,
                KnowledgeRelation.library_id == snapshot.library_id,
                KnowledgeRelation.ontology_version_id == snapshot.ontology_version_id,
                KnowledgeRelation.status == "active",
                KnowledgeRelation.review_status.in_(("not_required", "approved")),
            ),
        )
        .join(
            RelationType,
            and_(
                KnowledgeRelation.relation_type_id == RelationType.id,
                RelationType.library_id == snapshot.library_id,
                RelationType.ontology_version_id == snapshot.ontology_version_id,
                RelationType.status == "active",
            ),
        )
        .where(
            relation_item.publication_id == snapshot.publication_id,
            relation_item.library_id == snapshot.library_id,
            relation_item.ontology_version_id == snapshot.ontology_version_id,
            relation_item.item_kind == "relation",
            relation_item.status == "active",
            _traversal_direction_predicate(direction, frontier_ids),
        )
    )
    if seen_relation_ids:
        relation_candidates = relation_candidates.where(
            KnowledgeRelation.id.notin_(seen_relation_ids)
        )
    if relation_types:
        relation_candidates = relation_candidates.where(
            RelationType.id.in_(tuple(row.relation_type_id for row in relation_types))
        )
    candidate = relation_candidates.cte("traversal_relation_candidates").prefix_with(
        "MATERIALIZED"
    )

    return (
        select(
            candidate.c.relation_id,
            candidate.c.relation_item_hash,
            candidate.c.relation_type_id,
            candidate.c.relation_type_key,
            candidate.c.relation_type_label,
            candidate.c.relation_type_direction,
            candidate.c.source_entity_id,
            candidate.c.target_entity_id,
            candidate.c.relation_source_type,
            candidate.c.relation_confidence,
            source_item.item_hash.label("source_item_hash"),
            source_type.id.label("source_entity_type_id"),
            source_type.key.label("source_entity_type_key"),
            source_type.label.label("source_entity_type_label"),
            source_entity.canonical_name.label("source_canonical_name"),
            source_entity.normalized_name.label("source_normalized_name"),
            source_entity.source_type.label("source_source_type"),
            source_entity.confidence.label("source_confidence"),
            target_item.item_hash.label("target_item_hash"),
            target_type.id.label("target_entity_type_id"),
            target_type.key.label("target_entity_type_key"),
            target_type.label.label("target_entity_type_label"),
            target_entity.canonical_name.label("target_canonical_name"),
            target_entity.normalized_name.label("target_normalized_name"),
            target_entity.source_type.label("target_source_type"),
            target_entity.confidence.label("target_confidence"),
        )
        .select_from(candidate)
        .join(
            source_item,
            and_(
                source_item.publication_id == snapshot.publication_id,
                source_item.library_id == snapshot.library_id,
                source_item.ontology_version_id == snapshot.ontology_version_id,
                source_item.item_kind == "entity",
                source_item.status == "active",
                source_item.entity_id == candidate.c.source_entity_id,
            ),
        )
        .join(
            source_entity,
            and_(
                source_entity.id == candidate.c.source_entity_id,
                source_entity.library_id == snapshot.library_id,
                source_entity.ontology_version_id == snapshot.ontology_version_id,
                source_entity.status == "active",
            ),
        )
        .join(
            source_type,
            and_(
                source_entity.entity_type_id == source_type.id,
                source_type.library_id == snapshot.library_id,
                source_type.ontology_version_id == snapshot.ontology_version_id,
                source_type.status == "active",
            ),
        )
        .join(
            target_item,
            and_(
                target_item.publication_id == snapshot.publication_id,
                target_item.library_id == snapshot.library_id,
                target_item.ontology_version_id == snapshot.ontology_version_id,
                target_item.item_kind == "entity",
                target_item.status == "active",
                target_item.entity_id == candidate.c.target_entity_id,
            ),
        )
        .join(
            target_entity,
            and_(
                target_entity.id == candidate.c.target_entity_id,
                target_entity.library_id == snapshot.library_id,
                target_entity.ontology_version_id == snapshot.ontology_version_id,
                target_entity.status == "active",
            ),
        )
        .join(
            target_type,
            and_(
                target_entity.entity_type_id == target_type.id,
                target_type.library_id == snapshot.library_id,
                target_type.ontology_version_id == snapshot.ontology_version_id,
                target_type.status == "active",
            ),
        )
        .order_by(
            candidate.c.relation_type_key,
            candidate.c.source_entity_id,
            candidate.c.target_entity_id,
            candidate.c.relation_id,
        )
        .limit(max(1, relation_slots + 1))
    )


async def traverse_published_graph(
    db: AsyncSession,
    library: Library,
    snapshot: HealthyGraphSnapshot,
    seeds: Sequence[ResolvedPublishedEntity],
    relation_types: Sequence[ResolvedRelationType],
    *,
    direction: GraphRetrievalDirection,
    max_hops: int,
    max_nodes: int,
    max_relations: int,
) -> PublishedGraphTraversal:
    if (
        snapshot.library_id != library.id
        or not seeds
        or direction not in {"outbound", "inbound", "both"}
        or max_hops < 0
        or max_hops > 2
        or max_nodes < 1
        or max_relations < 1
    ):
        _fail("graph_publication_invariant_failed")

    unique_seeds: dict[uuid.UUID, ResolvedPublishedEntity] = {}
    for seed in seeds:
        unique_seeds.setdefault(seed.entity_id, seed)
    if len(unique_seeds) > max_nodes:
        _fail("graph_retrieval_limit_exceeded")

    seed_ids = tuple(sorted(unique_seeds, key=str))
    nodes_by_id = {
        entity_id: _traversed_seed(unique_seeds[entity_id]) for entity_id in seed_ids
    }
    node_order = list(seed_ids)
    frontier_ids = seed_ids
    seen_relation_ids: set[uuid.UUID] = set()
    relations: list[TraversedPublishedRelation] = []
    nodes_truncated = False
    relations_truncated = False

    for depth in range(1, max_hops + 1):
        if not frontier_ids:
            break
        relation_slots = max_relations - len(relations)
        rows = (
            await db.execute(
                _traversal_statement(
                    snapshot,
                    frontier_ids,
                    tuple(sorted(seen_relation_ids, key=str)),
                    relation_types,
                    direction,
                    relation_slots,
                )
            )
        ).all()
        next_frontier: list[uuid.UUID] = []
        stop = False
        for row in sorted(rows, key=_traversal_row_key):
            if row.relation_id in seen_relation_ids:
                continue
            source = _traversed_endpoint(row, "source", depth)
            target = _traversed_endpoint(row, "target", depth)
            new_nodes = tuple(
                node
                for node in (source, target)
                if node.entity_id not in nodes_by_id
            )
            relation_overflow = len(relations) >= max_relations
            node_overflow = len(nodes_by_id) + len(new_nodes) > max_nodes
            if relation_overflow or node_overflow:
                relations_truncated = relations_truncated or relation_overflow
                nodes_truncated = nodes_truncated or node_overflow
                stop = True
                break

            relation = _traversed_relation(row, depth)
            seen_relation_ids.add(relation.relation_id)
            relations.append(relation)
            for node in new_nodes:
                if node.entity_id in nodes_by_id:
                    continue
                nodes_by_id[node.entity_id] = node
                node_order.append(node.entity_id)
                next_frontier.append(node.entity_id)
        if stop:
            break
        frontier_ids = tuple(sorted(set(next_frontier), key=str))

    return PublishedGraphTraversal(
        nodes=tuple(nodes_by_id[entity_id] for entity_id in node_order),
        relations=tuple(relations),
        truncated=GraphTraversalTruncation(
            nodes=nodes_truncated,
            relations=relations_truncated,
        ),
    )


async def _resolve_and_traverse_graph_retrieval_query_unfenced(
    db: AsyncSession,
    library: Library,
    request: GraphRetrievalQueryRequest,
    *,
    config: Settings = settings,
) -> GraphRetrievalTraversalResolution:
    validate_graph_retrieval_request_limits(request, config)
    snapshot = await load_healthy_graph_snapshot(
        db,
        library,
        request.ontology_version_id,
        expected_publication_id=request.expected_publication_id,
    )
    seeds = await resolve_published_seeds(db, library, snapshot, request.seeds)
    relation_types = await resolve_relation_type_filters(
        db,
        library,
        snapshot,
        request.relation_type_keys,
    )
    resolution = GraphRetrievalResolution(
        snapshot=snapshot,
        seeds=seeds,
        relation_types=relation_types,
    )
    traversal = await traverse_published_graph(
        db,
        library,
        snapshot,
        seeds,
        relation_types,
        direction=request.direction,
        max_hops=request.max_hops,
        max_nodes=request.max_nodes,
        max_relations=request.max_relations,
    )
    return GraphRetrievalTraversalResolution(
        resolution=resolution,
        traversal=traversal,
    )


async def resolve_and_traverse_graph_retrieval_query(
    db: AsyncSession,
    library: Library,
    request: GraphRetrievalQueryRequest,
    *,
    config: Settings = settings,
) -> GraphRetrievalTraversalResolution:
    result = await _resolve_and_traverse_graph_retrieval_query_unfenced(
        db,
        library,
        request,
        config=config,
    )
    await assert_graph_snapshot_still_current(db, library, result.resolution.snapshot)
    return result


def _selected_graph_facts(
    traversal: PublishedGraphTraversal,
) -> tuple[SelectedGraphFact, ...]:
    facts: list[SelectedGraphFact] = []
    seen: set[tuple[str, uuid.UUID]] = set()
    for node in traversal.nodes:
        key = ("entity", node.entity_id)
        if key in seen:
            _fail("graph_publication_invariant_failed")
        seen.add(key)
        facts.append(
            SelectedGraphFact(
                output_index=len(facts),
                item_kind="entity",
                fact_id=node.entity_id,
                expected_item_hash=node.item_hash,
            )
        )
    for relation in traversal.relations:
        key = ("relation", relation.relation_id)
        if key in seen:
            _fail("graph_publication_invariant_failed")
        seen.add(key)
        facts.append(
            SelectedGraphFact(
                output_index=len(facts),
                item_kind="relation",
                fact_id=relation.relation_id,
                expected_item_hash=relation.item_hash,
            )
        )
    return tuple(facts)


def _fact_support_statement(
    snapshot: HealthyGraphSnapshot,
    facts: Sequence[SelectedGraphFact],
    *,
    max_evidence_per_fact: int,
):
    selected = (
        values(
            column("output_index", Integer),
            column("item_kind", String(16)),
            column("fact_id", PgUUID(as_uuid=True)),
            column("expected_item_hash", String(64)),
            name="selected_graph_facts",
        )
        .data(
            tuple(
                (
                    fact.output_index,
                    fact.item_kind,
                    fact.fact_id,
                    fact.expected_item_hash,
                )
                for fact in facts
            )
        )
        .alias("selected_graph_facts")
    )
    item = aliased(GraphPublicationItem, name="selected_fact_item")
    shape_valid = func.jsonb_typeof(item.support_evidence_ids) == "array"
    empty_json = cast(literal("[]"), JSONB)
    safe_supports = case(
        (shape_valid, item.support_evidence_ids),
        else_=empty_json,
    )
    safe_count = case(
        (shape_valid, func.jsonb_array_length(item.support_evidence_ids)),
        else_=0,
    )
    support_index = (
        select(
            func.generate_series(
                0,
                func.least(safe_count, max_evidence_per_fact) - 1,
            ).label("support_index")
        )
        .correlate(item)
        .lateral("selected_support_index")
    )
    exact_target = or_(
        and_(
            selected.c.item_kind == "entity",
            item.entity_id == selected.c.fact_id,
            item.relation_id.is_(None),
        ),
        and_(
            selected.c.item_kind == "relation",
            item.relation_id == selected.c.fact_id,
            item.entity_id.is_(None),
        ),
    )
    return (
        select(
            selected.c.output_index,
            selected.c.item_kind,
            selected.c.fact_id,
            shape_valid.label("support_shape_valid"),
            safe_count.label("support_count"),
            (support_index.c.support_index + 1).label("support_position"),
            safe_supports.op("->>")(support_index.c.support_index).label(
                "selected_evidence_id_text"
            ),
        )
        .select_from(selected)
        .join(
            item,
            and_(
                item.publication_id == snapshot.publication_id,
                item.library_id == snapshot.library_id,
                item.ontology_version_id == snapshot.ontology_version_id,
                item.status == "active",
                item.item_kind == selected.c.item_kind,
                item.item_hash == selected.c.expected_item_hash,
                exact_target,
            ),
        )
        .outerjoin(support_index, true())
        .order_by(selected.c.output_index, support_index.c.support_index)
    )


def _evidence_locator_statement(
    library: Library,
    evidence_ids: tuple[uuid.UUID, ...],
):
    block = aliased(DocumentBlock, name="selected_evidence_block")
    return (
        select(
            EvidenceUnit.id.label("evidence_id"),
            EvidenceUnit.document_id.label("document_id"),
            EvidenceUnit.document_revision_id.label("document_revision_id"),
            EvidenceUnit.document_block_id.label("document_block_id"),
            block.id.label("joined_document_block_id"),
            EvidenceUnit.evidence_kind.label("evidence_kind"),
            EvidenceUnit.page_start.label("page_start"),
            EvidenceUnit.page_end.label("page_end"),
            EvidenceUnit.source_start.label("source_start"),
            EvidenceUnit.source_end.label("source_end"),
        )
        .select_from(EvidenceUnit)
        .join(
            Document,
            and_(
                EvidenceUnit.document_id == Document.id,
                Document.library_id == library.id,
                Document.deleted_at.is_(None),
                Document.status == "ready",
                Document.current_revision_id == EvidenceUnit.document_revision_id,
            ),
        )
        .join(
            DocumentRevision,
            and_(
                EvidenceUnit.document_revision_id == DocumentRevision.id,
                DocumentRevision.library_id == library.id,
                DocumentRevision.document_id == EvidenceUnit.document_id,
                DocumentRevision.status == "ready",
            ),
        )
        .outerjoin(
            block,
            and_(
                EvidenceUnit.document_block_id == block.id,
                block.library_id == library.id,
                block.document_id == EvidenceUnit.document_id,
                block.document_revision_id == EvidenceUnit.document_revision_id,
            ),
        )
        .where(
            EvidenceUnit.id.in_(evidence_ids),
            EvidenceUnit.library_id == library.id,
            EvidenceUnit.status == "active",
        )
        .order_by(EvidenceUnit.id)
    )


def _parse_fact_support_rows(
    rows,
    facts: Sequence[SelectedGraphFact],
    max_evidence_per_fact: int,
) -> tuple[tuple[tuple[uuid.UUID, ...], ...], bool]:
    rows_by_index: dict[int, list] = {fact.output_index: [] for fact in facts}
    for row in rows:
        if row.output_index not in rows_by_index:
            _fail("graph_publication_invariant_failed")
        rows_by_index[row.output_index].append(row)

    selected_ids: list[tuple[uuid.UUID, ...]] = []
    truncated = False
    for fact in facts:
        fact_rows = rows_by_index[fact.output_index]
        if not fact_rows:
            _fail("graph_publication_invariant_failed")
        first = fact_rows[0]
        if (
            first.item_kind != fact.item_kind
            or first.fact_id != fact.fact_id
            or first.support_shape_valid is not True
            or not isinstance(first.support_count, int)
            or first.support_count < 0
        ):
            _fail("graph_publication_invariant_failed")
        if any(
            row.item_kind != fact.item_kind
            or row.fact_id != fact.fact_id
            or row.support_shape_valid is not True
            or row.support_count != first.support_count
            for row in fact_rows
        ):
            _fail("graph_publication_invariant_failed")

        selected_count = min(first.support_count, max_evidence_per_fact)
        truncated = truncated or first.support_count > max_evidence_per_fact
        if selected_count == 0:
            if (
                len(fact_rows) != 1
                or first.support_position is not None
                or first.selected_evidence_id_text is not None
            ):
                _fail("graph_publication_invariant_failed")
            selected_ids.append(())
            continue
        if len(fact_rows) != selected_count:
            _fail("graph_publication_invariant_failed")

        parsed: list[uuid.UUID] = []
        previous_text: str | None = None
        for expected_position, row in enumerate(fact_rows, start=1):
            text_value = row.selected_evidence_id_text
            if row.support_position != expected_position or not isinstance(text_value, str):
                _fail("graph_publication_invariant_failed")
            try:
                evidence_id = uuid.UUID(text_value)
            except ValueError:
                _fail("graph_publication_invariant_failed")
            if str(evidence_id) != text_value or (
                previous_text is not None and text_value < previous_text
            ):
                _fail("graph_publication_invariant_failed")
            previous_text = text_value
            if not parsed or parsed[-1] != evidence_id:
                parsed.append(evidence_id)
        selected_ids.append(tuple(parsed))
    return tuple(selected_ids), truncated


def _locator_from_row(row) -> GraphRetrievalEvidenceLocator:
    if row.document_block_id is not None and row.joined_document_block_id != row.document_block_id:
        _fail("graph_publication_invariant_failed")
    try:
        return GraphRetrievalEvidenceLocator(
            evidence_id=row.evidence_id,
            document_id=row.document_id,
            document_revision_id=row.document_revision_id,
            document_block_id=row.document_block_id,
            evidence_kind=row.evidence_kind,
            page_start=row.page_start,
            page_end=row.page_end,
            source_start=row.source_start,
            source_end=row.source_end,
        )
    except ValidationError:
        _fail("graph_publication_invariant_failed")


async def hydrate_graph_evidence_locators(
    db: AsyncSession,
    library: Library,
    result: GraphRetrievalTraversalResolution,
    *,
    max_evidence_per_fact: int,
) -> GraphEvidenceHydration:
    snapshot = result.resolution.snapshot
    if (
        snapshot.library_id != library.id
        or max_evidence_per_fact < 1
        or max_evidence_per_fact > 20
    ):
        _fail("graph_publication_invariant_failed")
    facts = _selected_graph_facts(result.traversal)
    if not facts:
        _fail("graph_publication_invariant_failed")
    support_rows = (
        await db.execute(
            _fact_support_statement(
                snapshot,
                facts,
                max_evidence_per_fact=max_evidence_per_fact,
            )
        )
    ).all()
    selected_by_fact, truncated = _parse_fact_support_rows(
        support_rows,
        facts,
        max_evidence_per_fact,
    )
    unique_evidence_ids = tuple(
        sorted(
            {evidence_id for values in selected_by_fact for evidence_id in values},
            key=str,
        )
    )
    locator_by_id: dict[uuid.UUID, GraphRetrievalEvidenceLocator] = {}
    if unique_evidence_ids:
        evidence_rows = (
            await db.execute(_evidence_locator_statement(library, unique_evidence_ids))
        ).all()
        for row in evidence_rows:
            if row.evidence_id in locator_by_id:
                _fail("graph_publication_invariant_failed")
            locator_by_id[row.evidence_id] = _locator_from_row(row)
        if set(locator_by_id) != set(unique_evidence_ids):
            _fail("graph_publication_invariant_failed")

    hydrated_facts = tuple(
        HydratedFactEvidence(
            item_kind=fact.item_kind,
            fact_id=fact.fact_id,
            locators=tuple(locator_by_id[evidence_id] for evidence_id in evidence_ids),
        )
        for fact, evidence_ids in zip(facts, selected_by_fact)
    )
    return GraphEvidenceHydration(
        facts=hydrated_facts,
        truncated=truncated,
    )


def _empty_graph_evidence(
    traversal: PublishedGraphTraversal,
) -> GraphEvidenceHydration:
    return GraphEvidenceHydration(
        facts=tuple(
            HydratedFactEvidence(fact.item_kind, fact.fact_id, ())
            for fact in _selected_graph_facts(traversal)
        ),
        truncated=False,
    )


def build_graph_retrieval_response(
    result: GraphRetrievalTraversalResolution,
    hydration: GraphEvidenceHydration,
    *,
    contract_version: str,
) -> GraphRetrievalQueryResponse:
    resolution = result.resolution
    traversal = result.traversal
    facts = _selected_graph_facts(traversal)
    if len(facts) != len(hydration.facts):
        _fail("graph_publication_invariant_failed")
    evidence_by_fact: dict[
        tuple[str, uuid.UUID], tuple[GraphRetrievalEvidenceLocator, ...]
    ] = {}
    for selected, hydrated in zip(facts, hydration.facts):
        if (
            selected.item_kind != hydrated.item_kind
            or selected.fact_id != hydrated.fact_id
            or (hydrated.item_kind, hydrated.fact_id) in evidence_by_fact
        ):
            _fail("graph_publication_invariant_failed")
        evidence_by_fact[(hydrated.item_kind, hydrated.fact_id)] = hydrated.locators

    snapshot = resolution.snapshot
    try:
        nodes = [
            GraphRetrievalNodeRead(
                id=node.entity_id,
                item_hash=node.item_hash,
                entity_type=GraphRetrievalEntityTypeRead(
                    id=node.entity_type_id,
                    key=node.entity_type_key,
                    label=node.entity_type_label,
                ),
                canonical_name=node.canonical_name,
                normalized_name=node.normalized_name,
                source_type=node.source_type,
                confidence=node.confidence,
                depth=node.depth,
                evidence=list(evidence_by_fact[("entity", node.entity_id)]),
            )
            for node in traversal.nodes
        ]
        relations = [
            GraphRetrievalRelationRead(
                id=relation.relation_id,
                item_hash=relation.item_hash,
                relation_type=GraphRetrievalRelationTypeRead(
                    id=relation.relation_type_id,
                    key=relation.relation_type_key,
                    label=relation.relation_type_label,
                    direction=relation.relation_type_direction,
                ),
                source_entity_id=relation.source_entity_id,
                target_entity_id=relation.target_entity_id,
                source_type=relation.source_type,
                confidence=relation.confidence,
                depth=relation.depth,
                evidence=list(evidence_by_fact[("relation", relation.relation_id)]),
            )
            for relation in traversal.relations
        ]
        evidence_count = sum(len(row.evidence) for row in (*nodes, *relations))
        return GraphRetrievalQueryResponse(
            contract_version=contract_version,
            publication=GraphRetrievalPublicationRead(
                id=snapshot.publication_id,
                ontology_version_id=snapshot.ontology_version_id,
                manifest_version=snapshot.manifest_version,
                manifest_hash=snapshot.manifest_hash,
                activated_at=snapshot.activated_at,
            ),
            seed_matches=[
                GraphRetrievalSeedMatch(
                    input_index=seed.input_index,
                    entity_id=seed.entity_id,
                )
                for seed in resolution.seeds
            ],
            nodes=nodes,
            relations=relations,
            counts=GraphRetrievalCounts(
                seeds=len(resolution.seeds),
                nodes=len(nodes),
                relations=len(relations),
                evidence_locators=evidence_count,
            ),
            truncated=GraphRetrievalTruncation(
                nodes=traversal.truncated.nodes,
                relations=traversal.truncated.relations,
                evidence=hydration.truncated,
            ),
        )
    except (ValidationError, KeyError):
        _fail("graph_publication_invariant_failed")


async def execute_graph_retrieval_query(
    db: AsyncSession,
    library: Library,
    request: GraphRetrievalQueryRequest,
    *,
    config: Settings = settings,
) -> GraphRetrievalQueryResponse:
    result = await _resolve_and_traverse_graph_retrieval_query_unfenced(
        db,
        library,
        request,
        config=config,
    )
    if request.include_evidence_locators:
        try:
            hydration = await hydrate_graph_evidence_locators(
                db,
                library,
                result,
                max_evidence_per_fact=config.graph_retrieval_max_evidence_per_fact,
            )
        except GraphRetrievalServiceError as exc:
            if exc.code == "graph_publication_invariant_failed":
                await assert_graph_snapshot_still_current(
                    db,
                    library,
                    result.resolution.snapshot,
                )
            raise
    else:
        hydration = _empty_graph_evidence(result.traversal)
    response = build_graph_retrieval_response(
        result,
        hydration,
        contract_version=config.graph_retrieval_contract_version,
    )
    await assert_graph_snapshot_still_current(
        db,
        library,
        result.resolution.snapshot,
    )
    return response
