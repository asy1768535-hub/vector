from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import aliased

from app.config import settings
from app.models.attribute_definition import AttributeDefinition
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.entity import Entity
from app.models.entity_mention import EntityMention
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.services.graph_extraction_eval import (
    GraphEvalAttemptPerformance,
    GraphEvalAttemptMetric,
    GraphEvalEntityPrediction,
    GraphEvalMetricReport,
    GraphEvalPolicy,
    GraphEvalRelationPrediction,
    GraphEvalRunArtifact,
    LoadedGraphEvalDataset,
    LoadedGraphEvalPolicy,
    assert_sanitized_eval_artifact,
    build_metric_report,
    build_performance_report,
    canonical_graph_eval_hash,
    gold_entity_keys,
    gold_relation_keys,
    load_graph_eval_policy,
    prediction_entity_keys,
    prediction_relation_keys,
)
from app.services.graph_extraction_jobs import (
    GraphExtractionJobError,
    create_graph_extraction_job,
    retry_graph_extraction_job,
)
from app.services.graph_extraction_provider import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL_NAME,
)
from app.services.graph_extraction_worker import (
    claim_graph_extraction_unit,
    process_graph_extraction_unit,
)
from app.services.graph_seed import (
    DEFAULT_ATTRIBUTE_DEFINITIONS,
    DEFAULT_ENTITY_TYPES,
    DEFAULT_RELATION_TYPES,
    expanded_default_relation_constraints,
)


EVAL_UUID_NAMESPACE = uuid.UUID("ea5d1f6f-72c0-4d3e-91e4-3a6c9e3c6b60")
DATABASE_NAME_RE = re.compile(r"^vkt_m6_eval_[a-z0-9_]+$")
_ALLOWED_UNTRACKED = {
    "_drop_tables.py",
    "_drop_test_db.py",
    "_drop_test_db2.py",
    "_drop_test_sync.py",
    "_verify_baseline.py",
    "docs/codex-handoff.md",
}


ProviderFactory = Callable[[GraphExtractionUnit], Any]


@dataclass(frozen=True, slots=True)
class EvalSeedResult:
    library_id: uuid.UUID
    document_ids_by_key: dict[str, uuid.UUID]
    revision_ids_by_key: dict[str, uuid.UUID]


@dataclass(frozen=True, slots=True)
class EvalRuntimeResult:
    artifact: GraphEvalRunArtifact
    output_path: Path


def eval_batch_size() -> int:
    raw = os.environ.get("GRAPH_EXTRACTION_EVAL_BATCH_SIZE", "").strip()
    if raw in {"", "1"}:
        return 1
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("Eval batch size must be an integer between 2 and 8") from exc
    if not 2 <= value <= 8:
        raise ValueError("Eval batch size must be between 2 and 8")
    return value


def eval_uuid(*parts: str) -> uuid.UUID:
    if not parts or any(not isinstance(part, str) or not part for part in parts):
        raise ValueError("deterministic Eval UUID parts must be non-empty strings")
    return uuid.uuid5(EVAL_UUID_NAMESPACE, "/".join(parts))


def validate_eval_database_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 63
        or not DATABASE_NAME_RE.fullmatch(value)
    ):
        raise ValueError("Eval database name must match ^vkt_m6_eval_[a-z0-9_]+$")
    return value


def validate_real_run_environment(
    *,
    loaded: LoadedGraphEvalDataset,
    database_name: str,
    confirmation: str,
    workers: int,
    phase: str,
) -> str:
    validate_eval_database_name(database_name)
    if confirmation != loaded.manifest.dataset_id:
        raise ValueError("external-send confirmation must equal the synthetic dataset ID")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    if phase not in {"development-smoke", "calibration", "post-freeze"}:
        raise ValueError("unsupported real Eval phase")
    if phase == "development-smoke" and loaded.manifest.dataset_id != "development-smoke-v1":
        raise ValueError("Development Smoke requires the frozen smoke manifest")
    if phase != "development-smoke" and loaded.manifest.dataset_id != "release-v1":
        raise ValueError("Release phases require the frozen Release manifest")
    admin_dsn = os.environ.get("VECTOR_KB_M6_EVAL_ADMIN_DSN", "").strip()
    process_key = os.environ.get("GRAPH_EXTRACTION_API_KEY", "").strip()
    if not admin_dsn:
        raise ValueError("VECTOR_KB_M6_EVAL_ADMIN_DSN is required in the process environment")
    if not process_key:
        raise ValueError("GRAPH_EXTRACTION_API_KEY is required in the process environment")
    if process_key != settings.graph_extraction_api_key.get_secret_value().strip():
        raise ValueError("process Provider key does not match the accepted runtime Secret")
    if not settings.graph_extraction_enabled or settings.graph_extraction_auto_trigger_enabled:
        raise ValueError("real Eval requires extraction enabled and auto trigger disabled")
    if settings.graph_extraction_model != DEEPSEEK_MODEL_NAME:
        raise ValueError("real Eval requires the frozen DeepSeek model")
    if settings.graph_extraction_base_url != DEEPSEEK_BASE_URL:
        raise ValueError("real Eval requires the frozen official DeepSeek base URL")
    return admin_dsn


def resolve_eval_policy(
    *,
    repository_root: Path,
    loaded: LoadedGraphEvalDataset,
    phase: str,
    policy_path: Path | None,
) -> LoadedGraphEvalPolicy | None:
    if phase != "post-freeze":
        if policy_path is not None:
            raise ValueError("Eval Policy is allowed only for post-freeze runs")
        return None
    if policy_path is None:
        raise ValueError("post-freeze runs require --policy")

    policy = load_graph_eval_policy(
        repository_root=repository_root,
        policy_path=policy_path,
    )
    if loaded.dataset_manifest_sha256 != policy.policy.dataset_manifest_sha256:
        raise ValueError("post-freeze dataset manifest hash does not match Eval Policy")
    if loaded.dataset_content_sha256 != policy.policy.dataset_content_sha256:
        raise ValueError("post-freeze dataset content hash does not match Eval Policy")
    return policy


def require_release_worktree_clean(repository_root: Path) -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("cannot resolve a full Git commit")
    for args in (["git", "diff", "--quiet"], ["git", "diff", "--cached", "--quiet"]):
        result = subprocess.run(args, cwd=repository_root, check=False)
        if result.returncode != 0:
            raise ValueError("selected Eval runs require no tracked or staged changes")
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    unexpected = sorted(set(untracked) - _ALLOWED_UNTRACKED)
    if unexpected:
        raise ValueError(f"selected Eval runs have unexpected untracked files: {unexpected}")
    return commit


def _database_url(admin_dsn: str, database_name: str) -> URL:
    return make_url(admin_dsn).set(
        drivername="postgresql+asyncpg",
        database=validate_eval_database_name(database_name),
    )


async def _connect(url: URL):
    import asyncpg

    return await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database or "postgres",
    )


async def create_clean_eval_database(admin_dsn: str, database_name: str) -> URL:
    name = validate_eval_database_name(database_name)
    admin_url = make_url(admin_dsn).set(drivername="postgresql+asyncpg")
    connection = await _connect(admin_url)
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", name
        )
        if exists:
            raise ValueError("Eval database already exists; use a fresh database name")
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()
    return _database_url(admin_dsn, name)


async def drop_eval_database(admin_dsn: str, database_name: str) -> None:
    name = validate_eval_database_name(database_name)
    admin_url = make_url(admin_dsn).set(drivername="postgresql+asyncpg")
    connection = await _connect(admin_url)
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await connection.close()


def upgrade_eval_database(database_url: URL, *, repository_root: Path) -> None:
    original = (
        settings.db_host,
        settings.db_port,
        settings.db_user,
        settings.db_password,
        settings.db_name,
    )
    try:
        settings.db_host = database_url.host or "localhost"
        settings.db_port = int(database_url.port or 5432)
        settings.db_user = database_url.username or ""
        settings.db_password = database_url.password or ""
        settings.db_name = database_url.database or ""
        config = Config(str(repository_root / "alembic.ini"))
        command.upgrade(config, "head")
    finally:
        (
            settings.db_host,
            settings.db_port,
            settings.db_user,
            settings.db_password,
            settings.db_name,
        ) = original


async def seed_eval_dataset(
    session_factory,
    *,
    loaded: LoadedGraphEvalDataset,
) -> EvalSeedResult:
    dataset_id = loaded.manifest.dataset_id
    library_id = eval_uuid(dataset_id, "library")
    ontology_id = eval_uuid(dataset_id, "ontology", "enterprise-v1")
    now = datetime.now(timezone.utc)
    document_ids: dict[str, uuid.UUID] = {}
    revision_ids: dict[str, uuid.UUID] = {}
    async with session_factory() as db, db.begin():
        library = Library(
            id=library_id,
            slug=f"m6-{dataset_id}",
            name=f"M6 synthetic Eval {dataset_id}",
            embedding_model="bge-m3",
            embedding_dim=1024,
            vector_distance="cosine",
            chunk_size=1000,
            chunk_overlap=120,
            retrieval_mode="dense",
            qdrant_collection=f"m6_{dataset_id.replace('-', '_')}",
            graph_extraction_enabled=True,
            external_llm_enabled=True,
            graph_extraction_allowed_security_levels=["internal"],
        )
        db.add(library)
        await db.flush()
        ontology = OntologyVersion(
            id=ontology_id,
            library_id=library_id,
            version_key="enterprise",
            version_no=1,
            status="active",
            description="Deterministic M6 enterprise ontology",
            published_at=now,
        )
        db.add(ontology)
        await db.flush()
        entity_types: dict[str, EntityType] = {}
        for spec in DEFAULT_ENTITY_TYPES:
            row = EntityType(
                id=eval_uuid(dataset_id, "entity-type", spec.key),
                library_id=library_id,
                ontology_version_id=ontology_id,
                key=spec.key,
                label=spec.label,
                description=spec.description,
                properties_schema=spec.properties_schema,
                is_seeded=True,
                status="active",
            )
            entity_types[spec.key] = row
            db.add(row)
        relation_types: dict[str, RelationType] = {}
        for spec in DEFAULT_RELATION_TYPES:
            row = RelationType(
                id=eval_uuid(dataset_id, "relation-type", spec.key),
                library_id=library_id,
                ontology_version_id=ontology_id,
                key=spec.key,
                label=spec.label,
                description=spec.description,
                direction=spec.direction,
                requires_evidence=spec.requires_evidence,
                default_review_policy=spec.default_review_policy,
                properties_schema=spec.properties_schema,
                is_seeded=True,
                status="active",
            )
            relation_types[spec.key] = row
            db.add(row)
        await db.flush()
        for spec in expanded_default_relation_constraints():
            db.add(
                RelationTypeConstraint(
                    id=eval_uuid(
                        dataset_id,
                        "constraint",
                        spec.relation_type_key,
                        spec.source_entity_type_key,
                        spec.target_entity_type_key,
                    ),
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    relation_type_id=relation_types[spec.relation_type_key].id,
                    source_entity_type_id=entity_types[spec.source_entity_type_key].id,
                    target_entity_type_id=entity_types[spec.target_entity_type_key].id,
                    cardinality=spec.cardinality,
                    requires_review=spec.requires_review,
                    status="active",
                )
            )
        for spec in DEFAULT_ATTRIBUTE_DEFINITIONS:
            owner = entity_types[spec.owner_type_key]
            db.add(
                AttributeDefinition(
                    id=eval_uuid(
                        dataset_id,
                        "attribute",
                        spec.owner_kind,
                        spec.owner_type_key,
                        spec.key,
                    ),
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    owner_kind=spec.owner_kind,
                    owner_type_id=owner.id,
                    key=spec.key,
                    label=spec.label,
                    value_type=spec.value_type,
                    required=spec.required,
                    enum_values=spec.enum_values,
                    validation_schema=spec.validation_schema,
                    indexed=spec.indexed,
                    status="active",
                )
            )
        for document_spec in loaded.documents:
            document_id = eval_uuid(dataset_id, "document", document_spec.document_key)
            revision_id = eval_uuid(dataset_id, "revision", document_spec.document_key, "1")
            document_ids[document_spec.document_key] = document_id
            revision_ids[document_spec.document_key] = revision_id
            normalized_text = "\n".join(unit.text for unit in document_spec.units)
            content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
            document = Document(
                id=document_id,
                library_id=library_id,
                title=document_spec.title,
                doc_metadata={},
                content_hash=content_hash,
                current_revision=1,
                security_level="internal",
                status="ready",
            )
            revision = DocumentRevision(
                id=revision_id,
                document_id=document_id,
                library_id=library_id,
                revision_no=1,
                title=document_spec.title,
                document_metadata={},
                content_hash=content_hash,
                normalized_text=normalized_text,
                parser_name="m6-synthetic",
                parser_version="m6-synthetic-v1",
                chunking_strategy="one-unit-one-chunk",
                chunking_strategy_version="m6-unit-v1",
                security_level="internal",
                status="ready",
                published_at=now,
                finished_at=now,
            )
            db.add(document)
            await db.flush()
            db.add(revision)
            await db.flush()
            document.current_revision_id = revision_id
            document.latest_revision_id = revision_id
            for ordinal, unit in enumerate(document_spec.units):
                evidence_id = eval_uuid(
                    dataset_id, "evidence", document_spec.document_key, unit.unit_key
                )
                chunk_id = eval_uuid(
                    dataset_id, "chunk", document_spec.document_key, unit.unit_key
                )
                quote_hash = hashlib.sha256(unit.text.encode("utf-8")).hexdigest()
                evidence = EvidenceUnit(
                    id=evidence_id,
                    library_id=library_id,
                    document_id=document_id,
                    document_revision_id=revision_id,
                    evidence_kind="chunk",
                    source_start=0,
                    source_end=len(unit.text),
                    title_path=[document_spec.title],
                    text_quote=unit.text,
                    text_quote_hash=quote_hash,
                    evidence_metadata={"synthetic": True, "unit_key": unit.unit_key},
                    security_level="internal",
                    status="active",
                )
                chunk = Chunk(
                    id=chunk_id,
                    document_id=document_id,
                    library_id=library_id,
                    document_revision_id=revision_id,
                    evidence_id=evidence_id,
                    seq=ordinal,
                    chunk_kind="text",
                    text=unit.text,
                    token_count=len(unit.text),
                    title_path=[document_spec.title],
                    source_start=0,
                    source_end=len(unit.text),
                    chunk_metadata={"synthetic": True, "unit_key": unit.unit_key},
                )
                db.add_all([evidence, chunk])
    return EvalSeedResult(library_id, document_ids, revision_ids)


async def create_eval_jobs(
    session_factory,
    *,
    loaded: LoadedGraphEvalDataset,
    seed: EvalSeedResult,
) -> tuple[uuid.UUID, ...]:
    job_ids: list[uuid.UUID] = []
    async with session_factory() as db, db.begin():
        library = await db.get(Library, seed.library_id)
        assert library is not None
        for document_spec in loaded.documents:
            document = await db.get(Document, seed.document_ids_by_key[document_spec.document_key])
            revision = await db.get(
                DocumentRevision,
                seed.revision_ids_by_key[document_spec.document_key],
            )
            assert document is not None and revision is not None
            job = await create_graph_extraction_job(
                db,
                library=library,
                document=document,
                revision=revision,
                trigger_type="eval",
                execution_mode="eval",
                requested_by=None,
                idempotency_key=(
                    f"eval:{loaded.manifest.dataset_id}:{document_spec.document_key}"
                ),
            )
            job_ids.append(job.id)
    return tuple(job_ids)


async def _drain_one_worker(
    session_factory,
    *,
    worker_index: int,
    provider_factory: ProviderFactory | None,
) -> Counter[str]:
    outcomes: Counter[str] = Counter()
    while True:
        async with session_factory() as db:
            unit = await claim_graph_extraction_unit(
                db,
                worker_id=f"m6-eval-{worker_index}",
                lease_seconds=settings.graph_extraction_unit_lease_seconds,
                max_attempts=settings.graph_extraction_worker_max_model_attempts,
            )
        if unit is None:
            return outcomes
        if unit.claim_token is None:
            outcomes["lost_lease"] += 1
            continue
        provider = provider_factory(unit) if provider_factory is not None else None
        result = await process_graph_extraction_unit(
            session_factory,
            unit_id=unit.id,
            claim_token=unit.claim_token,
            provider=provider,
        )
        outcomes[result.outcome] += 1


async def _retry_failed_eval_jobs(session_factory, *, library_id: uuid.UUID) -> int:
    async with session_factory() as db:
        result = await db.execute(
            select(GraphExtractionJob.id).where(
                GraphExtractionJob.library_id == library_id,
                GraphExtractionJob.status.in_(("failed", "partially_succeeded")),
            )
        )
        job_ids = list(result.scalars().all())
    retried = 0
    for job_id in job_ids:
        try:
            async with session_factory() as db, db.begin():
                library = await db.get(Library, library_id)
                assert library is not None
                await retry_graph_extraction_job(
                    db,
                    library=library,
                    job_id=job_id,
                    max_attempts=settings.graph_extraction_worker_max_model_attempts,
                )
            retried += 1
        except GraphExtractionJobError as exc:
            if exc.code != "no_retryable_units":
                raise
    return retried


async def run_eval_workers(
    session_factory,
    *,
    library_id: uuid.UUID,
    workers: int,
    provider_factory: ProviderFactory | None = None,
) -> Counter[str]:
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    outcomes: Counter[str] = Counter()
    for _wave in range(settings.graph_extraction_worker_max_model_attempts):
        rows = await asyncio.gather(
            *(
                _drain_one_worker(
                    session_factory,
                    worker_index=index,
                    provider_factory=provider_factory,
                )
                for index in range(workers)
            )
        )
        for row in rows:
            outcomes.update(row)
        if not await _retry_failed_eval_jobs(
            session_factory, library_id=library_id
        ):
            break
    return outcomes


async def run_batched_eval_workers(
    session_factory,
    *,
    library_id: uuid.UUID,
    workers: int,
    batch_size: int,
) -> Counter[str]:
    from app.services.graph_extraction_batch_eval import (
        drain_eval_graph_extraction_batches,
    )

    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    if not 1 <= batch_size <= 8:
        raise ValueError("batch_size must be between 1 and 8")
    outcomes: Counter[str] = Counter()
    for _wave in range(settings.graph_extraction_worker_max_model_attempts):
        rows = await asyncio.gather(
            *(
                drain_eval_graph_extraction_batches(
                    session_factory,
                    worker_index=index,
                    batch_size=batch_size,
                )
                for index in range(workers)
            )
        )
        for row in rows:
            outcomes.update(row)
        if not await _retry_failed_eval_jobs(
            session_factory, library_id=library_id
        ):
            break
    return outcomes


def _counter(rows: list[str | None], *, none_key: str = "none") -> dict[str, int]:
    return dict(sorted(Counter(row or none_key for row in rows).items()))


def _evaluation_config_payload(job: GraphExtractionJob) -> dict[str, Any]:
    return {
        "model_config_hash": job.model_config_hash,
        "policy_config_hash": job.policy_config_hash,
        "ontology_snapshot_hash": job.ontology_snapshot_hash,
        "prompt_content_hash": job.prompt_content_hash,
        "prompt_version": job.prompt_version,
        "extractor_version": job.extractor_version,
        "output_parser_version": job.output_parser_version,
        "context_policy_version": job.context_policy_version,
        "extraction_policy_version": job.extraction_policy_version,
        "normalization_rule_version": job.normalization_rule_version,
        "confidence_policy_version": job.confidence_policy_version,
        "document_parser_version": job.document_parser_version,
        "chunking_strategy_version": job.chunking_strategy_version,
    }


def determine_eval_run_status(
    *,
    phase: str,
    job_statuses: list[str],
    unit_statuses: list[str],
    metrics: GraphEvalMetricReport,
    real_provider: bool,
    real_model_call_count: int,
    evaluation_config_hash: str,
    policy: GraphEvalPolicy | None,
) -> str:
    passed = (
        set(job_statuses) == {"succeeded"}
        and set(unit_statuses) == {"succeeded"}
        and not metrics.cross_revision_evidence_count
        and not metrics.eval_formal_write_count
    )
    if phase != "post-freeze":
        if policy is not None:
            raise ValueError("Eval Policy is allowed only for post-freeze runs")
        return "passed" if passed else "failed"
    if policy is None:
        raise ValueError("post-freeze verdict requires a frozen Eval Policy")

    thresholds = policy.thresholds
    rate_gates = (
        (metrics.json_parse_rate.value, 0.99),
        (metrics.schema_valid_rate.value, 0.95),
        (metrics.entity.precision.value, thresholds.entity_precision),
        (metrics.entity.recall.value, thresholds.entity_recall),
        (metrics.relation.precision.value, thresholds.relation_precision),
        (metrics.relation.recall.value, thresholds.relation_recall),
    )
    passed = (
        passed
        and real_provider
        and real_model_call_count >= 100
        and evaluation_config_hash == policy.evaluation_config_hash
        and all(value is not None and value >= floor for value, floor in rate_gates)
    )
    return "passed" if passed else "failed"


async def collect_eval_artifact(
    session_factory,
    *,
    loaded: LoadedGraphEvalDataset,
    seed: EvalSeedResult,
    job_ids: tuple[uuid.UUID, ...],
    run_id: str,
    phase: str,
    real_provider: bool,
    database_name: str,
    code_commit: str,
    started_at: datetime,
    finished_at: datetime,
    policy: GraphEvalPolicy | None = None,
    policy_sha256: str | None = None,
) -> GraphEvalRunArtifact:
    if (policy is None) != (policy_sha256 is None):
        raise ValueError("Eval Policy and its SHA-256 must appear together")
    source_entity = aliased(GraphEntityCandidate)
    target_entity = aliased(GraphEntityCandidate)
    async with session_factory() as db:
        jobs = list(
            (
                await db.execute(
                    select(GraphExtractionJob)
                    .where(GraphExtractionJob.id.in_(job_ids))
                    .order_by(GraphExtractionJob.id)
                )
            )
            .scalars()
            .all()
        )
        units = list(
            (
                await db.execute(
                    select(GraphExtractionUnit).where(
                        GraphExtractionUnit.job_id.in_(job_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        attempts = list(
            (
                await db.execute(
                    select(ExtractionRawOutputAttempt)
                    .join(GraphExtractionUnit)
                    .where(GraphExtractionUnit.job_id.in_(job_ids))
                )
            )
            .scalars()
            .all()
        )
        entity_rows = list(
            (
                await db.execute(
                    select(GraphEntityCandidate).where(
                        GraphEntityCandidate.job_id.in_(job_ids),
                        GraphEntityCandidate.purged_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        relation_rows = (
            await db.execute(
                select(
                    GraphRelationCandidate,
                    source_entity,
                    target_entity,
                    RelationType.direction,
                )
                .join(
                    source_entity,
                    source_entity.id == GraphRelationCandidate.source_candidate_id,
                )
                .join(
                    target_entity,
                    target_entity.id == GraphRelationCandidate.target_candidate_id,
                )
                .join(
                    RelationType,
                    (RelationType.ontology_version_id == GraphRelationCandidate.ontology_version_id)
                    & (RelationType.key == GraphRelationCandidate.relation_type_key),
                )
                .where(
                    GraphRelationCandidate.job_id.in_(job_ids),
                    GraphRelationCandidate.purged_at.is_(None),
                )
            )
        ).all()
        entity_evidence = list(
            (
                await db.execute(
                    select(GraphEntityCandidateEvidence).where(
                        GraphEntityCandidateEvidence.job_id.in_(job_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        relation_evidence = list(
            (
                await db.execute(
                    select(GraphRelationCandidateEvidence).where(
                        GraphRelationCandidateEvidence.job_id.in_(job_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        formal_counts = []
        for model in (Entity, EntityMention, KnowledgeRelation, RelationEvidence):
            formal_counts.append(
                int(
                    await db.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.created_by_job_id.in_(job_ids))
                    )
                    or 0
                )
            )
    if len(jobs) != len(job_ids):
        raise ValueError("Eval artifact scope is missing Jobs")
    config_values = {
        canonical_graph_eval_hash(_evaluation_config_payload(job)) for job in jobs
    }
    if len(config_values) != 1:
        raise ValueError("Eval Jobs do not share one evaluation config hash")
    evaluation_config_hash = next(iter(config_values))
    document_key_by_id = {
        document_id: key for key, document_id in seed.document_ids_by_key.items()
    }
    predicted_entities = tuple(
        GraphEvalEntityPrediction(
            document_key=document_key_by_id[
                next(job.document_id for job in jobs if job.id == row.job_id)
            ],
            entity_type_key=row.entity_type_key,
            canonical_name=row.canonical_name or "",
        )
        for row in entity_rows
    )
    predicted_relations = tuple(
        GraphEvalRelationPrediction(
            document_key=document_key_by_id[
                next(job.document_id for job in jobs if job.id == relation.job_id)
            ],
            source_entity_type_key=source.entity_type_key,
            source_canonical_name=source.canonical_name or "",
            relation_type_key=relation.relation_type_key,
            target_entity_type_key=target.entity_type_key,
            target_canonical_name=target.canonical_name or "",
            directed=direction == "directed",
        )
        for relation, source, target, direction in relation_rows
    )
    all_evidence = entity_evidence + relation_evidence
    revision_by_job = {job.id: job.document_revision_id for job in jobs}
    cross_revision = sum(
        row.resolved_document_revision_id is not None
        and row.resolved_document_revision_id != revision_by_job[row.job_id]
        for row in all_evidence
    )
    metrics: GraphEvalMetricReport = build_metric_report(
        gold_entities=gold_entity_keys(loaded.documents),
        predicted_entities=prediction_entity_keys(predicted_entities),
        gold_relations=gold_relation_keys(
            loaded.documents,
            undirected_relation_types=frozenset({"related_to"}),
        ),
        predicted_relations=prediction_relation_keys(predicted_relations),
        attempts=tuple(
            GraphEvalAttemptMetric(row.request_status, row.parse_status)
            for row in attempts
        ),
        relation_schema_statuses=tuple(
            relation.ontology_validation_status
            for relation, _source, _target, _direction in relation_rows
        ),
        evidence_statuses=tuple(row.validation_status for row in all_evidence),
        cross_revision_evidence_count=cross_revision,
        eval_formal_write_count=sum(formal_counts),
    )
    request_ids = sorted(
        row.provider_request_id for row in attempts if row.provider_request_id
    )
    request_digest = (
        canonical_graph_eval_hash(request_ids) if request_ids else None
    )
    job_errors = [job.error_code for job in jobs if job.error_code]
    unit_errors = [unit.error_code for unit in units if unit.error_code]
    first_job = jobs[0]
    status = determine_eval_run_status(
        phase=phase,
        job_statuses=[job.status for job in jobs],
        unit_statuses=[unit.status for unit in units],
        metrics=metrics,
        real_provider=real_provider,
        real_model_call_count=len(attempts) if real_provider else 0,
        evaluation_config_hash=evaluation_config_hash,
        policy=policy,
    )
    artifact = GraphEvalRunArtifact(
        schema_version="graph-extraction-eval-result-v1",
        run_id=run_id,
        phase=phase,
        status=status,
        real_provider=real_provider,
        started_at=started_at,
        finished_at=finished_at,
        code_commit=code_commit,
        alembic_head="0022",
        database_name=database_name,
        dataset_id=loaded.manifest.dataset_id,
        dataset_counts=loaded.counts,
        dataset_manifest_sha256=loaded.dataset_manifest_sha256,
        dataset_content_sha256=loaded.dataset_content_sha256,
        evaluation_config_hash=evaluation_config_hash,
        model_provider=first_job.model_provider,
        model_name=first_job.model_name,
        component_versions={
            key: str(value)
            for key, value in _evaluation_config_payload(first_job).items()
            if key.endswith("_version")
        },
        job_ids=tuple(job.id for job in jobs),
        job_status_counts=_counter([job.status for job in jobs]),
        unit_status_counts=_counter([unit.status for unit in units]),
        attempt_status_counts=_counter([row.request_status for row in attempts]),
        model_attempt_count=len(attempts),
        real_model_call_count=len(attempts) if real_provider else 0,
        provider_request_id_count=len(request_ids),
        provider_request_id_sha256=request_digest,
        metrics=metrics,
        performance=build_performance_report(
            started_at=started_at,
            finished_at=finished_at,
            unit_count=len(units),
            succeeded_unit_count=sum(row.status == "succeeded" for row in units),
            attempted_unit_count=len({row.extraction_unit_id for row in attempts}),
            attempts=tuple(
                GraphEvalAttemptPerformance(
                    latency_ms=row.latency_ms,
                    input_token_count=row.input_token_count,
                    output_token_count=row.output_token_count,
                    finish_reason=row.finish_reason,
                )
                for row in attempts
            ),
        ),
        stable_error_code_counts=_counter(job_errors + unit_errors),
        policy_id=policy.policy_id if policy is not None else None,
        policy_sha256=policy_sha256,
    )
    assert_sanitized_eval_artifact(artifact.model_dump(mode="json"))
    return artifact


def write_eval_artifact(
    *,
    repository_root: Path,
    output_path: Path,
    artifact: GraphEvalRunArtifact,
) -> Path:
    root = repository_root.resolve()
    output = output_path.resolve()
    results_root = (root / "eval/graph_extraction/results").resolve()
    if results_root not in output.parents or output.parent != results_root:
        raise ValueError("Eval result must be directly under eval/graph_extraction/results")
    if output.name != f"{artifact.run_id}.json":
        raise ValueError("Eval result filename must match run_id")
    if output.exists():
        raise FileExistsError("Eval result artifacts are immutable and cannot be overwritten")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    if temporary.exists():
        raise FileExistsError("Eval result temporary path already exists")
    payload = json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
    ) + "\n"
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(temporary, output)
    return output


async def execute_eval_run(
    *,
    repository_root: Path,
    loaded: LoadedGraphEvalDataset,
    database_name: str,
    run_id: str,
    phase: str,
    workers: int,
    confirmation: str,
    output_path: Path,
    policy_path: Path | None = None,
) -> EvalRuntimeResult:
    policy = resolve_eval_policy(
        repository_root=repository_root,
        loaded=loaded,
        phase=phase,
        policy_path=policy_path,
    )
    admin_dsn = validate_real_run_environment(
        loaded=loaded,
        database_name=database_name,
        confirmation=confirmation,
        workers=workers,
        phase=phase,
    )
    code_commit = require_release_worktree_clean(repository_root)
    batch_size = eval_batch_size()
    started_at = datetime.now(timezone.utc)
    database_url = await create_clean_eval_database(admin_dsn, database_name)
    upgrade_eval_database(database_url, repository_root=repository_root)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        seed = await seed_eval_dataset(session_factory, loaded=loaded)
        job_ids = await create_eval_jobs(session_factory, loaded=loaded, seed=seed)
        worker_error: Exception | None = None
        try:
            if batch_size == 1:
                await run_eval_workers(
                    session_factory,
                    library_id=seed.library_id,
                    workers=workers,
                )
            else:
                await run_batched_eval_workers(
                    session_factory,
                    library_id=seed.library_id,
                    workers=workers,
                    batch_size=batch_size,
                )
        except Exception as exc:  # Preserve a failed artifact when database state is readable.
            worker_error = exc
        artifact = await collect_eval_artifact(
            session_factory,
            loaded=loaded,
            seed=seed,
            job_ids=job_ids,
            run_id=run_id,
            phase=phase,
            real_provider=True,
            database_name=database_name,
            code_commit=code_commit,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            policy=policy.policy if policy is not None else None,
            policy_sha256=policy.policy_sha256 if policy is not None else None,
        )
        if batch_size > 1:
            from app.services.graph_extraction_batch_eval import (
                BATCH_PROMPT_VERSION,
                SCHEMA_ROUTER_VERSION,
            )

            components = {
                **artifact.component_versions,
                "batch_prompt_version": BATCH_PROMPT_VERSION,
                "schema_router_version": SCHEMA_ROUTER_VERSION,
                "eval_batch_size": str(batch_size),
            }
            artifact = artifact.model_copy(
                update={
                    "component_versions": components,
                    "evaluation_config_hash": canonical_graph_eval_hash(
                        {
                            "job_config_hash": artifact.evaluation_config_hash,
                            "batch_prompt_version": BATCH_PROMPT_VERSION,
                            "schema_router_version": SCHEMA_ROUTER_VERSION,
                            "batch_size": batch_size,
                        }
                    ),
                }
            )
            assert_sanitized_eval_artifact(artifact.model_dump(mode="json"))
        path = write_eval_artifact(
            repository_root=repository_root,
            output_path=output_path,
            artifact=artifact,
        )
        if worker_error is not None:
            raise RuntimeError(
                f"Eval workers failed; sanitized artifact written to {path.name}"
            ) from worker_error
        return EvalRuntimeResult(artifact, path)
    finally:
        await engine.dispose()
