from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, Sequence

from sqlalchemy import select

from app.config import settings
from app.models.attribute_definition import AttributeDefinition
from app.models.entity_type import EntityType
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services.graph_canonical import canonical_graph_value_hash_v1
from app.services.library_compatibility_contracts import (
    GRAPH_PROFILE_VERSION,
    CompatibilityFingerprint,
)


def canonical_schema_payload(
    ontology: OntologyVersion,
    entity_types: Sequence[EntityType],
    relation_types: Sequence[RelationType],
    constraints: Sequence[RelationTypeConstraint],
    attributes: Sequence[AttributeDefinition],
) -> dict[str, Any]:
    for row in (*entity_types, *relation_types, *constraints, *attributes):
        if (
            row.library_id != ontology.library_id
            or row.ontology_version_id != ontology.id
        ):
            raise ValueError("graph schema scope is invalid")
    entity_by_id = {row.id: row.key for row in entity_types}
    relation_by_id = {row.id: row.key for row in relation_types}
    entities = sorted(
        (
            {
                "key": row.key,
                "label": row.label,
                "description": row.description,
                "properties_schema": row.properties_schema,
            }
            for row in entity_types
        ),
        key=lambda row: row["key"],
    )
    relations = sorted(
        (
            {
                "key": row.key,
                "label": row.label,
                "description": row.description,
                "direction": row.direction,
                "requires_evidence": row.requires_evidence,
                "default_review_policy": row.default_review_policy,
                "properties_schema": row.properties_schema,
            }
            for row in relation_types
        ),
        key=lambda row: row["key"],
    )
    constraint_values = sorted(
        (
            {
                "relation_type_key": relation_by_id[row.relation_type_id],
                "source_entity_type_key": entity_by_id[row.source_entity_type_id],
                "target_entity_type_key": entity_by_id[row.target_entity_type_id],
                "cardinality": row.cardinality,
                "requires_review": row.requires_review,
            }
            for row in constraints
        ),
        key=lambda row: (
            row["relation_type_key"],
            row["source_entity_type_key"],
            row["target_entity_type_key"],
        ),
    )

    def owner_key(row: AttributeDefinition) -> str:
        owners = entity_by_id if row.owner_kind == "entity_type" else relation_by_id
        return owners[row.owner_type_id]

    attribute_values = sorted(
        (
            {
                "owner_kind": row.owner_kind,
                "owner_type_key": owner_key(row),
                "key": row.key,
                "label": row.label,
                "value_type": row.value_type,
                "required": row.required,
                "enum_values": row.enum_values,
                "validation_schema": row.validation_schema,
                "indexed": row.indexed,
            }
            for row in attributes
        ),
        key=lambda row: (row["owner_kind"], row["owner_type_key"], row["key"]),
    )
    return {
        "entity_types": entities,
        "relation_types": relations,
        "constraints": constraint_values,
        "attributes": attribute_values,
    }


async def build_graph_profiles(
    db,
    libraries: Sequence[Library],
) -> dict[uuid.UUID, CompatibilityFingerprint]:
    library_ids = tuple(library.id for library in libraries)
    ontologies = (
        await db.execute(
            select(OntologyVersion)
            .where(
                OntologyVersion.library_id.in_(library_ids),
                OntologyVersion.status == "active",
            )
            .order_by(
                OntologyVersion.library_id,
                OntologyVersion.version_key,
                OntologyVersion.version_no.desc(),
                OntologyVersion.id,
            )
        )
    ).scalars().all()
    ontologies_by_library: dict[uuid.UUID, list[OntologyVersion]] = defaultdict(list)
    for ontology in ontologies:
        ontologies_by_library[ontology.library_id].append(ontology)
    selected = {
        library_id: rows[0]
        for library_id, rows in ontologies_by_library.items()
        if len(rows) == 1
    }
    ontology_ids = tuple(ontology.id for ontology in selected.values())
    if ontology_ids:
        entity_types = (
            await db.execute(
                select(EntityType).where(
                    EntityType.ontology_version_id.in_(ontology_ids),
                    EntityType.status == "active",
                )
            )
        ).scalars().all()
        relation_types = (
            await db.execute(
                select(RelationType).where(
                    RelationType.ontology_version_id.in_(ontology_ids),
                    RelationType.status == "active",
                )
            )
        ).scalars().all()
        constraints = (
            await db.execute(
                select(RelationTypeConstraint).where(
                    RelationTypeConstraint.ontology_version_id.in_(ontology_ids),
                    RelationTypeConstraint.status == "active",
                )
            )
        ).scalars().all()
        attributes = (
            await db.execute(
                select(AttributeDefinition).where(
                    AttributeDefinition.ontology_version_id.in_(ontology_ids),
                    AttributeDefinition.status == "active",
                )
            )
        ).scalars().all()
        publications = (
            await db.execute(
                select(GraphPublication).where(
                    GraphPublication.library_id.in_(library_ids),
                    GraphPublication.ontology_version_id.in_(ontology_ids),
                    GraphPublication.status.in_(("active", "degraded")),
                )
            )
        ).scalars().all()
    else:
        entity_types = relation_types = constraints = attributes = publications = []

    def grouped(rows: Sequence[Any]) -> dict[uuid.UUID, list[Any]]:
        values: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for row in rows:
            values[row.ontology_version_id].append(row)
        return values

    entities_by_ontology = grouped(entity_types)
    relations_by_ontology = grouped(relation_types)
    constraints_by_ontology = grouped(constraints)
    attributes_by_ontology = grouped(attributes)
    publications_by_scope: dict[tuple[uuid.UUID, uuid.UUID], list[GraphPublication]] = (
        defaultdict(list)
    )
    for publication in publications:
        publications_by_scope[
            (publication.library_id, publication.ontology_version_id)
        ].append(publication)
    profiles: dict[uuid.UUID, CompatibilityFingerprint] = {}
    for library in libraries:
        ontology_rows = ontologies_by_library.get(library.id, [])
        if not ontology_rows:
            profiles[library.id] = CompatibilityFingerprint(
                GRAPH_PROFILE_VERSION, None, False, "graph_ontology_missing"
            )
            continue
        if len(ontology_rows) != 1:
            profiles[library.id] = CompatibilityFingerprint(
                GRAPH_PROFILE_VERSION, None, False, "graph_ontology_ambiguous"
            )
            continue
        ontology = ontology_rows[0]
        publication_rows = publications_by_scope.get((library.id, ontology.id), [])
        if not publication_rows:
            profiles[library.id] = CompatibilityFingerprint(
                GRAPH_PROFILE_VERSION, None, False, "graph_publication_missing"
            )
            continue
        if len(publication_rows) != 1 or publication_rows[0].status != "active":
            profiles[library.id] = CompatibilityFingerprint(
                GRAPH_PROFILE_VERSION, None, False, "graph_publication_unhealthy"
            )
            continue
        publication = publication_rows[0]
        policy_snapshot = publication.policy_snapshot
        if (
            not isinstance(policy_snapshot, dict)
            or policy_snapshot.get("manifest_version") != publication.manifest_version
            or policy_snapshot.get("policy_version") != publication.policy_version
        ):
            profiles[library.id] = CompatibilityFingerprint(
                GRAPH_PROFILE_VERSION, None, False, "graph_publication_unhealthy"
            )
            continue
        try:
            schema = canonical_schema_payload(
                ontology,
                entities_by_ontology.get(ontology.id, ()),
                relations_by_ontology.get(ontology.id, ()),
                constraints_by_ontology.get(ontology.id, ()),
                attributes_by_ontology.get(ontology.id, ()),
            )
        except (KeyError, TypeError, ValueError):
            profiles[library.id] = CompatibilityFingerprint(
                GRAPH_PROFILE_VERSION, None, False, "graph_schema_invalid"
            )
            continue
        try:
            fingerprint = canonical_graph_value_hash_v1(
                {
                    "contract_version": GRAPH_PROFILE_VERSION,
                    "schema": schema,
                    "publication": {
                        "manifest_version": publication.manifest_version,
                        "policy_version": publication.policy_version,
                        "policy_snapshot": policy_snapshot,
                    },
                    "retrieval": {
                        "enabled": settings.graph_retrieval_enabled,
                        "contract_version": settings.graph_retrieval_contract_version,
                        "max_seeds": settings.graph_retrieval_max_seeds,
                        "max_hops": settings.graph_retrieval_max_hops,
                        "max_nodes": settings.graph_retrieval_max_nodes,
                        "max_relations": settings.graph_retrieval_max_relations,
                        "max_evidence_per_fact": settings.graph_retrieval_max_evidence_per_fact,
                    },
                    "entity_linking": {
                        "contract_version": settings.entity_linking_contract_version,
                        "policy_version": settings.entity_linking_policy_version,
                        "policy_sha256": settings.entity_linking_policy_sha256 or None,
                    },
                    "evidence_locator_contract": "evidence-locator-v1",
                }
            )
        except (TypeError, ValueError):
            profiles[library.id] = CompatibilityFingerprint(
                GRAPH_PROFILE_VERSION, None, False, "graph_publication_unhealthy"
            )
            continue
        profiles[library.id] = CompatibilityFingerprint(
            GRAPH_PROFILE_VERSION,
            fingerprint,
            True,
        )
    return profiles
