from __future__ import annotations

import hashlib
import math
import platform
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.backend import current_active_user
from app.config import Settings, settings
from app.db import get_db
from app.main import app
from app.models.audit import AuditLog
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.entity import Entity
from app.models.entity_mention import EntityMention
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.user import User
from app.schemas.v06_graph_retrieval import GraphRetrievalQueryRequest
from app.services import graph_retrieval
from app.services.graph_retrieval_eval import (
    GraphRetrievalCaseResponseHash,
    GraphRetrievalCaseScore,
    GraphRetrievalEvalCase,
    GraphRetrievalEvalResultArtifact,
    GraphRetrievalEvalThresholds,
    GraphRetrievalExplainStats,
    GraphRetrievalLatencyStats,
    LoadedGraphRetrievalEvalPolicy,
    LoadedGraphRetrievalEvalDataset,
    build_graph_retrieval_metric_report,
    canonical_graph_retrieval_response_hash,
    canonical_json_sha256,
    nearest_rank_latency,
)


GRAPH_RETRIEVAL_EVAL_UUID_NAMESPACE = uuid.UUID("cb2f1c85-28c2-5797-b4fe-350d6549dd33")
GRAPH_RETRIEVAL_EVAL_ACTIVATED_AT = datetime(2026, 7, 16, tzinfo=timezone.utc)
GRAPH_RETRIEVAL_EVAL_DB_RE = re.compile(r"^vkt_v06_m5_eval_[a-z0-9_]+$")
GRAPH_RETRIEVAL_EVAL_WARMUPS = 5
GRAPH_RETRIEVAL_EVAL_SAMPLES = 30
GRAPH_RETRIEVAL_EVAL_HARD_P95_US = 2_400_000


def graph_retrieval_eval_uuid(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(GRAPH_RETRIEVAL_EVAL_UUID_NAMESPACE, f"{kind}:{key}")


def _stable_hash(kind: str, key: str) -> str:
    return hashlib.sha256(f"graph-retrieval-eval:{kind}:{key}".encode()).hexdigest()


def validate_graph_retrieval_eval_database_id(database_id: str) -> str:
    if len(database_id) > 63 or GRAPH_RETRIEVAL_EVAL_DB_RE.fullmatch(database_id) is None:
        raise ValueError("database ID must use the vkt_v06_m5_eval_ prefix")
    return database_id


def graph_retrieval_eval_target_dsn(admin_dsn: str, database_id: str) -> str:
    validate_graph_retrieval_eval_database_id(database_id)
    url = make_url(admin_dsn)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("graph retrieval Eval requires PostgreSQL")
    return url.set(drivername="postgresql+asyncpg", database=database_id).render_as_string(
        hide_password=False
    )


async def _admin_connection(admin_dsn: str):
    import asyncpg

    url = make_url(admin_dsn)
    return await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database="postgres",
    )


async def graph_retrieval_eval_database_exists(admin_dsn: str, database_id: str) -> bool:
    validate_graph_retrieval_eval_database_id(database_id)
    connection = await _admin_connection(admin_dsn)
    try:
        return bool(
            await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = $1)",
                database_id,
            )
        )
    finally:
        await connection.close()


async def create_graph_retrieval_eval_database(admin_dsn: str, database_id: str) -> str:
    validate_graph_retrieval_eval_database_id(database_id)
    connection = await _admin_connection(admin_dsn)
    try:
        if await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = $1)",
            database_id,
        ):
            raise ValueError("graph retrieval Eval database already exists")
        await connection.execute(f'CREATE DATABASE "{database_id}"')
    finally:
        await connection.close()
    return graph_retrieval_eval_target_dsn(admin_dsn, database_id)


async def drop_graph_retrieval_eval_database(admin_dsn: str, database_id: str) -> None:
    validate_graph_retrieval_eval_database_id(database_id)
    connection = await _admin_connection(admin_dsn)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{database_id}" WITH (FORCE)')
    finally:
        await connection.close()


@contextmanager
def _database_settings(dsn: str):
    url = make_url(dsn)
    original = {
        "db_host": settings.db_host,
        "db_port": settings.db_port,
        "db_user": settings.db_user,
        "db_password": settings.db_password,
        "db_name": settings.db_name,
    }
    settings.db_host = url.host or "localhost"
    settings.db_port = url.port or 5432
    settings.db_user = url.username or ""
    settings.db_password = url.password or ""
    settings.db_name = url.database or ""
    try:
        yield
    finally:
        for key, value in original.items():
            setattr(settings, key, value)


def upgrade_graph_retrieval_eval_database(repository_root: Path, dsn: str) -> None:
    config = Config(str(repository_root / "alembic.ini"))
    with _database_settings(dsn):
        command.upgrade(config, "0023")


@dataclass(frozen=True, slots=True)
class SeededGraphRetrievalEval:
    dataset: LoadedGraphRetrievalEvalDataset
    uuid_by_logical: Mapping[str, uuid.UUID]
    logical_by_uuid: Mapping[str, str]
    library_id_by_key: Mapping[str, uuid.UUID]
    library_slug_by_key: Mapping[str, str]
    ontology_id_by_key: Mapping[str, uuid.UUID]
    publication_id_by_scope: Mapping[str, uuid.UUID]
    stale_primary_publication_id: uuid.UUID
    primary_partial_item_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class GraphRetrievalCorrectnessResult:
    scores: tuple[GraphRetrievalCaseScore, ...]
    response_hashes: tuple[GraphRetrievalCaseResponseHash, ...]
    canonical_response_set_sha256: str
    passed_cases: int
    failed_cases: int
    stable_error_code_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class SeededGraphRetrievalPerformance:
    library_id: uuid.UUID
    library_slug: str
    ontology_id: uuid.UUID
    publication_id: uuid.UUID
    entity_type_id: uuid.UUID
    relation_type_id: uuid.UUID
    evidence_id: uuid.UUID
    seed_entity_id: uuid.UUID
    high_degree_seed_id: uuid.UUID
    first_relation_id: uuid.UUID
    first_relation_item_hash: str
    logical_by_uuid: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GraphRetrievalPerformanceResult:
    latency: Mapping[str, GraphRetrievalLatencyStats]
    explain: Mapping[str, GraphRetrievalExplainStats]
    environment_fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class GraphRetrievalEvalRunMaterial:
    correctness: GraphRetrievalCorrectnessResult
    performance: GraphRetrievalPerformanceResult
    query_state_unchanged: bool
    timeout_rollback_reused: bool
    disabled_compatibility_passed: bool


def _uuid_maps(dataset: LoadedGraphRetrievalEvalDataset) -> tuple[dict[str, uuid.UUID], dict[str, str]]:
    gold = dataset.gold
    typed_keys: list[tuple[str, str]] = []
    typed_keys.extend(("library", row.library_key) for row in gold.libraries)
    typed_keys.extend(("ontology", row.ontology_key) for row in gold.ontologies)
    typed_keys.extend(("entity-type", row.type_key) for row in gold.entity_types)
    typed_keys.extend(("relation-type", row.type_key) for row in gold.relation_types)
    typed_keys.extend(("entity", row.entity_key) for row in gold.entities)
    typed_keys.extend(("relation", row.relation_key) for row in gold.relations)
    typed_keys.extend(("evidence", row.evidence_key) for row in gold.evidence)
    typed_keys.extend(("document", row.document_key) for row in gold.evidence)
    typed_keys.extend(("revision", row.revision_key) for row in gold.evidence)
    typed_keys.extend(
        ("block", row.block_key) for row in gold.evidence if row.block_key is not None
    )
    uuid_by_logical = {
        key: graph_retrieval_eval_uuid(kind, key) for kind, key in typed_keys
    }
    logical_by_uuid = {str(value): key for key, value in uuid_by_logical.items()}
    return uuid_by_logical, logical_by_uuid


async def seed_graph_retrieval_eval_dataset(
    Session: async_sessionmaker[AsyncSession],
    dataset: LoadedGraphRetrievalEvalDataset,
) -> SeededGraphRetrievalEval:
    gold = dataset.gold
    uuid_by_logical, logical_by_uuid = _uuid_maps(dataset)
    library_id_by_key = {
        row.library_key: uuid_by_logical[row.library_key] for row in gold.libraries
    }
    ontology_id_by_key = {
        row.ontology_key: uuid_by_logical[row.ontology_key] for row in gold.ontologies
    }
    library_by_key = {row.library_key: row for row in gold.libraries}
    ontology_by_key = {row.ontology_key: row for row in gold.ontologies}
    evidence_by_key = {row.evidence_key: row for row in gold.evidence}

    async with Session() as db:
        await db.execute(
            insert(Library),
            [
                {
                    "id": uuid_by_logical[row.library_key],
                    "slug": row.slug,
                    "name": row.slug,
                    "embedding_model": "bge-m3",
                    "embedding_dim": 1024,
                    "qdrant_collection": row.slug,
                }
                for row in gold.libraries
            ],
        )
        await db.execute(
            insert(OntologyVersion),
            [
                {
                    "id": uuid_by_logical[row.ontology_key],
                    "library_id": uuid_by_logical[row.library_key],
                    "version_key": row.version_key,
                    "version_no": index + 1,
                    "status": "active",
                }
                for index, row in enumerate(gold.ontologies)
            ],
        )
        await db.execute(
            insert(EntityType),
            [
                {
                    "id": uuid_by_logical[row.type_key],
                    "library_id": uuid_by_logical[ontology_by_key[row.ontology_key].library_key],
                    "ontology_version_id": uuid_by_logical[row.ontology_key],
                    "key": row.type_key,
                    "label": row.label,
                    "properties_schema": {},
                    "status": "active",
                }
                for row in gold.entity_types
            ],
        )
        await db.execute(
            insert(RelationType),
            [
                {
                    "id": uuid_by_logical[row.type_key],
                    "library_id": uuid_by_logical[ontology_by_key[row.ontology_key].library_key],
                    "ontology_version_id": uuid_by_logical[row.ontology_key],
                    "key": row.type_key,
                    "label": row.label,
                    "direction": row.direction,
                    "requires_evidence": False,
                    "default_review_policy": "auto_active",
                    "properties_schema": {},
                    "status": "active",
                }
                for row in gold.relation_types
            ],
        )
        await db.execute(
            insert(Entity),
            [
                {
                    "id": uuid_by_logical[row.entity_key],
                    "library_id": uuid_by_logical[
                        ontology_by_key[row.ontology_key].library_key
                    ],
                    "ontology_version_id": uuid_by_logical[row.ontology_key],
                    "entity_type_id": uuid_by_logical[row.entity_type_key],
                    "canonical_name": row.canonical_name,
                    "normalized_name": row.normalized_name,
                    "properties": row.properties,
                    "status": "active",
                    "source_type": row.source_type,
                    "confidence": row.confidence,
                }
                for row in gold.entities
            ],
        )
        await db.execute(
            insert(Document),
            [
                {
                    "id": uuid_by_logical[row.document_key],
                    "library_id": uuid_by_logical[row.library_key],
                    "title": f"Synthetic Evidence {row.evidence_key}",
                    "content_hash": _stable_hash("document", row.document_key),
                    "current_revision": 1,
                    "current_revision_id": uuid_by_logical[row.revision_key],
                    "latest_revision_id": uuid_by_logical[row.revision_key],
                    "status": "ready",
                }
                for row in gold.evidence
            ],
        )
        await db.execute(
            insert(DocumentRevision),
            [
                {
                    "id": uuid_by_logical[row.revision_key],
                    "document_id": uuid_by_logical[row.document_key],
                    "library_id": uuid_by_logical[row.library_key],
                    "revision_no": 1,
                    "content_hash": _stable_hash("revision", row.revision_key),
                    "normalized_text": f"private revision {row.evidence_key}",
                    "parser_name": "synthetic",
                    "parser_version": "v1",
                    "chunking_strategy": "fixed",
                    "chunking_strategy_version": "v1",
                    "status": "ready",
                }
                for row in gold.evidence
            ],
        )
        block_rows = [row for row in gold.evidence if row.block_key is not None]
        await db.execute(
            insert(DocumentBlock),
            [
                {
                    "id": uuid_by_logical[row.block_key or ""],
                    "library_id": uuid_by_logical[row.library_key],
                    "document_id": uuid_by_logical[row.document_key],
                    "document_revision_id": uuid_by_logical[row.revision_key],
                    "seq": index,
                    "block_kind": "paragraph",
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "source_start": row.source_start,
                    "source_end": row.source_end,
                    "text": f"private source {row.evidence_key}",
                    "content": {"private": row.evidence_key},
                    "parser_name": "synthetic",
                    "parser_version": "v1",
                }
                for index, row in enumerate(block_rows)
            ],
        )
        await db.execute(
            insert(EvidenceUnit),
            [
                {
                    "id": uuid_by_logical[row.evidence_key],
                    "library_id": uuid_by_logical[row.library_key],
                    "document_id": uuid_by_logical[row.document_key],
                    "document_revision_id": uuid_by_logical[row.revision_key],
                    "document_block_id": (
                        uuid_by_logical[row.block_key] if row.block_key is not None else None
                    ),
                    "evidence_kind": row.evidence_kind,
                    "source_start": row.source_start,
                    "source_end": row.source_end,
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "text_quote": f"private quote {row.evidence_key}",
                    "evidence_metadata": {"private": row.evidence_key},
                    "status": "active",
                }
                for row in gold.evidence
            ],
        )
        await db.execute(
            insert(KnowledgeRelation),
            [
                {
                    "id": uuid_by_logical[row.relation_key],
                    "library_id": uuid_by_logical[
                        ontology_by_key[row.ontology_key].library_key
                    ],
                    "ontology_version_id": uuid_by_logical[row.ontology_key],
                    "relation_type_id": uuid_by_logical[row.relation_type_key],
                    "source_entity_id": uuid_by_logical[row.source_entity_key],
                    "target_entity_id": uuid_by_logical[row.target_entity_key],
                    "properties": row.properties,
                    "status": "active",
                    "review_status": "approved",
                    "source_type": row.source_type,
                    "confidence": row.confidence,
                }
                for row in gold.relations
            ],
        )

        mention_rows = []
        for entity in gold.entities:
            for evidence_key in entity.support_evidence_keys:
                evidence = evidence_by_key[evidence_key]
                mention_rows.append(
                    {
                        "id": graph_retrieval_eval_uuid(
                            "entity-mention", f"{entity.entity_key}:{evidence_key}"
                        ),
                        "library_id": uuid_by_logical[evidence.library_key],
                        "entity_id": uuid_by_logical[entity.entity_key],
                        "evidence_id": uuid_by_logical[evidence_key],
                        "document_id": uuid_by_logical[evidence.document_key],
                        "document_revision_id": uuid_by_logical[evidence.revision_key],
                        "mention_text": f"private mention {entity.entity_key}",
                        "quote_text": "private mention quote",
                        "evidence_text_snapshot": "private mention snapshot",
                        "source_type": "manual",
                        "status": "active",
                    }
                )
        if mention_rows:
            await db.execute(insert(EntityMention), mention_rows)

        support_rows = []
        for relation in gold.relations:
            for evidence_key in relation.support_evidence_keys:
                evidence = evidence_by_key[evidence_key]
                support_rows.append(
                    {
                        "id": graph_retrieval_eval_uuid(
                            "relation-evidence", f"{relation.relation_key}:{evidence_key}"
                        ),
                        "library_id": uuid_by_logical[evidence.library_key],
                        "relation_id": uuid_by_logical[relation.relation_key],
                        "evidence_id": uuid_by_logical[evidence_key],
                        "document_id": uuid_by_logical[evidence.document_key],
                        "document_revision_id": uuid_by_logical[evidence.revision_key],
                        "support_type": "supports",
                        "quote_text": "private relation quote",
                        "evidence_text_snapshot": "private relation snapshot",
                        "status": "active",
                    }
                )
        if support_rows:
            await db.execute(insert(RelationEvidence), support_rows)

        publication_scopes = {
            "primary": ("lib-primary", "ontology-primary-v1"),
            "secondary": ("lib-secondary", "ontology-secondary-v1"),
            "v2": ("lib-primary", "ontology-primary-v2"),
        }
        publication_id_by_scope = {
            scope: graph_retrieval_eval_uuid("publication", f"publication-{scope}")
            for scope in publication_scopes
        }
        for scope, (library_key, ontology_key) in publication_scopes.items():
            scoped_entities = [row for row in gold.entities if row.ontology_key == ontology_key]
            scoped_relations = [row for row in gold.relations if row.ontology_key == ontology_key]
            publication_id = publication_id_by_scope[scope]
            logical_by_uuid[str(publication_id)] = f"publication-{scope}"
            await db.execute(
                insert(GraphPublication),
                [
                    {
                        "id": publication_id,
                        "library_id": uuid_by_logical[library_key],
                        "ontology_version_id": uuid_by_logical[ontology_key],
                        "status": "active",
                        "source_mode": "initial_seed",
                        "manifest_version": "v1",
                        "policy_version": "v1",
                        "policy_snapshot": {},
                        "manifest_hash": _stable_hash("manifest", scope),
                        "idempotency_key": f"eval-{scope}",
                        "include_drafts": False,
                        "plan_options": {},
                        "entity_count": len(scoped_entities),
                        "relation_count": len(scoped_relations),
                        "blocked_counts": {},
                        "blocked_diagnostics": {},
                        "item_hashes_summary": {},
                        "activated_at": GRAPH_RETRIEVAL_EVAL_ACTIVATED_AT,
                    }
                ],
            )
            item_rows = []
            for entity in scoped_entities:
                supports = sorted(
                    str(uuid_by_logical[key]) for key in entity.support_evidence_keys
                )
                item_rows.append(
                    {
                        "id": graph_retrieval_eval_uuid(
                            "publication-item", f"{scope}:entity:{entity.entity_key}"
                        ),
                        "publication_id": publication_id,
                        "library_id": uuid_by_logical[library_key],
                        "ontology_version_id": uuid_by_logical[ontology_key],
                        "item_kind": "entity",
                        "entity_id": uuid_by_logical[entity.entity_key],
                        "relation_id": None,
                        "item_hash": _stable_hash("entity-item", entity.entity_key),
                        "status": "active",
                        "support_evidence_ids": supports,
                        "support_counts": {"total": len(supports)},
                        "fact_snapshot": {},
                    }
                )
            for relation in scoped_relations:
                supports = sorted(
                    str(uuid_by_logical[key]) for key in relation.support_evidence_keys
                )
                item_rows.append(
                    {
                        "id": graph_retrieval_eval_uuid(
                            "publication-item", f"{scope}:relation:{relation.relation_key}"
                        ),
                        "publication_id": publication_id,
                        "library_id": uuid_by_logical[library_key],
                        "ontology_version_id": uuid_by_logical[ontology_key],
                        "item_kind": "relation",
                        "entity_id": None,
                        "relation_id": uuid_by_logical[relation.relation_key],
                        "item_hash": _stable_hash("relation-item", relation.relation_key),
                        "status": "active",
                        "support_evidence_ids": supports,
                        "support_counts": {"total": len(supports)},
                        "fact_snapshot": {},
                    }
                )
            await db.execute(insert(GraphPublicationItem), item_rows)
        await db.commit()

    primary_partial_item_id = graph_retrieval_eval_uuid(
        "publication-item", "primary:entity:ent-001"
    )
    stale_primary_publication_id = graph_retrieval_eval_uuid(
        "publication", "publication-stale-primary"
    )
    logical_by_uuid[str(stale_primary_publication_id)] = "publication-stale-primary"
    return SeededGraphRetrievalEval(
        dataset=dataset,
        uuid_by_logical=uuid_by_logical,
        logical_by_uuid=logical_by_uuid,
        library_id_by_key=library_id_by_key,
        library_slug_by_key={
            key: library_by_key[key].slug for key in library_id_by_key
        },
        ontology_id_by_key=ontology_id_by_key,
        publication_id_by_scope=publication_id_by_scope,
        stale_primary_publication_id=stale_primary_publication_id,
        primary_partial_item_id=primary_partial_item_id,
    )


def _scope_for_publication(publication_key: str) -> str:
    if "secondary" in publication_key:
        return "secondary"
    if publication_key.endswith("-v2"):
        return "v2"
    return "primary"


async def _apply_case_scenario(
    Session: async_sessionmaker[AsyncSession],
    seeded: SeededGraphRetrievalEval,
    case: GraphRetrievalEvalCase,
) -> None:
    primary_id = seeded.publication_id_by_scope["primary"]
    async with Session() as db:
        await db.execute(
            update(GraphPublication)
            .where(GraphPublication.id == primary_id)
            .values(status="active")
        )
        await db.execute(
            update(GraphPublicationItem)
            .where(GraphPublicationItem.id == seeded.primary_partial_item_id)
            .values(status="active")
        )
        if case.scenario_publication_key == "pub-degraded-primary":
            await db.execute(
                update(GraphPublication)
                .where(GraphPublication.id == primary_id)
                .values(status="degraded")
            )
        elif case.scenario_publication_key == "pub-superseded-primary":
            await db.execute(
                update(GraphPublication)
                .where(GraphPublication.id == primary_id)
                .values(status="superseded")
            )
        elif case.scenario_publication_key == "pub-partial-primary":
            await db.execute(
                update(GraphPublicationItem)
                .where(GraphPublicationItem.id == seeded.primary_partial_item_id)
                .values(status="degraded")
            )
        await db.commit()


def _case_request_body(
    seeded: SeededGraphRetrievalEval,
    case: GraphRetrievalEvalCase,
) -> dict[str, Any]:
    scope = _scope_for_publication(case.scenario_publication_key)
    if case.scenario_publication_key == "pub-changed-primary":
        expected_publication_id = seeded.stale_primary_publication_id
    elif case.request.expected_publication:
        expected_publication_id = seeded.publication_id_by_scope[scope]
    else:
        expected_publication_id = None
    seeds = []
    for seed in case.request.seeds:
        if seed.entity_key is not None:
            seeds.append({"entity_id": str(seeded.uuid_by_logical[seed.entity_key])})
        else:
            value: dict[str, Any] = {"canonical_name": seed.canonical_name}
            if seed.entity_type_key is not None:
                value["entity_type_key"] = seed.entity_type_key
            seeds.append(value)
    return {
        "ontology_version_id": str(
            seeded.ontology_id_by_key[case.request.ontology_key]
        ),
        "expected_publication_id": (
            str(expected_publication_id) if expected_publication_id is not None else None
        ),
        "seeds": seeds,
        "direction": case.request.direction,
        "relation_type_keys": list(case.request.relation_type_keys),
        "max_hops": case.request.max_hops,
        "max_nodes": case.request.max_nodes,
        "max_relations": case.request.max_relations,
        "include_evidence_locators": case.request.include_evidence_locators,
    }


def _response_property_leaks(value: Any, canaries: Sequence[str]) -> int:
    lowered_canaries = tuple(item.casefold() for item in canaries if item)
    leaks = 0

    def visit(item: Any) -> None:
        nonlocal leaks
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).casefold() in {"properties", "include_properties"}:
                    leaks += 1
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            lowered = item.casefold()
            leaks += sum(marker in lowered for marker in lowered_canaries)

    visit(value)
    return leaks


def _logical(value: str, mapping: Mapping[str, str]) -> str:
    logical = mapping.get(value.lower()) or mapping.get(value)
    if logical is None:
        raise ValueError("response contains an unknown runtime UUID")
    return logical


def _score_case_response(
    case: GraphRetrievalEvalCase,
    body: Mapping[str, Any],
    *,
    status_code: int,
    logical_by_uuid: Mapping[str, str],
    deterministic_equal: int,
    privacy_total: int,
    property_leaks: int,
) -> tuple[GraphRetrievalCaseScore, bool, str | None]:
    expected = case.expected
    exact = status_code == expected.status_code
    stable_error_code: str | None = None
    seed_correct = 0
    seed_total = 0
    expected_nodes = 0
    returned_expected_nodes = 0
    expected_relations = 0
    returned_expected_relations = 0
    returned_relations = 0
    expected_evidence_pairs = 0
    returned_expected_evidence_pairs = 0

    if status_code == 200:
        try:
            actual_seeds = tuple(
                _logical(row["entity_id"], logical_by_uuid)
                for row in body.get("seed_matches", [])
            )
            actual_nodes = {
                _logical(row["id"], logical_by_uuid) for row in body.get("nodes", [])
            }
            relation_rows = body.get("relations", [])
            actual_relations = {
                _logical(row["id"], logical_by_uuid) for row in relation_rows
            }
            actual_evidence = {
                _logical(row["id"], logical_by_uuid): {
                    _logical(evidence["evidence_id"], logical_by_uuid)
                    for evidence in row.get("evidence", [])
                }
                for row in relation_rows
            }
        except (KeyError, TypeError, ValueError):
            actual_seeds = ()
            actual_nodes = set()
            actual_relations = set()
            actual_evidence = {}
            exact = False
        expected_node_set = set(expected.node_keys)
        expected_relation_set = set(expected.relation_keys)
        seed_total = len(expected.seed_entity_keys)
        seed_correct = seed_total if actual_seeds == expected.seed_entity_keys else 0
        expected_nodes = len(expected_node_set)
        returned_expected_nodes = len(actual_nodes & expected_node_set)
        expected_relations = len(expected_relation_set)
        returned_expected_relations = len(actual_relations & expected_relation_set)
        returned_relations = len(actual_relations)
        expected_pairs = {
            (relation_key, evidence_key)
            for relation_key, evidence_keys in expected.relation_evidence.items()
            for evidence_key in evidence_keys
        }
        actual_pairs = {
            (relation_key, evidence_key)
            for relation_key, evidence_keys in actual_evidence.items()
            for evidence_key in evidence_keys
        }
        expected_evidence_pairs = len(expected_pairs)
        returned_expected_evidence_pairs = len(expected_pairs & actual_pairs)
        exact = exact and actual_nodes == expected_node_set
        exact = exact and actual_relations == expected_relation_set
        exact = exact and actual_pairs == expected_pairs
        exact = exact and body.get("truncated") == expected.truncated.model_dump(mode="json")
    else:
        stable_error_code = str(body.get("detail", "unknown_error"))
        actual_candidates = {
            _logical(row["entity_id"], logical_by_uuid)
            for row in body.get("candidates", [])
            if isinstance(row, dict) and isinstance(row.get("entity_id"), str)
        }
        exact = exact and body.get("detail") == expected.detail
        exact = exact and actual_candidates == set(expected.candidate_entity_keys)
        if "ambiguity-or-not-found" in case.categories:
            seed_total = 1
            seed_correct = int(exact)

    scope_total = int("scope-state-negative" in case.categories)
    score = GraphRetrievalCaseScore(
        seed_correct=seed_correct,
        seed_total=seed_total,
        expected_nodes=expected_nodes,
        returned_expected_nodes=returned_expected_nodes,
        expected_relations=expected_relations,
        returned_expected_relations=returned_expected_relations,
        returned_relations=returned_relations,
        expected_evidence_pairs=expected_evidence_pairs,
        returned_expected_evidence_pairs=returned_expected_evidence_pairs,
        deterministic_equal=deterministic_equal,
        deterministic_total=2,
        scope_safe=int(exact) if scope_total else 0,
        scope_total=scope_total,
        privacy_clean=max(0, privacy_total - property_leaks),
        privacy_total=privacy_total,
        property_leak_count=property_leaks,
        publication_membership_failures=0,
    )
    return score, exact and deterministic_equal == 2 and property_leaks == 0, stable_error_code


async def run_graph_retrieval_correctness(
    Session: async_sessionmaker[AsyncSession],
    seeded: SeededGraphRetrievalEval,
) -> GraphRetrievalCorrectnessResult:
    canaries = tuple(
        marker
        for row in seeded.dataset.gold.privacy_canaries
        for marker in (row.key_marker, row.value_marker)
    )

    async def override_db():
        async with Session() as db:
            yield db

    async def override_user():
        return User(
            id=graph_retrieval_eval_uuid("user", "correctness-runner"),
            email="v06-m5-eval@example.invalid",
            is_superuser=True,
            is_active=True,
        )

    original_overrides = dict(app.dependency_overrides)
    original_enabled = settings.graph_retrieval_enabled
    original_timeout = settings.graph_retrieval_timeout_seconds
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_active_user] = override_user
    settings.graph_retrieval_enabled = True
    settings.graph_retrieval_timeout_seconds = 3.0
    scores: list[GraphRetrievalCaseScore] = []
    response_hashes: list[GraphRetrievalCaseResponseHash] = []
    error_counts: Counter[str] = Counter()
    passed_cases = 0
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            for case in seeded.dataset.cases:
                await _apply_case_scenario(Session, seeded, case)
                publication = next(
                    row
                    for row in seeded.dataset.gold.scenario_publications
                    if row.publication_key == case.scenario_publication_key
                )
                path = (
                    f"/libraries/{seeded.library_slug_by_key[publication.library_key]}"
                    "/v06/graph/query"
                )
                request_body = _case_request_body(seeded, case)
                repeats = [
                    await client.post(path, json=request_body)
                    for _repeat in range(3)
                ]
                bodies = [response.json() for response in repeats]
                hashes = [
                    canonical_graph_retrieval_response_hash(
                        status_code=response.status_code,
                        body=body,
                        logical_id_by_uuid=seeded.logical_by_uuid,
                    )
                    for response, body in zip(repeats, bodies, strict=True)
                ]
                deterministic_equal = sum(value == hashes[0] for value in hashes[1:])
                property_leaks = sum(
                    _response_property_leaks(body, canaries) for body in bodies
                )
                privacy_total = len(canaries) * len(bodies)
                score, passed, error_code = _score_case_response(
                    case,
                    bodies[0],
                    status_code=repeats[0].status_code,
                    logical_by_uuid=seeded.logical_by_uuid,
                    deterministic_equal=deterministic_equal,
                    privacy_total=privacy_total,
                    property_leaks=property_leaks,
                )
                scores.append(score)
                passed_cases += int(passed)
                if error_code is not None:
                    error_counts[error_code] += 1
                response_hashes.append(
                    GraphRetrievalCaseResponseHash(
                        case_id=case.case_id,
                        status_code=repeats[0].status_code,
                        canonical_sha256=hashes[0],
                    )
                )
    finally:
        settings.graph_retrieval_enabled = original_enabled
        settings.graph_retrieval_timeout_seconds = original_timeout
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        primary_id = seeded.publication_id_by_scope["primary"]
        async with Session() as db:
            await db.execute(
                update(GraphPublication)
                .where(GraphPublication.id == primary_id)
                .values(status="active")
            )
            await db.execute(
                update(GraphPublicationItem)
                .where(GraphPublicationItem.id == seeded.primary_partial_item_id)
                .values(status="active")
            )
            await db.commit()

    response_set_hash = canonical_json_sha256(
        [row.model_dump(mode="json") for row in response_hashes]
    )
    return GraphRetrievalCorrectnessResult(
        scores=tuple(scores),
        response_hashes=tuple(response_hashes),
        canonical_response_set_sha256=response_set_hash,
        passed_cases=passed_cases,
        failed_cases=len(seeded.dataset.cases) - passed_cases,
        stable_error_code_counts=dict(sorted(error_counts.items())),
    )


async def seed_graph_retrieval_performance_fixture(
    Session: async_sessionmaker[AsyncSession],
    fixture_config,
) -> SeededGraphRetrievalPerformance:
    if (
        fixture_config.entities != 6000
        or fixture_config.relations != 4000
        or fixture_config.high_degree_relations < 250
        or fixture_config.two_hop_nodes < 101
    ):
        raise ValueError("performance fixture does not match the frozen M5 scale")

    library_id = graph_retrieval_eval_uuid("perf-library", "release-v1")
    ontology_id = graph_retrieval_eval_uuid("perf-ontology", "release-v1")
    entity_type_id = graph_retrieval_eval_uuid("perf-entity-type", "node")
    relation_type_id = graph_retrieval_eval_uuid("perf-relation-type", "connected")
    publication_id = graph_retrieval_eval_uuid("perf-publication", "release-v1")
    evidence_id = graph_retrieval_eval_uuid("perf-evidence", "selected-facts")
    document_id = graph_retrieval_eval_uuid("perf-document", "selected-facts")
    revision_id = graph_retrieval_eval_uuid("perf-revision", "selected-facts")
    block_id = graph_retrieval_eval_uuid("perf-block", "selected-facts")

    def entity_id(index: int) -> uuid.UUID:
        return graph_retrieval_eval_uuid("perf-entity", f"perf-ent-{index:04d}")

    def relation_id(index: int) -> uuid.UUID:
        return graph_retrieval_eval_uuid("perf-relation", f"perf-rel-{index:04d}")

    relation_rows: list[dict[str, Any]] = []
    endpoints: list[tuple[int, int]] = []
    for index in range(1, fixture_config.relations + 1):
        if index <= fixture_config.high_degree_relations:
            source_index = 1
            target_index = index + 1
        elif index <= fixture_config.high_degree_relations + fixture_config.two_hop_nodes:
            offset = index - fixture_config.high_degree_relations - 1
            source_index = 2 + offset
            target_index = 2 + fixture_config.high_degree_relations + offset
        else:
            offset = index - (
                fixture_config.high_degree_relations + fixture_config.two_hop_nodes + 1
            )
            source_index = 1000 + offset
            target_index = source_index + 1
        if target_index > fixture_config.entities:
            source_index = 1000 + (offset % 3500)
            target_index = source_index + 1
        endpoints.append((source_index, target_index))
        relation_rows.append(
            {
                "id": relation_id(index),
                "library_id": library_id,
                "ontology_version_id": ontology_id,
                "relation_type_id": relation_type_id,
                "source_entity_id": entity_id(source_index),
                "target_entity_id": entity_id(target_index),
                "properties": {},
                "status": "active",
                "review_status": "approved",
                "source_type": "manual",
                "confidence": None,
            }
        )

    measured_relation_indexes = {
        fixture_config.high_degree_relations + fixture_config.two_hop_nodes + 1,
        fixture_config.high_degree_relations + fixture_config.two_hop_nodes + 2,
    }
    evidence_text = str(evidence_id)
    logical_by_uuid = {
        str(library_id): "perf-library",
        str(ontology_id): "perf-ontology",
        str(entity_type_id): "perf-entity-type",
        str(relation_type_id): "perf-relation-type",
        str(publication_id): "perf-publication",
        str(evidence_id): "perf-evidence",
        str(document_id): "perf-document",
        str(revision_id): "perf-revision",
        str(block_id): "perf-block",
    }
    for index in range(1, fixture_config.entities + 1):
        logical_by_uuid[str(entity_id(index))] = f"perf-ent-{index:04d}"
    for index in range(1, fixture_config.relations + 1):
        logical_by_uuid[str(relation_id(index))] = f"perf-rel-{index:04d}"

    async with Session() as db:
        await db.execute(
            insert(Library),
            [
                {
                    "id": library_id,
                    "slug": "v06-eval-performance",
                    "name": "v06-eval-performance",
                    "embedding_model": "bge-m3",
                    "embedding_dim": 1024,
                    "qdrant_collection": "v06-eval-performance",
                }
            ],
        )
        await db.execute(
            insert(OntologyVersion),
            [
                {
                    "id": ontology_id,
                    "library_id": library_id,
                    "version_key": "performance-v1",
                    "version_no": 1,
                    "status": "active",
                }
            ],
        )
        await db.execute(
            insert(EntityType),
            [
                {
                    "id": entity_type_id,
                    "library_id": library_id,
                    "ontology_version_id": ontology_id,
                    "key": "perf-node",
                    "label": "Performance Node",
                    "properties_schema": {},
                    "status": "active",
                }
            ],
        )
        await db.execute(
            insert(RelationType),
            [
                {
                    "id": relation_type_id,
                    "library_id": library_id,
                    "ontology_version_id": ontology_id,
                    "key": "perf-connected",
                    "label": "Performance Connected",
                    "direction": "directed",
                    "requires_evidence": False,
                    "default_review_policy": "auto_active",
                    "properties_schema": {},
                    "status": "active",
                }
            ],
        )
        await db.execute(
            insert(Entity),
            [
                {
                    "id": entity_id(index),
                    "library_id": library_id,
                    "ontology_version_id": ontology_id,
                    "entity_type_id": entity_type_id,
                    "canonical_name": f"Performance Node {index:04d}",
                    "normalized_name": f"performance node {index:04d}",
                    "properties": {},
                    "status": "active",
                    "source_type": "manual",
                }
                for index in range(1, fixture_config.entities + 1)
            ],
        )
        await db.execute(insert(KnowledgeRelation), relation_rows)
        await db.execute(
            insert(Document),
            [
                {
                    "id": document_id,
                    "library_id": library_id,
                    "title": "Performance Evidence",
                    "content_hash": _stable_hash("perf-document", "selected-facts"),
                    "current_revision": 1,
                    "current_revision_id": revision_id,
                    "latest_revision_id": revision_id,
                    "status": "ready",
                }
            ],
        )
        await db.execute(
            insert(DocumentRevision),
            [
                {
                    "id": revision_id,
                    "document_id": document_id,
                    "library_id": library_id,
                    "revision_no": 1,
                    "content_hash": _stable_hash("perf-revision", "selected-facts"),
                    "normalized_text": "private performance source",
                    "parser_name": "synthetic",
                    "parser_version": "v1",
                    "chunking_strategy": "fixed",
                    "chunking_strategy_version": "v1",
                    "status": "ready",
                }
            ],
        )
        await db.execute(
            insert(DocumentBlock),
            [
                {
                    "id": block_id,
                    "library_id": library_id,
                    "document_id": document_id,
                    "document_revision_id": revision_id,
                    "seq": 0,
                    "block_kind": "paragraph",
                    "page_start": 1,
                    "page_end": 1,
                    "source_start": 0,
                    "source_end": 10,
                    "text": "private performance block",
                    "content": {"private": "performance"},
                    "parser_name": "synthetic",
                    "parser_version": "v1",
                }
            ],
        )
        await db.execute(
            insert(EvidenceUnit),
            [
                {
                    "id": evidence_id,
                    "library_id": library_id,
                    "document_id": document_id,
                    "document_revision_id": revision_id,
                    "document_block_id": block_id,
                    "evidence_kind": "quote",
                    "page_start": 1,
                    "page_end": 1,
                    "source_start": 0,
                    "source_end": 10,
                    "text_quote": "private performance quote",
                    "status": "active",
                }
            ],
        )
        await db.execute(
            insert(RelationEvidence),
            [
                {
                    "id": graph_retrieval_eval_uuid(
                        "perf-relation-evidence", f"{index}:selected-facts"
                    ),
                    "library_id": library_id,
                    "relation_id": relation_id(index),
                    "evidence_id": evidence_id,
                    "document_id": document_id,
                    "document_revision_id": revision_id,
                    "support_type": "supports",
                    "quote_text": "private performance relation quote",
                    "status": "active",
                }
                for index in sorted(measured_relation_indexes)
            ],
        )
        await db.execute(
            insert(GraphPublication),
            [
                {
                    "id": publication_id,
                    "library_id": library_id,
                    "ontology_version_id": ontology_id,
                    "status": "active",
                    "source_mode": "initial_seed",
                    "manifest_version": "v1",
                    "policy_version": "v1",
                    "policy_snapshot": {},
                    "manifest_hash": _stable_hash("perf-manifest", "release-v1"),
                    "idempotency_key": "perf-release-v1",
                    "include_drafts": False,
                    "plan_options": {},
                    "entity_count": fixture_config.entities,
                    "relation_count": fixture_config.relations,
                    "blocked_counts": {},
                    "blocked_diagnostics": {},
                    "item_hashes_summary": {},
                    "activated_at": GRAPH_RETRIEVAL_EVAL_ACTIVATED_AT,
                }
            ],
        )
        entity_items = [
            {
                "id": graph_retrieval_eval_uuid(
                    "perf-publication-item", f"entity:{index:04d}"
                ),
                "publication_id": publication_id,
                "library_id": library_id,
                "ontology_version_id": ontology_id,
                "item_kind": "entity",
                "entity_id": entity_id(index),
                "relation_id": None,
                "item_hash": _stable_hash("perf-entity-item", f"{index:04d}"),
                "status": "active",
                "support_evidence_ids": [],
                "support_counts": {"total": 0},
                "fact_snapshot": {},
            }
            for index in range(1, fixture_config.entities + 1)
        ]
        relation_items = [
            {
                "id": graph_retrieval_eval_uuid(
                    "perf-publication-item", f"relation:{index:04d}"
                ),
                "publication_id": publication_id,
                "library_id": library_id,
                "ontology_version_id": ontology_id,
                "item_kind": "relation",
                "entity_id": None,
                "relation_id": relation_id(index),
                "item_hash": _stable_hash("perf-relation-item", f"{index:04d}"),
                "status": "active",
                "support_evidence_ids": (
                    [evidence_text] if index in measured_relation_indexes else []
                ),
                "support_counts": {
                    "total": int(index in measured_relation_indexes)
                },
                "fact_snapshot": {},
            }
            for index in range(1, fixture_config.relations + 1)
        ]
        await db.execute(insert(GraphPublicationItem), entity_items)
        await db.execute(insert(GraphPublicationItem), relation_items)
        await db.commit()

        library = await db.get(Library, library_id)
        if library is None:
            raise ValueError("performance Library was not seeded")
        snapshot = await graph_retrieval.load_healthy_graph_snapshot(
            db,
            library,
            ontology_id,
            expected_publication_id=publication_id,
        )
        if snapshot.entity_count + snapshot.relation_count != 10_000:
            raise ValueError("performance publication is not the frozen 10,000-item fixture")

    first_relation_index = min(measured_relation_indexes)
    return SeededGraphRetrievalPerformance(
        library_id=library_id,
        library_slug="v06-eval-performance",
        ontology_id=ontology_id,
        publication_id=publication_id,
        entity_type_id=entity_type_id,
        relation_type_id=relation_type_id,
        evidence_id=evidence_id,
        seed_entity_id=entity_id(1000),
        high_degree_seed_id=entity_id(1),
        first_relation_id=relation_id(first_relation_index),
        first_relation_item_hash=_stable_hash(
            "perf-relation-item", f"{first_relation_index:04d}"
        ),
        logical_by_uuid=logical_by_uuid,
    )


def _performance_requests(
    seeded: SeededGraphRetrievalPerformance,
) -> Mapping[str, GraphRetrievalQueryRequest]:
    base = {
        "ontology_version_id": seeded.ontology_id,
        "expected_publication_id": seeded.publication_id,
        "direction": "outbound",
        "relation_type_keys": ["perf-connected"],
        "max_nodes": 100,
        "max_relations": 200,
    }
    return {
        "one-hop-evidence-off": GraphRetrievalQueryRequest(
            **base,
            seeds=[{"entity_id": seeded.seed_entity_id}],
            max_hops=1,
            include_evidence_locators=False,
        ),
        "one-hop-evidence-on": GraphRetrievalQueryRequest(
            **base,
            seeds=[{"entity_id": seeded.seed_entity_id}],
            max_hops=1,
            include_evidence_locators=True,
        ),
        "two-hop-evidence-off": GraphRetrievalQueryRequest(
            **base,
            seeds=[{"entity_id": seeded.seed_entity_id}],
            max_hops=2,
            include_evidence_locators=False,
        ),
        "two-hop-evidence-on": GraphRetrievalQueryRequest(
            **base,
            seeds=[{"entity_id": seeded.seed_entity_id}],
            max_hops=2,
            include_evidence_locators=True,
        ),
        "high-degree-relation-truncation": GraphRetrievalQueryRequest(
            **{
                **base,
                "seeds": [{"entity_id": seeded.high_degree_seed_id}],
                "max_hops": 1,
                "max_nodes": 100,
                "max_relations": 50,
                "include_evidence_locators": False,
            }
        ),
        "high-degree-node-truncation": GraphRetrievalQueryRequest(
            **{
                **base,
                "seeds": [{"entity_id": seeded.high_degree_seed_id}],
                "max_hops": 1,
                "max_nodes": 10,
                "max_relations": 200,
                "include_evidence_locators": False,
            }
        ),
    }


async def _environment_fingerprint(db: AsyncSession) -> str:
    names = (
        "server_version_num",
        "shared_buffers",
        "work_mem",
        "effective_cache_size",
        "random_page_cost",
        "jit",
    )
    values = {
        name: (await db.execute(text(f"SELECT current_setting('{name}')"))).scalar_one()
        for name in names
    }
    return canonical_json_sha256(
        {
            "database_settings": values,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "python_implementation": platform.python_implementation(),
            "sample_count": GRAPH_RETRIEVAL_EVAL_SAMPLES,
            "timing_protocol": "perf-counter-nearest-rank-v1",
            "warmup_count": GRAPH_RETRIEVAL_EVAL_WARMUPS,
        }
    )


def _normalized_explain(plan: Mapping[str, Any], shape_name: str) -> GraphRetrievalExplainStats:
    scan_rows = 0
    plan_rows = 0
    shared_hit = 0
    shared_read = 0
    temp_read = 0
    temp_written = 0

    def visit(node: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal scan_rows, plan_rows, shared_hit, shared_read, temp_read, temp_written
        node_type = str(node.get("Node Type", "Unknown"))
        actual_rows = max(0, int(node.get("Actual Rows", 0)))
        loops = max(0, int(node.get("Actual Loops", 0)))
        removed = max(0, int(node.get("Rows Removed by Filter", 0)))
        observed = (actual_rows + removed) * loops
        plan_rows += observed
        if "Scan" in node_type:
            scan_rows += observed
        shared_hit += max(0, int(node.get("Shared Hit Blocks", 0)))
        shared_read += max(0, int(node.get("Shared Read Blocks", 0)))
        temp_read += max(0, int(node.get("Temp Read Blocks", 0)))
        temp_written += max(0, int(node.get("Temp Written Blocks", 0)))
        return {
            "node_type": node_type,
            "plans": [visit(child) for child in node.get("Plans", [])],
            "relation_role": shape_name,
        }

    shape = visit(plan)
    return GraphRetrievalExplainStats(
        scan_rows_observed=scan_rows,
        plan_rows_observed=plan_rows,
        shared_hit_blocks=shared_hit,
        shared_read_blocks=shared_read,
        temp_read_blocks=temp_read,
        temp_written_blocks=temp_written,
        plan_shape_sha256=canonical_json_sha256(shape),
    )


async def _explain_statement(
    db: AsyncSession,
    statement,
    shape_name: str,
) -> GraphRetrievalExplainStats:
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    rendered = re.sub(
        r"'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'",
        r"CAST('\1' AS UUID)",
        str(compiled),
    )
    sql = (
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON, TIMING OFF) "
        + rendered
    )
    value = (await db.execute(text(sql))).scalar_one()
    if isinstance(value, str):
        import json

        value = json.loads(value)
    if not isinstance(value, list) or not value or not isinstance(value[0], dict):
        raise ValueError("PostgreSQL returned an invalid EXPLAIN JSON shape")
    plan = value[0].get("Plan")
    if not isinstance(plan, dict):
        raise ValueError("PostgreSQL EXPLAIN JSON is missing Plan")
    return _normalized_explain(plan, shape_name)


async def _collect_explain(
    db: AsyncSession,
    library: Library,
    seeded: SeededGraphRetrievalPerformance,
    config: Settings,
) -> Mapping[str, GraphRetrievalExplainStats]:
    request = _performance_requests(seeded)["one-hop-evidence-on"]
    resolution = await graph_retrieval.resolve_graph_retrieval_query(
        db,
        library,
        request,
        config=config,
    )
    snapshot = resolution.snapshot
    fact = graph_retrieval.SelectedGraphFact(
        output_index=0,
        item_kind="relation",
        fact_id=seeded.first_relation_id,
        expected_item_hash=seeded.first_relation_item_hash,
    )
    statements = {
        "current-publication": graph_retrieval._current_publication_statement(
            library, seeded.ontology_id
        ),
        "membership-items": graph_retrieval._item_aggregate_statement(snapshot),
        "membership-entities": graph_retrieval._entity_aggregate_statement(snapshot),
        "membership-relations": graph_retrieval._relation_aggregate_statement(snapshot),
        "seed-id": graph_retrieval._id_seed_statement(
            ((0, seeded.seed_entity_id),), snapshot
        ),
        "traversal": graph_retrieval._traversal_statement(
            snapshot,
            (seeded.seed_entity_id,),
            (),
            resolution.relation_types,
            "outbound",
            200,
        ),
        "fact-support": graph_retrieval._fact_support_statement(
            snapshot,
            (fact,),
            max_evidence_per_fact=20,
        ),
        "evidence-locator": graph_retrieval._evidence_locator_statement(
            library,
            (seeded.evidence_id,),
        ),
    }
    output = {}
    for name, statement in statements.items():
        output[name] = await _explain_statement(db, statement, name)
    return output


async def run_graph_retrieval_performance(
    engine: AsyncEngine,
    Session: async_sessionmaker[AsyncSession],
    seeded: SeededGraphRetrievalPerformance,
) -> GraphRetrievalPerformanceResult:
    config = Settings(_env_file=None)
    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    async with Session() as db:
        library = await db.get(Library, seeded.library_id)
        if library is None:
            raise ValueError("performance Library is missing")
        environment_hash = await _environment_fingerprint(db)

    event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
    latency: dict[str, GraphRetrievalLatencyStats] = {}
    try:
        for name, request in _performance_requests(seeded).items():
            expected_hash: str | None = None
            samples: list[int] = []
            total_runs = GRAPH_RETRIEVAL_EVAL_WARMUPS + GRAPH_RETRIEVAL_EVAL_SAMPLES
            for run_index in range(total_runs):
                statements.clear()
                async with Session() as db:
                    started = time.perf_counter_ns()
                    response = await graph_retrieval.execute_graph_retrieval_query(
                        db,
                        library,
                        request,
                        config=config,
                    )
                    duration = time.perf_counter_ns() - started
                cap = 12 if request.include_evidence_locators else 10
                if len(statements) > cap:
                    raise ValueError("graph retrieval performance statement budget exceeded")
                response_hash = canonical_graph_retrieval_response_hash(
                    status_code=200,
                    body=response.model_dump(mode="json"),
                    logical_id_by_uuid=seeded.logical_by_uuid,
                )
                if expected_hash is None:
                    expected_hash = response_hash
                elif response_hash != expected_hash:
                    raise ValueError("performance response hash changed between samples")
                if run_index >= GRAPH_RETRIEVAL_EVAL_WARMUPS:
                    samples.append(duration)
            stats = nearest_rank_latency(samples)
            if stats.sample_count != GRAPH_RETRIEVAL_EVAL_SAMPLES:
                raise ValueError("performance scenario has an incomplete sample population")
            if stats.p95_us > GRAPH_RETRIEVAL_EVAL_HARD_P95_US:
                raise ValueError("performance scenario exceeds the M5 hard p95 ceiling")
            latency[name] = stats
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)

    async with Session() as db:
        library = await db.get(Library, seeded.library_id)
        if library is None:
            raise ValueError("performance Library is missing for EXPLAIN")
        explain = await _collect_explain(db, library, seeded, config)
    return GraphRetrievalPerformanceResult(
        latency=latency,
        explain=explain,
        environment_fingerprint_sha256=environment_hash,
    )


async def run_graph_retrieval_timeout_and_compatibility(
    Session: async_sessionmaker[AsyncSession],
    seeded: SeededGraphRetrievalEval,
) -> tuple[bool, bool]:
    case = seeded.dataset.cases[35]
    body = _case_request_body(seeded, case)
    path = (
        f"/libraries/{seeded.library_slug_by_key['lib-primary']}"
        "/v06/graph/query"
    )

    async def override_db():
        async with Session() as db:
            yield db

    async def override_user():
        return User(
            id=graph_retrieval_eval_uuid("user", "timeout-runner"),
            email="v06-m5-timeout@example.invalid",
            is_superuser=True,
            is_active=True,
        )

    original_overrides = dict(app.dependency_overrides)
    original_enabled = settings.graph_retrieval_enabled
    original_timeout = settings.graph_retrieval_timeout_seconds
    original_execute = graph_retrieval.execute_graph_retrieval_query
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_active_user] = override_user
    timeout_reused = False
    compatibility_passed = False
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            settings.graph_retrieval_enabled = False
            disabled = await client.post(path, json=body)
            active_path = (
                f"/libraries/{seeded.library_slug_by_key['lib-primary']}"
                "/v05/graph-publications/active"
            )
            active = await client.get(
                active_path,
                params={
                    "ontology_version_id": str(
                        seeded.ontology_id_by_key["ontology-primary-v1"]
                    )
                },
            )
            compatibility_passed = (
                disabled.status_code == 503
                and disabled.json().get("detail") == "graph_retrieval_disabled"
                and active.status_code == 200
            )

            async def delayed_query(db, *_args, **_kwargs):
                await db.execute(text("SELECT pg_sleep(1)"))

            graph_retrieval.execute_graph_retrieval_query = delayed_query
            settings.graph_retrieval_enabled = True
            settings.graph_retrieval_timeout_seconds = 0.05
            timeout_response = await client.post(path, json=body)

            reuse_probe: list[int] = []

            async def probe_query(db, *_args, **_kwargs):
                reuse_probe.append((await db.execute(text("SELECT 1"))).scalar_one())
                raise graph_retrieval.GraphRetrievalServiceError("seed_not_found")

            graph_retrieval.execute_graph_retrieval_query = probe_query
            probe_response = await client.post(path, json=body)
            timeout_reused = (
                timeout_response.status_code == 504
                and timeout_response.json().get("detail") == "graph_retrieval_timeout"
                and probe_response.status_code == 404
                and reuse_probe == [1]
            )
    finally:
        graph_retrieval.execute_graph_retrieval_query = original_execute
        settings.graph_retrieval_enabled = original_enabled
        settings.graph_retrieval_timeout_seconds = original_timeout
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
    return timeout_reused, compatibility_passed


async def _query_state_counts(Session: async_sessionmaker[AsyncSession]) -> tuple[int, ...]:
    async with Session() as db:
        counts = []
        for model in (
            AuditLog,
            Entity,
            KnowledgeRelation,
            GraphPublication,
            GraphPublicationItem,
            EntityMention,
            RelationEvidence,
        ):
            value = (
                await db.execute(select(func.count()).select_from(model))
            ).scalar_one()
            counts.append(int(value))
        return tuple(counts)


async def collect_graph_retrieval_eval_run(
    dsn: str,
    dataset: LoadedGraphRetrievalEvalDataset,
) -> GraphRetrievalEvalRunMaterial:
    engine = create_async_engine(dsn, pool_size=8, max_overflow=4, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            head = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        if head != "0023":
            raise ValueError("graph retrieval Eval database must be at Alembic 0023")
        seeded = await seed_graph_retrieval_eval_dataset(Session, dataset)
        before_correctness = await _query_state_counts(Session)
        correctness = await run_graph_retrieval_correctness(Session, seeded)
        after_correctness = await _query_state_counts(Session)
        timeout_reused, compatibility_passed = (
            await run_graph_retrieval_timeout_and_compatibility(Session, seeded)
        )
        after_smoke = await _query_state_counts(Session)
        performance_seed = await seed_graph_retrieval_performance_fixture(
            Session,
            dataset.manifest.performance_fixture,
        )
        before_performance = await _query_state_counts(Session)
        performance = await run_graph_retrieval_performance(
            engine,
            Session,
            performance_seed,
        )
        after_performance = await _query_state_counts(Session)
        state_unchanged = (
            before_correctness == after_correctness == after_smoke
            and before_performance == after_performance
        )
        return GraphRetrievalEvalRunMaterial(
            correctness=correctness,
            performance=performance,
            query_state_unchanged=state_unchanged,
            timeout_rollback_reused=timeout_reused,
            disabled_compatibility_passed=compatibility_passed,
        )
    finally:
        await engine.dispose()


def graph_retrieval_calibration_thresholds(
    performance: GraphRetrievalPerformanceResult,
) -> GraphRetrievalEvalThresholds:
    max_p95_us = {}
    for name, stats in performance.latency.items():
        if stats.p95_us > GRAPH_RETRIEVAL_EVAL_HARD_P95_US:
            raise ValueError("calibration p95 exceeds the fixed M5 hard ceiling")
        max_p95_us[name] = min(
            GRAPH_RETRIEVAL_EVAL_HARD_P95_US,
            max(math.ceil(stats.p95_us * 1.25), stats.p95_us + 5_000),
        )
    max_scan_rows = {
        name: max(1, math.ceil(stats.scan_rows_observed * 1.10))
        for name, stats in performance.explain.items()
    }
    return GraphRetrievalEvalThresholds(
        seed_exact_accuracy=1.0,
        node_recall=1.0,
        relation_recall=1.0,
        relation_precision=1.0,
        evidence_coverage=1.0,
        determinism=1.0,
        scope_safety=1.0,
        property_privacy=1.0,
        max_property_leaks=0,
        max_membership_failures=0,
        max_p95_us=max_p95_us,
        max_scan_rows=max_scan_rows,
    )


def build_graph_retrieval_calibration_artifact(
    *,
    run_id: str,
    database_id: str,
    started_at: datetime,
    finished_at: datetime,
    code_commit: str,
    implementation_tree_sha256: str,
    dataset: LoadedGraphRetrievalEvalDataset,
    material: GraphRetrievalEvalRunMaterial,
    database_cleanup_succeeded: bool,
) -> GraphRetrievalEvalResultArtifact:
    metrics = build_graph_retrieval_metric_report(material.correctness.scores)
    passed = (
        material.correctness.failed_cases == 0
        and metrics.passes_hard_gates()
        and material.query_state_unchanged
        and material.timeout_rollback_reused
        and material.disabled_compatibility_passed
        and database_cleanup_succeeded
    )
    return GraphRetrievalEvalResultArtifact(
        schema_version="graph-retrieval-eval-result-v1",
        run_id=run_id,
        phase="calibration",
        status="passed" if passed else "failed",
        started_at=started_at,
        finished_at=finished_at,
        code_commit=code_commit,
        implementation_tree_sha256=implementation_tree_sha256,
        alembic_head="0023",
        database_id=database_id,
        environment_fingerprint_sha256=(
            material.performance.environment_fingerprint_sha256
        ),
        dataset_id="release-v1",
        dataset_counts=dataset.manifest.counts,
        dataset_manifest_sha256=dataset.dataset_manifest_sha256,
        dataset_content_sha256=dataset.dataset_content_sha256,
        evaluation_config_sha256=dataset.evaluation_config_sha256,
        contract_version="v1",
        normalization_version="normalize_graph_name_v1",
        response_canonicalization_version="graph-retrieval-response-canonical-v1",
        metrics=metrics,
        performance=dict(material.performance.latency),
        explain=dict(material.performance.explain),
        candidate_thresholds=graph_retrieval_calibration_thresholds(
            material.performance
        ),
        case_counts={
            "passed": material.correctness.passed_cases,
            "failed": material.correctness.failed_cases,
        },
        stable_error_code_counts=dict(material.correctness.stable_error_code_counts),
        case_response_hashes=material.correctness.response_hashes,
        canonical_response_set_sha256=(
            material.correctness.canonical_response_set_sha256
        ),
        policy_id=None,
        policy_canonical_sha256=None,
        policy_file_sha256=None,
        database_cleanup_succeeded=database_cleanup_succeeded,
    )


def build_graph_retrieval_post_freeze_artifact(
    *,
    run_id: str,
    database_id: str,
    started_at: datetime,
    finished_at: datetime,
    code_commit: str,
    implementation_tree_sha256: str,
    dataset: LoadedGraphRetrievalEvalDataset,
    material: GraphRetrievalEvalRunMaterial,
    loaded_policy: LoadedGraphRetrievalEvalPolicy,
    database_cleanup_succeeded: bool,
) -> GraphRetrievalEvalResultArtifact:
    policy = loaded_policy.policy
    metrics = build_graph_retrieval_metric_report(material.correctness.scores)
    latency_passed = set(material.performance.latency) == set(policy.thresholds.max_p95_us)
    latency_passed = latency_passed and all(
        stats.p95_us <= policy.thresholds.max_p95_us[name]
        for name, stats in material.performance.latency.items()
    )
    explain_passed = set(material.performance.explain) == set(
        policy.thresholds.max_scan_rows
    )
    explain_passed = explain_passed and all(
        stats.scan_rows_observed <= policy.thresholds.max_scan_rows[name]
        for name, stats in material.performance.explain.items()
    )
    hashes_match = (
        implementation_tree_sha256 == policy.implementation_tree_sha256
        and dataset.dataset_manifest_sha256 == policy.dataset_manifest_sha256
        and dataset.dataset_content_sha256 == policy.dataset_content_sha256
        and dataset.evaluation_config_sha256 == policy.evaluation_config_sha256
        and material.performance.environment_fingerprint_sha256
        == policy.environment_fingerprint_sha256
    )
    passed = (
        material.correctness.failed_cases == 0
        and metrics.passes_hard_gates()
        and material.query_state_unchanged
        and material.timeout_rollback_reused
        and material.disabled_compatibility_passed
        and latency_passed
        and explain_passed
        and hashes_match
        and database_cleanup_succeeded
    )
    return GraphRetrievalEvalResultArtifact(
        schema_version="graph-retrieval-eval-result-v1",
        run_id=run_id,
        phase="post-freeze",
        status="passed" if passed else "failed",
        started_at=started_at,
        finished_at=finished_at,
        code_commit=code_commit,
        implementation_tree_sha256=implementation_tree_sha256,
        alembic_head="0023",
        database_id=database_id,
        environment_fingerprint_sha256=(
            material.performance.environment_fingerprint_sha256
        ),
        dataset_id="release-v1",
        dataset_counts=dataset.manifest.counts,
        dataset_manifest_sha256=dataset.dataset_manifest_sha256,
        dataset_content_sha256=dataset.dataset_content_sha256,
        evaluation_config_sha256=dataset.evaluation_config_sha256,
        contract_version="v1",
        normalization_version="normalize_graph_name_v1",
        response_canonicalization_version="graph-retrieval-response-canonical-v1",
        metrics=metrics,
        performance=dict(material.performance.latency),
        explain=dict(material.performance.explain),
        candidate_thresholds=None,
        case_counts={
            "passed": material.correctness.passed_cases,
            "failed": material.correctness.failed_cases,
        },
        stable_error_code_counts=dict(material.correctness.stable_error_code_counts),
        case_response_hashes=material.correctness.response_hashes,
        canonical_response_set_sha256=(
            material.correctness.canonical_response_set_sha256
        ),
        policy_id=policy.policy_id,
        policy_canonical_sha256=loaded_policy.policy_canonical_sha256,
        policy_file_sha256=loaded_policy.policy_file_sha256,
        database_cleanup_succeeded=database_cleanup_succeeded,
    )
