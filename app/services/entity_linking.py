from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Literal, NoReturn, Sequence

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models.entity import Entity
from app.models.entity_type import EntityType
from app.models.graph_publication_item import GraphPublicationItem
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.schemas.v07_entity_linking import (
    EntityLinkingCandidateRead,
    EntityLinkingCounts,
    EntityLinkingPublicationRead,
    EntityLinkingResolveRequest,
    EntityLinkingResolveResponse,
    EntityLinkingResult,
)
from app.services import graph_retrieval
from app.services.entity_linking_scorer import (
    EntityLinkingCandidate,
    EntityLinkingDecision,
    ScoredEntityLinkingCandidate,
    resolve_mention,
)
from app.services.graph_normalization import normalize_graph_name_v1


EntityLinkingServiceErrorCode = Literal[
    "publication_changed",
    "entity_type_not_found",
    "entity_linking_limit_exceeded",
    "graph_publication_unavailable",
    "graph_publication_invariant_failed",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SCHEMA_ENTITY_TYPES = 10_000


class EntityLinkingServiceError(RuntimeError):
    def __init__(self, code: EntityLinkingServiceErrorCode) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OntologyEntityTypeIdentity:
    entity_type_id: uuid.UUID
    key: str
    label: str


@dataclass(frozen=True, slots=True)
class OntologySchemaIdentity:
    schema_hash: str
    entity_types: tuple[OntologyEntityTypeIdentity, ...]


def _fail(code: EntityLinkingServiceErrorCode) -> NoReturn:
    raise EntityLinkingServiceError(code)


def _translate_graph_error(
    exc: graph_retrieval.GraphRetrievalServiceError,
) -> NoReturn:
    if exc.code in {
        "publication_changed",
        "graph_publication_unavailable",
        "graph_publication_invariant_failed",
    }:
        _fail(exc.code)
    _fail("graph_publication_invariant_failed")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_entity_linking_request_limits(
    request: EntityLinkingResolveRequest,
    config: Settings,
) -> None:
    if len(request.mentions) > config.entity_linking_max_mentions:
        _fail("entity_linking_limit_exceeded")
    if request.max_candidates_per_mention > config.entity_linking_max_candidates:
        _fail("entity_linking_limit_exceeded")


async def load_ontology_schema_identity(
    db: AsyncSession,
    library: Library,
    ontology_version_id: uuid.UUID,
) -> OntologySchemaIdentity:
    rows = (
        await db.execute(
            select(
                OntologyVersion.id.label("ontology_version_id"),
                OntologyVersion.library_id.label("ontology_library_id"),
                OntologyVersion.version_key.label("version_key"),
                OntologyVersion.version_no.label("version_no"),
                OntologyVersion.status.label("ontology_status"),
                EntityType.id.label("entity_type_id"),
                EntityType.key.label("entity_type_key"),
                EntityType.label.label("entity_type_label"),
                EntityType.status.label("entity_type_status"),
            )
            .select_from(OntologyVersion)
            .outerjoin(
                EntityType,
                and_(
                    EntityType.library_id == library.id,
                    EntityType.ontology_version_id == OntologyVersion.id,
                    EntityType.status == "active",
                ),
            )
            .where(
                OntologyVersion.id == ontology_version_id,
                OntologyVersion.library_id == library.id,
                OntologyVersion.status == "active",
            )
            .order_by(EntityType.id)
            .limit(_MAX_SCHEMA_ENTITY_TYPES + 1)
        )
    ).all()
    if not rows:
        _fail("graph_publication_invariant_failed")
    if len(rows) > _MAX_SCHEMA_ENTITY_TYPES:
        _fail("graph_publication_invariant_failed")
    first = rows[0]
    if (
        first.ontology_version_id != ontology_version_id
        or first.ontology_library_id != library.id
        or first.ontology_status != "active"
    ):
        _fail("graph_publication_invariant_failed")

    entity_types = tuple(
        OntologyEntityTypeIdentity(
            entity_type_id=row.entity_type_id,
            key=row.entity_type_key,
            label=row.entity_type_label,
        )
        for row in rows
        if row.entity_type_id is not None
    )
    if len({row.entity_type_id for row in entity_types}) != len(entity_types):
        _fail("graph_publication_invariant_failed")
    if len({row.key for row in entity_types}) != len(entity_types):
        _fail("graph_publication_invariant_failed")
    if any(not row.key or not row.label for row in entity_types):
        _fail("graph_publication_invariant_failed")

    payload = {
        "identity_version": "entity-linking-ontology-schema-v1",
        "library_id": str(library.id).lower(),
        "ontology_version_id": str(ontology_version_id).lower(),
        "version_key": first.version_key,
        "version_no": first.version_no,
        "ontology_status": first.ontology_status,
        "entity_types": [
            {
                "id": str(row.entity_type_id).lower(),
                "key": row.key,
                "label": row.label,
                "status": "active",
            }
            for row in entity_types
        ],
    }
    return OntologySchemaIdentity(
        schema_hash=_canonical_sha256(payload),
        entity_types=entity_types,
    )


def _published_entity_projection(snapshot: graph_retrieval.HealthyGraphSnapshot):
    return (
        select(
            GraphPublicationItem.item_hash.label("item_hash"),
            Entity.id.label("entity_id"),
            Entity.entity_type_id.label("entity_type_id"),
            Entity.canonical_name.label("canonical_name"),
            Entity.normalized_name.label("normalized_name"),
            EntityType.id.label("joined_entity_type_id"),
            EntityType.key.label("entity_type_key"),
            EntityType.label.label("entity_type_label"),
        )
        .select_from(GraphPublicationItem)
        .join(
            Entity,
            and_(
                GraphPublicationItem.entity_id == Entity.id,
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
            GraphPublicationItem.publication_id == snapshot.publication_id,
            GraphPublicationItem.library_id == snapshot.library_id,
            GraphPublicationItem.ontology_version_id == snapshot.ontology_version_id,
            GraphPublicationItem.item_kind == "entity",
            GraphPublicationItem.status == "active",
        )
        .order_by(Entity.id)
    )


async def load_published_entity_candidates(
    db: AsyncSession,
    snapshot: graph_retrieval.HealthyGraphSnapshot,
    schema: OntologySchemaIdentity,
) -> tuple[EntityLinkingCandidate, ...]:
    rows = (await db.execute(_published_entity_projection(snapshot))).all()
    if len(rows) != snapshot.entity_count:
        _fail("graph_publication_invariant_failed")

    schema_types = {row.entity_type_id: row for row in schema.entity_types}
    output = []
    seen_ids: set[uuid.UUID] = set()
    for row in rows:
        schema_type = schema_types.get(row.entity_type_id)
        if (
            row.entity_id in seen_ids
            or row.entity_type_id != row.joined_entity_type_id
            or schema_type is None
            or schema_type.key != row.entity_type_key
            or schema_type.label != row.entity_type_label
            or not _SHA256_RE.fullmatch(row.item_hash or "")
            or not row.canonical_name
            or normalize_graph_name_v1(row.canonical_name) != row.normalized_name
        ):
            _fail("graph_publication_invariant_failed")
        seen_ids.add(row.entity_id)
        output.append(
            EntityLinkingCandidate(
                entity_id=row.entity_id,
                item_hash=row.item_hash,
                entity_type_id=row.entity_type_id,
                entity_type_key=row.entity_type_key,
                entity_type_label=row.entity_type_label,
                canonical_name=row.canonical_name,
                normalized_name=row.normalized_name,
            )
        )
    return tuple(output)


def _candidate_read(row: ScoredEntityLinkingCandidate) -> EntityLinkingCandidateRead:
    candidate = row.candidate
    return EntityLinkingCandidateRead(
        entity_id=candidate.entity_id,
        item_hash=candidate.item_hash,
        entity_type_id=candidate.entity_type_id,
        entity_type_key=candidate.entity_type_key,
        entity_type_label=candidate.entity_type_label,
        canonical_name=candidate.canonical_name,
        score_micros=row.score_micros,
    )


def _result_read(
    input_index: int,
    decision: EntityLinkingDecision,
    max_candidates: int,
) -> EntityLinkingResult:
    return EntityLinkingResult(
        input_index=input_index,
        status=decision.status,
        method=decision.method,
        selected=_candidate_read(decision.selected) if decision.selected else None,
        candidates=[_candidate_read(row) for row in decision.candidates[:max_candidates]],
    )


def _counts(results: Sequence[EntityLinkingResult]) -> EntityLinkingCounts:
    return EntityLinkingCounts(
        mentions=len(results),
        linked_exact=sum(row.method == "exact_canonical" for row in results),
        linked_lexical=sum(row.method == "lexical_v1" for row in results),
        ambiguous=sum(row.status == "ambiguous" for row in results),
        not_found=sum(row.status == "not_found" for row in results),
        candidates=sum(len(row.candidates) for row in results),
    )


async def execute_entity_linking(
    db: AsyncSession,
    library: Library,
    request: EntityLinkingResolveRequest,
    *,
    config: Settings = settings,
) -> EntityLinkingResolveResponse:
    validate_entity_linking_request_limits(request, config)
    try:
        snapshot = await graph_retrieval.load_healthy_graph_snapshot(
            db,
            library,
            request.ontology_version_id,
            expected_publication_id=request.expected_publication_id,
        )
    except graph_retrieval.GraphRetrievalServiceError as exc:
        _translate_graph_error(exc)

    if snapshot.entity_count > config.entity_linking_max_publication_entities:
        _fail("entity_linking_limit_exceeded")

    schema = await load_ontology_schema_identity(
        db,
        library,
        request.ontology_version_id,
    )
    known_type_keys = {row.key for row in schema.entity_types}
    requested_type_keys = {
        mention.entity_type_key
        for mention in request.mentions
        if mention.entity_type_key is not None
    }
    if not requested_type_keys <= known_type_keys:
        _fail("entity_type_not_found")

    candidates = await load_published_entity_candidates(db, snapshot, schema)
    decisions = tuple(
        resolve_mention(
            mention.text,
            candidates,
            entity_type_key=mention.entity_type_key,
            min_score_micros=config.entity_linking_min_score_micros,
            min_margin_micros=config.entity_linking_min_margin_micros,
            candidate_floor_micros=config.entity_linking_candidate_floor_micros,
            max_candidates=config.entity_linking_max_candidates,
        )
        for mention in request.mentions
    )

    try:
        await graph_retrieval.assert_graph_snapshot_still_current(db, library, snapshot)
    except graph_retrieval.GraphRetrievalServiceError as exc:
        _translate_graph_error(exc)
    end_schema = await load_ontology_schema_identity(
        db,
        library,
        request.ontology_version_id,
    )
    if end_schema != schema:
        _fail("publication_changed")

    results = [
        _result_read(index, decision, request.max_candidates_per_mention)
        for index, decision in enumerate(decisions)
    ]
    return EntityLinkingResolveResponse(
        contract_version=config.entity_linking_contract_version,
        policy_version=config.entity_linking_policy_version,
        publication=EntityLinkingPublicationRead(
            id=snapshot.publication_id,
            ontology_version_id=snapshot.ontology_version_id,
            manifest_version=snapshot.manifest_version,
            manifest_hash=snapshot.manifest_hash,
            ontology_schema_hash=schema.schema_hash,
            activated_at=snapshot.activated_at,
        ),
        results=results,
        counts=_counts(results),
    )
