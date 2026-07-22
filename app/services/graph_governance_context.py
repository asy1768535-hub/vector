from __future__ import annotations

from collections import defaultdict
from typing import TypeVar

from sqlalchemy import select

from app.models.entity_type import EntityType
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.schemas.graph_governance import (
    GraphGovernanceEntityTypeOptionRead,
    GraphGovernanceOntologyOptionRead,
    GraphGovernanceRelationTypeOptionRead,
    GraphGovernanceWriteContextRead,
)
from app.services.graph_governance_contracts import GraphGovernanceError


MAX_ONTOLOGY_OPTIONS = 20
MAX_TYPE_OPTIONS_PER_ONTOLOGY = 100
_TypeRow = TypeVar("_TypeRow", EntityType, RelationType)


def _group_types(
    values: tuple[_TypeRow, ...],
) -> dict[object, list[_TypeRow]]:
    grouped: dict[object, list[_TypeRow]] = defaultdict(list)
    for value in values:
        grouped[value.ontology_version_id].append(value)
    if any(len(rows) > MAX_TYPE_OPTIONS_PER_ONTOLOGY for rows in grouped.values()):
        raise GraphGovernanceError("graph_governance_unavailable")
    return grouped


async def load_graph_governance_write_context(
    db,
    *,
    library: Library,
) -> GraphGovernanceWriteContextRead:
    ontologies = tuple(
        (
            await db.execute(
                select(OntologyVersion)
                .where(
                    OntologyVersion.library_id == library.id,
                    OntologyVersion.status == "active",
                )
                .order_by(
                    OntologyVersion.version_key,
                    OntologyVersion.version_no,
                    OntologyVersion.id,
                )
                .limit(MAX_ONTOLOGY_OPTIONS + 1)
            )
        )
        .scalars()
        .all()
    )
    if len(ontologies) > MAX_ONTOLOGY_OPTIONS:
        raise GraphGovernanceError("graph_governance_unavailable")
    ontology_ids = tuple(value.id for value in ontologies)
    entity_types: tuple[EntityType, ...] = ()
    relation_types: tuple[RelationType, ...] = ()
    if ontology_ids:
        type_limit = len(ontology_ids) * MAX_TYPE_OPTIONS_PER_ONTOLOGY + 1
        entity_types = tuple(
            (
                await db.execute(
                    select(EntityType)
                    .where(
                        EntityType.library_id == library.id,
                        EntityType.ontology_version_id.in_(ontology_ids),
                        EntityType.status == "active",
                    )
                    .order_by(
                        EntityType.ontology_version_id,
                        EntityType.key,
                        EntityType.id,
                    )
                    .limit(type_limit)
                )
            )
            .scalars()
            .all()
        )
        relation_types = tuple(
            (
                await db.execute(
                    select(RelationType)
                    .where(
                        RelationType.library_id == library.id,
                        RelationType.ontology_version_id.in_(ontology_ids),
                        RelationType.status == "active",
                    )
                    .order_by(
                        RelationType.ontology_version_id,
                        RelationType.key,
                        RelationType.id,
                    )
                    .limit(type_limit)
                )
            )
            .scalars()
            .all()
        )

    entities_by_ontology = _group_types(entity_types)
    relations_by_ontology = _group_types(relation_types)
    return GraphGovernanceWriteContextRead(
        library_id=library.id,
        library_slug=library.slug,
        ontology_versions=[
            GraphGovernanceOntologyOptionRead(
                id=ontology.id,
                version_key=ontology.version_key,
                version_no=ontology.version_no,
                entity_types=[
                    GraphGovernanceEntityTypeOptionRead(
                        id=value.id,
                        key=value.key,
                        label=value.label,
                    )
                    for value in entities_by_ontology.get(ontology.id, [])
                ],
                relation_types=[
                    GraphGovernanceRelationTypeOptionRead(
                        id=value.id,
                        key=value.key,
                        label=value.label,
                        direction=value.direction,
                        default_review_policy=value.default_review_policy,
                        requires_evidence=value.requires_evidence,
                    )
                    for value in relations_by_ontology.get(ontology.id, [])
                ],
            )
            for ontology in ontologies
        ],
    )
