from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.models.canonical_mapping import GraphClaimMapping, GraphMappingAuthoritySnapshot
from app.models.chunk import Chunk
from app.models.claim_decision import GraphClaimDecision
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_candidates import GraphEntityCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.organization import DEFAULT_ORGANIZATION_ID, Organization
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.services.canonical_mapping_persistence import (
    CanonicalMappingAuthorityError,
    CanonicalMappingRemapBlockedError,
    _row_from_result,
    create_or_get_mapping_authority_snapshot,
    create_or_get_canonical_mapping,
    deterministic_mapping_authority_id,
    get_canonical_mapping,
    list_canonical_mappings,
)
from app.services.claim_decision_persistence import create_or_get_claim_decision
from app.services.graph_candidate_aggregation import canonical_graph_value_hash_v1
from app.services.raw_claim_persistence import _new_core, _new_occurrence
from app.schemas.canonical_mapping import (
    CanonicalMappingV1,
    FrozenOntologySnapshotV1,
    deterministic_mapping_result_id,
)
from tests.test_canonical_mapping import _build, _input, _ontology, _proposal, _plain_claim


_PG_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a unique disposable local vkt_m3_* PostgreSQL database",
)


def _require_safe_dsn() -> None:
    assert _PG_DSN is not None
    url = make_url(_PG_DSN)
    if url.host not in {"127.0.0.1", "localhost"}:
        pytest.fail("M3 PG acceptance requires a local disposable PostgreSQL host")
    if not url.database or not url.database.startswith("vkt_m3_"):
        pytest.fail("M3 PG acceptance requires a database named vkt_m3_*")


def _configure_alembic_for_dsn(monkeypatch) -> None:
    assert _PG_DSN is not None
    url = make_url(_PG_DSN)
    from app.config import settings

    monkeypatch.setattr(settings, "db_host", url.host or "127.0.0.1")
    monkeypatch.setattr(settings, "db_port", url.port or 5432)
    monkeypatch.setattr(settings, "db_user", url.username or "postgres")
    monkeypatch.setattr(settings, "db_password", url.password or "")
    monkeypatch.setattr(settings, "db_name", url.database or "")


async def _count(db, model) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def _seed(db, input_value):
    claim = input_value.claim
    reference = claim.evidence_refs[0]
    body = "M3 canonical mapping evidence"
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    organization = await db.get(Organization, DEFAULT_ORGANIZATION_ID)
    if organization is None:
        db.add(
            Organization(
                id=DEFAULT_ORGANIZATION_ID,
                slug="m3-canonical-mapping",
                name="M3 canonical mapping",
                deployment_profile="private",
                status="active",
            )
        )
        await db.flush()

    library = Library(
        id=claim.library_id,
        slug="m3-canonical-mapping-" + uuid.uuid4().hex[:8],
        name="M3 canonical mapping",
        embedding_model="fixture",
        embedding_dim=3,
        qdrant_collection="m3-unused",
        lifecycle_mode="managed",
        index_state="ready",
    )
    document = Document(
        id=claim.document_id,
        library_id=claim.library_id,
        title="M3 fixture",
        external_id="m3-fixture",
        content_hash=body_hash,
        current_revision=1,
        status="ready",
    )
    revision = DocumentRevision(
        id=claim.document_revision_id,
        document_id=claim.document_id,
        library_id=claim.library_id,
        revision_no=claim.revision_no,
        title="M3 fixture",
        content_hash=body_hash,
        normalized_text=body,
        parser_name="fixture",
        parser_version="v1",
        chunking_strategy="fixture",
        chunking_strategy_version="v1",
        status="ready",
    )
    ontology = OntologyVersion(
        id=input_value.ontology_version_id,
        library_id=claim.library_id,
        version_key="m3-fixture",
        version_no=1,
        status="active",
        origin="user",
        confirmed=True,
    )
    job = GraphExtractionJob(
        id=claim.job_id,
        library_id=claim.library_id,
        document_id=claim.document_id,
        document_revision_id=claim.document_revision_id,
        ontology_version_id=input_value.ontology_version_id,
        trigger_type="eval",
        execution_mode="eval",
        status="succeeded",
        input_fingerprint="c" * 64,
        idempotency_key="m3-job-" + uuid.uuid4().hex,
        model_provider="fixture",
        model_name="fixture-model",
        prompt_version="v1",
        extractor_version="v1",
        output_parser_version="v1",
        context_policy_version="v1",
        extraction_policy_version="v1",
        normalization_rule_version="v1",
        confidence_policy_version="v1",
        document_parser_version="v1",
        chunking_strategy_version="v1",
        model_config_snapshot={},
        policy_config_snapshot={},
        model_config_hash="d" * 64,
        policy_config_hash="e" * 64,
        ontology_snapshot={
            "frozen_ontology": input_value.frozen_ontology.model_dump(mode="json"),
            "authorization_registry_snapshot": input_value.authorization_registry_snapshot.model_dump(mode="json"),
        },
        ontology_snapshot_hash=input_value.ontology_snapshot_hash,
        prompt_content_hash="f" * 64,
    )
    evidence = EvidenceUnit(
        id=reference.evidence_id,
        library_id=claim.library_id,
        document_id=claim.document_id,
        document_revision_id=claim.document_revision_id,
        evidence_kind="chunk",
        source_start=0,
        source_end=len(body),
        text_quote=body,
        text_quote_hash=body_hash,
        status="active",
    )
    chunk = Chunk(
        id=reference.chunk_id,
        document_id=claim.document_id,
        library_id=claim.library_id,
        document_revision_id=claim.document_revision_id,
        evidence_id=reference.evidence_id,
        seq=0,
        chunk_kind="text",
        text=body,
        token_count=4,
        source_start=0,
        source_end=len(body),
    )
    unit = GraphExtractionUnit(
        id=claim.extraction_unit_id,
        job_id=claim.job_id,
        library_id=claim.library_id,
        document_revision_id=claim.document_revision_id,
        ordinal=0,
        center_chunk_id=reference.chunk_id,
        center_evidence_id=reference.evidence_id,
        unit_fingerprint="1" * 64,
        status="succeeded",
    )
    # These models intentionally do not declare ORM relationships for the
    # fixture scope; flush the parent rows before their FK dependants.
    db.add(library)
    await db.flush()
    db.add_all([document, revision, ontology])
    await db.flush()
    db.add_all([job, evidence])
    await db.flush()
    db.add_all([chunk, unit])
    await db.flush()
    db.add(_new_core(claim))
    db.add(_new_occurrence(claim, claim_id=claim.claim_id))
    await db.commit()
    if input_value.decision is not None:
        await create_or_get_claim_decision(db, input_value.decision)
        await db.commit()


def test_graph_extraction_job_ontology_snapshot_freeze_lifecycle(monkeypatch):
    _require_safe_dsn()
    _configure_alembic_for_dsn(monkeypatch)
    command.upgrade(Config("alembic.ini"), "0059")

    async def run():
        assert _PG_DSN is not None
        engine = create_async_engine(_PG_DSN, pool_size=2, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        library_id = uuid.uuid4()
        alternate_library_id = uuid.uuid4()
        document_id = uuid.uuid4()
        revision_id = uuid.uuid4()
        ontology_id = uuid.uuid4()
        alternate_ontology_id = uuid.uuid4()
        cross_library_ontology_id = uuid.uuid4()
        job_id = uuid.uuid4()
        placeholder = {
            "ontology_version_id": str(ontology_id),
            "schema_state": "ai_discovery_pending",
            "confirmed": False,
            "entity_types": [],
            "relation_types": [],
            "relation_constraints": [],
            "source_hash": "a" * 64,
        }
        draft = {**placeholder, "schema_state": "ai_draft"}
        confirmed = {**draft, "schema_state": "confirmed", "confirmed": True}
        try:
            async with sessions() as db:
                organization = await db.get(Organization, DEFAULT_ORGANIZATION_ID)
                if organization is None:
                    db.add(
                        Organization(
                            id=DEFAULT_ORGANIZATION_ID,
                            slug="m3-freeze-lifecycle",
                            name="M3 freeze lifecycle",
                            deployment_profile="private",
                            status="active",
                        )
                    )
                    await db.flush()
                body_hash = hashlib.sha256(b"M3 freeze lifecycle").hexdigest()
                db.add(
                    Library(
                        id=library_id,
                        slug="m3-freeze-" + uuid.uuid4().hex[:12],
                        name="M3 freeze lifecycle",
                        embedding_model="fixture",
                        embedding_dim=3,
                        qdrant_collection="m3-freeze-" + uuid.uuid4().hex[:8],
                        lifecycle_mode="managed",
                        index_state="ready",
                    )
                )
                db.add(
                    Library(
                        id=alternate_library_id,
                        slug="m3-freeze-alt-" + uuid.uuid4().hex[:12],
                        name="M3 freeze alternate library",
                        embedding_model="fixture",
                        embedding_dim=3,
                        qdrant_collection="m3-freeze-alt-" + uuid.uuid4().hex[:8],
                        lifecycle_mode="managed",
                        index_state="ready",
                    )
                )
                await db.flush()
                db.add(
                    Document(
                        id=document_id,
                        library_id=library_id,
                        title="M3 freeze lifecycle",
                        external_id="m3-freeze-" + uuid.uuid4().hex,
                        content_hash=body_hash,
                        current_revision=1,
                        status="ready",
                    )
                )
                db.add(
                    DocumentRevision(
                        id=revision_id,
                        document_id=document_id,
                        library_id=library_id,
                        revision_no=1,
                        title="M3 freeze lifecycle",
                        content_hash=body_hash,
                        normalized_text="M3 freeze lifecycle",
                        parser_name="fixture",
                        parser_version="v1",
                        chunking_strategy="fixture",
                        chunking_strategy_version="v1",
                        status="ready",
                    )
                )
                db.add(
                    OntologyVersion(
                        id=ontology_id,
                        library_id=library_id,
                        version_key="m3-freeze-" + uuid.uuid4().hex[:8],
                        version_no=1,
                        status="draft",
                        origin="user",
                        confirmed=False,
                    )
                )
                db.add(
                    OntologyVersion(
                        id=alternate_ontology_id,
                        library_id=library_id,
                        version_key="m3-freeze-alternate-" + uuid.uuid4().hex[:8],
                        version_no=2,
                        status="draft",
                        origin="user",
                        confirmed=False,
                    )
                )
                db.add(
                    OntologyVersion(
                        id=cross_library_ontology_id,
                        library_id=alternate_library_id,
                        version_key="m3-freeze-cross-library-" + uuid.uuid4().hex[:8],
                        version_no=1,
                        status="draft",
                        origin="user",
                        confirmed=False,
                    )
                )
                await db.flush()
                db.add(
                    GraphExtractionJob(
                        id=job_id,
                        library_id=library_id,
                        document_id=document_id,
                        document_revision_id=revision_id,
                        ontology_version_id=ontology_id,
                        trigger_type="eval",
                        execution_mode="eval",
                        status="waiting_schema",
                        current_stage="waiting_schema",
                        input_fingerprint="b" * 64,
                        idempotency_key="m3-freeze-" + uuid.uuid4().hex,
                        model_provider="fixture",
                        model_name="fixture-model",
                        prompt_version="v1",
                        extractor_version="v1",
                        output_parser_version="v1",
                        context_policy_version="v1",
                        extraction_policy_version="v1",
                        normalization_rule_version="v1",
                        confidence_policy_version="v1",
                        document_parser_version="v1",
                        chunking_strategy_version="v1",
                        model_config_snapshot={},
                        policy_config_snapshot={},
                        model_config_hash="c" * 64,
                        policy_config_hash="d" * 64,
                        ontology_snapshot=placeholder,
                        ontology_snapshot_hash=canonical_graph_value_hash_v1(placeholder),
                        prompt_content_hash="e" * 64,
                    )
                )
                await db.commit()

                job = await db.get(GraphExtractionJob, job_id)
                assert job is not None
                job.ontology_version_id = alternate_ontology_id
                job.ontology_snapshot = {
                    **placeholder,
                    "ontology_version_id": str(alternate_ontology_id),
                }
                job.ontology_snapshot_hash = canonical_graph_value_hash_v1(job.ontology_snapshot)
                with pytest.raises(DBAPIError):
                    await db.commit()
                await db.rollback()

                job = await db.get(GraphExtractionJob, job_id)
                assert job is not None
                job.ontology_snapshot = draft
                job.ontology_snapshot_hash = canonical_graph_value_hash_v1(draft)
                await db.commit()

                job = await db.get(GraphExtractionJob, job_id)
                assert job is not None
                job.status = "queued"
                job.statistics = {"freeze": "draft"}
                await db.commit()

                job = await db.get(GraphExtractionJob, job_id)
                assert job is not None
                job.ontology_snapshot = confirmed
                job.ontology_snapshot_hash = canonical_graph_value_hash_v1(confirmed)
                await db.commit()

                job = await db.get(GraphExtractionJob, job_id)
                assert job is not None
                job.status = "processing"
                job.statistics = {"freeze": "confirmed"}
                await db.commit()

                job = await db.get(GraphExtractionJob, job_id)
                assert job is not None
                job.library_id = alternate_library_id
                with pytest.raises(DBAPIError):
                    await db.commit()
                await db.rollback()

                job = await db.get(GraphExtractionJob, job_id)
                assert job is not None
                job.library_id = alternate_library_id
                job.ontology_version_id = cross_library_ontology_id
                job.ontology_snapshot = {
                    **confirmed,
                    "library_id": str(alternate_library_id),
                    "ontology_version_id": str(cross_library_ontology_id),
                }
                job.ontology_snapshot_hash = canonical_graph_value_hash_v1(job.ontology_snapshot)
                with pytest.raises(DBAPIError):
                    await db.commit()
                await db.rollback()

                # Reuse one valid persisted row as a direct-SQL INSERT template.
                # The trigger must reject both library/ontology mismatch variants
                # before the row can reach the table.
                for attack_library_id, attack_ontology_id in (
                    (alternate_library_id, ontology_id),
                    (library_id, cross_library_ontology_id),
                ):
                    attack_id = uuid.uuid4()
                    with pytest.raises(DBAPIError):
                        await db.execute(
                            text(
                                """
                                INSERT INTO graph_extraction_jobs
                                SELECT (jsonb_populate_record(
                                    NULL::graph_extraction_jobs,
                                    to_jsonb(base) || jsonb_build_object(
                                        'id', CAST(:attack_id AS uuid),
                                        'library_id', CAST(:attack_library_id AS uuid),
                                        'ontology_version_id', CAST(:attack_ontology_id AS uuid)
                                    )
                                )).*
                                FROM graph_extraction_jobs AS base
                                WHERE base.id = CAST(:base_id AS uuid)
                                """
                            ),
                            {
                                "attack_id": attack_id,
                                "attack_library_id": attack_library_id,
                                "attack_ontology_id": attack_ontology_id,
                                "base_id": job_id,
                            },
                        )
                    await db.rollback()

                for mutation in (
                    {"ontology_snapshot_hash": "f" * 64},
                    {"ontology_snapshot": None},
                    {
                        "ontology_snapshot": {
                            "schema_state": "confirmed",
                            "confirmed": True,
                        },
                        "ontology_snapshot_hash": canonical_graph_value_hash_v1(
                            {"schema_state": "confirmed", "confirmed": True}
                        ),
                    },
                    {
                        "ontology_snapshot": {**confirmed, "entity_types": [{"key": "forged"}]},
                        "ontology_snapshot_hash": canonical_graph_value_hash_v1(
                            {**confirmed, "entity_types": [{"key": "forged"}]}
                        ),
                    },
                ):
                    job = await db.get(GraphExtractionJob, job_id)
                    assert job is not None
                    for field, value in mutation.items():
                        setattr(job, field, value)
                    with pytest.raises(DBAPIError):
                        await db.commit()
                    await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canonical_mapping_pg_new_typed_sql_attack_matrix(monkeypatch):
    _require_safe_dsn()
    _configure_alembic_for_dsn(monkeypatch)
    command.upgrade(Config("alembic.ini"), "0059")

    async def run():
        assert _PG_DSN is not None
        engine = create_async_engine(_PG_DSN, pool_size=2, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        base_ontology = _ontology()
        ontology = FrozenOntologySnapshotV1.from_content(
            ontology_version_id=uuid.uuid4(),
            ontology_contract_version=base_ontology.ontology_contract_version,
            entity_type_keys=base_ontology.entity_type_keys,
            relation_type_keys=base_ontology.relation_type_keys,
            constraints=[item.model_dump(mode="json") for item in base_ontology.constraints],
        )
        scope = {
            "claim_id": uuid.uuid4(),
            "library_id": uuid.uuid4(),
            "document_id": uuid.uuid4(),
            "document_revision_id": uuid.uuid4(),
            "job_id": uuid.uuid4(),
            "extraction_unit_id": uuid.uuid4(),
        }
        reference = _plain_claim().evidence_refs[0].model_dump(mode="python")
        reference.update(
            {
                "evidence_id": uuid.uuid4(),
                "library_id": scope["library_id"],
                "document_id": scope["document_id"],
                "document_revision_id": scope["document_revision_id"],
                "job_id": scope["job_id"],
                "extraction_unit_id": scope["extraction_unit_id"],
                "unit_id": uuid.uuid4(),
                "chunk_id": uuid.uuid4(),
                "block_id": uuid.uuid4(),
            }
        )
        reference["locator"].update(
            {
                "document_id": scope["document_id"],
                "document_revision_id": scope["document_revision_id"],
                "unit_id": reference["unit_id"],
            }
        )
        claim = _plain_claim(
            **scope,
            extraction_occurrence_id=uuid.uuid4(),
            ontology_snapshot_hash=ontology.ontology_snapshot_hash,
            evidence_refs=[reference],
        )
        input_value = _input(
            claim,
            ontology=ontology,
            validated_at=datetime.fromisoformat("2026-08-10T12:00:00+08:00"),
        )
        try:
            async with sessions() as db:
                await _seed(db, input_value)
                authority = await create_or_get_mapping_authority_snapshot(
                    db,
                    input_value.authorization_registry_snapshot,
                    job_id=input_value.job_id,
                    authority_id=deterministic_mapping_authority_id(
                        source_id=input_value.job_id,
                        source_hash=input_value.ontology_snapshot_hash,
                        registry_snapshot_hash=input_value.authorization_registry_snapshot.registry_snapshot_hash or "",
                    ),
                    ontology_version_id=input_value.ontology_version_id,
                    ontology_contract_version=input_value.ontology_contract_version,
                )
                await db.commit()
                valid = _row_from_result(
                    _build(input_value, mapping_version=1),
                    authority=authority,
                )
                db.add(valid)
                await db.commit()

                attacks = (
                    (
                        "created_at-invalid",
                        lambda projection: projection.__setitem__("created_at", "bogus"),
                    ),
                    (
                        "created_at-no-timezone",
                        lambda projection: projection.__setitem__(
                            "created_at", "2026-08-10T12:00:00"
                        ),
                    ),
                    (
                        "created_at-infinite",
                        lambda projection: projection.__setitem__("created_at", "infinity"),
                    ),
                    (
                        "validated_at-invalid",
                        lambda projection: projection["evidence_bindings"][0]["attestation"].__setitem__(
                            "validated_at", "bogus"
                        ),
                    ),
                    (
                        "validated_at-no-timezone",
                        lambda projection: projection["evidence_bindings"][0]["attestation"].__setitem__(
                            "validated_at", "2026-08-10T12:00:00"
                        ),
                    ),
                    (
                        "validated_at-infinite",
                        lambda projection: projection["evidence_bindings"][0]["attestation"].__setitem__(
                            "validated_at", "-infinity"
                        ),
                    ),
                    (
                        "binding-extra-key",
                        lambda projection: projection["evidence_bindings"][0].__setitem__(
                            "unexpected", "forged"
                        ),
                    ),
                    (
                        "attestation-extra-key",
                        lambda projection: projection["evidence_bindings"][0]["attestation"].__setitem__(
                            "unexpected", "forged"
                        ),
                    ),
                    (
                        "unknown-source-provenance",
                        lambda projection: projection["source_provenance"].__setitem__(
                            "source_kind", "forged_kind"
                        ),
                    ),
                    (
                        "unknown-actor-provenance",
                        lambda projection: projection["actor_provenance"].__setitem__(
                            "actor_kind", "forged_kind"
                        ),
                    ),
                    (
                        "source-endpoint-json-null",
                        lambda projection: projection.__setitem__("source_endpoint_resolution", None),
                    ),
                )
                for offset, (_label, mutate) in enumerate(attacks, start=2):
                    row = _row_from_result(
                        _build(input_value, mapping_version=offset),
                        authority=authority,
                    )
                    projection = deepcopy(row.result_projection)
                    mutate(projection)
                    row.result_projection = projection
                    row.evidence_bindings = deepcopy(projection["evidence_bindings"])
                    savepoint = await db.begin_nested()
                    db.add(row)
                    with pytest.raises(DBAPIError):
                        await db.flush()
                    await savepoint.rollback()
        finally:
            await engine.dispose()

    asyncio.run(run())


def _remap_result(
    previous: CanonicalMappingV1,
    *,
    generation: int,
    mapping_version: int,
    remap_changes: dict[str, object] | None = None,
) -> CanonicalMappingV1:
    previous_remap = previous.remap_provenance
    root_id = (
        previous_remap.lineage_root_mapping_result_id
        if previous_remap is not None and previous_remap.lineage_root_mapping_result_id is not None
        else previous.mapping_result_id
    )
    root_fingerprint = (
        previous_remap.lineage_root_mapping_result_fingerprint
        if previous_remap is not None and previous_remap.lineage_root_mapping_result_fingerprint is not None
        else previous.mapping_result_fingerprint
    )
    payload = previous.model_dump(mode="json")
    remap = {
        "remap_generation": generation,
        "remap_version": generation,
        "supersedes_mapping_result_id": str(previous.mapping_result_id),
        "supersedes_mapping_result_fingerprint": previous.mapping_result_fingerprint,
        "supersedes_scope": {
            "library_id": str(previous.library_id),
            "document_id": str(previous.document_id),
            "document_revision_id": str(previous.document_revision_id),
            "revision_no": previous.revision_no,
            "job_id": str(previous.job_id),
            "extraction_unit_id": str(previous.extraction_unit_id),
            "claim_id": str(previous.claim_id),
            "extraction_occurrence_id": str(previous.extraction_occurrence_id),
        },
        "prior_remap_generation": generation - 1,
        "lineage_root_mapping_result_id": str(root_id),
        "lineage_root_mapping_result_fingerprint": root_fingerprint,
        "supersedes_lineage_root_mapping_result_id": str(root_id),
        "supersedes_lineage_root_mapping_result_fingerprint": root_fingerprint,
        "supersedes_source_precedence": previous.source_provenance.source_precedence,
        "supersedes_actor_precedence": previous.actor_provenance.actor_precedence,
        "reason_code": "mapper_refresh",
    }
    remap.update(remap_changes or {})
    payload.update(
        mapping_version=mapping_version,
        created_at=(previous.created_at + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        mapping_attempt_id=None,
        mapping_attempt_fingerprint=None,
        mapping_result_id=None,
        mapping_result_fingerprint=None,
        remap_provenance=remap,
    )
    return CanonicalMappingV1.model_validate(payload)


def test_canonical_mapping_pg_migration_and_append_only_acceptance(monkeypatch):
    _require_safe_dsn()
    _configure_alembic_for_dsn(monkeypatch)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0059")
    command.downgrade(cfg, "0058")
    command.upgrade(cfg, "0059")

    async def run():
        assert _PG_DSN is not None
        engine = create_async_engine(_PG_DSN, pool_size=4, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        input_value = _input(decision=True)
        try:
            async with sessions() as db:
                await _seed(db, input_value)
                before = {
                    "claims": await _count(db, GraphRawClaim),
                    "occurrences": await _count(db, GraphRawClaimOccurrence),
                    "decisions": await _count(db, GraphClaimDecision),
                }
                authority = await create_or_get_mapping_authority_snapshot(
                    db,
                    input_value.authorization_registry_snapshot,
                    job_id=input_value.job_id,
                    authority_id=deterministic_mapping_authority_id(
                        source_id=input_value.job_id,
                        source_hash=input_value.ontology_snapshot_hash,
                        registry_snapshot_hash=input_value.authorization_registry_snapshot.registry_snapshot_hash or "",
                    ),
                    ontology_version_id=input_value.ontology_version_id,
                    ontology_contract_version=input_value.ontology_contract_version,
                )
                with pytest.raises(CanonicalMappingAuthorityError):
                    await create_or_get_mapping_authority_snapshot(
                        db,
                        input_value.authorization_registry_snapshot,
                        job_id=input_value.job_id,
                        authority_id=authority.authority_id,
                        ontology_version_id=input_value.ontology_version_id,
                        ontology_contract_version=input_value.ontology_contract_version,
                        source_hash="0" * 64,
                    )
                with pytest.raises(CanonicalMappingAuthorityError):
                    await create_or_get_mapping_authority_snapshot(
                        db,
                        input_value.authorization_registry_snapshot.model_copy(
                            update={"scope": input_value.authorization_registry_snapshot.scope.model_copy(
                                update={"job_id": uuid.uuid4()}
                            )}
                        ),
                        job_id=input_value.job_id,
                        authority_id=authority.authority_id,
                        ontology_version_id=input_value.ontology_version_id,
                        ontology_contract_version=input_value.ontology_contract_version,
                    )
                first_result = _build(input_value)
                first = await create_or_get_canonical_mapping(
                    db,
                    input_value,
                    first_result,
                    authority_id=authority.authority_id,
                )
                await db.commit()
                assert first.mapping_created
                after = {
                    "claims": await _count(db, GraphRawClaim),
                    "occurrences": await _count(db, GraphRawClaimOccurrence),
                    "decisions": await _count(db, GraphClaimDecision),
                }
                assert after == before

                retry = await create_or_get_canonical_mapping(
                    db,
                    input_value,
                    _build(
                        input_value,
                        created_at=first_result.created_at + timedelta(hours=1),
                    ),
                    authority_id=authority.authority_id,
                )
                await db.commit()
                assert retry.mapping_created is False
                assert retry.mapping.created_at == first.mapping.created_at

                second_result = _build(
                    input_value,
                    mapping_version=2,
                    proposal=_proposal(
                        input_value,
                        endpoint_transform="swap",
                        predicate_transform="inverse",
                    ),
                )
                second = await create_or_get_canonical_mapping(
                    db,
                    input_value,
                    second_result,
                    authority_id=authority.authority_id,
                )
                await db.commit()
                assert second.mapping_created
                remap_one_result = _remap_result(second.mapping, generation=1, mapping_version=3)
                remap_one = await create_or_get_canonical_mapping(
                    db,
                    input_value,
                    remap_one_result,
                    authority_id=authority.authority_id,
                )
                await db.commit()
                assert remap_one.mapping_created
                remap_two_result = _remap_result(remap_one.mapping, generation=2, mapping_version=4)
                remap_two = await create_or_get_canonical_mapping(
                    db,
                    input_value,
                    remap_two_result,
                    authority_id=authority.authority_id,
                )
                await db.commit()
                assert remap_two.mapping_created
                assert len(
                    await list_canonical_mappings(
                        db,
                        library_id=input_value.library_id,
                        document_revision_id=input_value.document_revision_id,
                    )
                ) == 4
                assert await get_canonical_mapping(
                    db,
                    library_id=input_value.library_id,
                    document_revision_id=uuid.uuid4(),
                    mapping_result_id=first.mapping.mapping_result_id,
                ) is None
                fk_rows = (
                    await db.execute(
                        text(
                            "SELECT conname, confdeltype FROM pg_constraint "
                            "WHERE conname IN ("
                            "'fk_graph_claim_mappings_claim',"
                            "'fk_graph_claim_mappings_occurrence',"
                            "'fk_graph_claim_mappings_decision',"
                            "'fk_graph_claim_mappings_authority',"
                            "'fk_graph_claim_mappings_supersedes',"
                            "'fk_graph_claim_mappings_lineage_root')"
                        )
                    )
                ).all()
                assert {
                    name: delete_type.decode() if isinstance(delete_type, bytes) else delete_type
                    for name, delete_type in fk_rows
                } == {
                    "fk_graph_claim_mappings_claim": "r",
                    "fk_graph_claim_mappings_occurrence": "r",
                    "fk_graph_claim_mappings_decision": "r",
                    "fk_graph_claim_mappings_authority": "r",
                    "fk_graph_claim_mappings_supersedes": "r",
                    "fk_graph_claim_mappings_lineage_root": "r",
                }

                fake_predecessor_fingerprint = "b" * 64
                fake_predecessor_id = deterministic_mapping_result_id(fake_predecessor_fingerprint)
                missing_predecessor = _remap_result(
                    second.mapping,
                    generation=1,
                    mapping_version=5,
                    remap_changes={
                        "supersedes_mapping_result_id": str(fake_predecessor_id),
                        "supersedes_mapping_result_fingerprint": fake_predecessor_fingerprint,
                    },
                )
                with pytest.raises(CanonicalMappingRemapBlockedError):
                    await create_or_get_canonical_mapping(
                        db,
                        input_value,
                        missing_predecessor,
                        authority_id=authority.authority_id,
                    )
                await db.rollback()

                wrong_root = _remap_result(
                    second.mapping,
                    generation=1,
                    mapping_version=6,
                    remap_changes={
                        "lineage_root_mapping_result_id": str(uuid.uuid4()),
                        "lineage_root_mapping_result_fingerprint": "c" * 64,
                        "supersedes_lineage_root_mapping_result_id": str(uuid.uuid4()),
                        "supersedes_lineage_root_mapping_result_fingerprint": "d" * 64,
                    },
                )
                with pytest.raises(CanonicalMappingRemapBlockedError):
                    await create_or_get_canonical_mapping(
                        db,
                        input_value,
                        wrong_root,
                        authority_id=authority.authority_id,
                    )
                await db.rollback()

                wrong_precedence = _remap_result(
                    second.mapping,
                    generation=1,
                    mapping_version=7,
                    remap_changes={"supersedes_source_precedence": 2},
                )
                with pytest.raises(CanonicalMappingRemapBlockedError):
                    await create_or_get_canonical_mapping(
                        db,
                        input_value,
                        wrong_precedence,
                        authority_id=authority.authority_id,
                    )
                await db.rollback()

                with pytest.raises(ValueError, match="generation"):
                    _remap_result(
                        remap_one.mapping,
                        generation=2,
                        mapping_version=8,
                        remap_changes={"prior_remap_generation": 0},
                    )

                with pytest.raises(CanonicalMappingAuthorityError):
                    await create_or_get_canonical_mapping(
                        db,
                        input_value,
                        _build(input_value, mapping_version=9),
                        authority_id=uuid.uuid4(),
                    )
                await db.rollback()

                authority_row = await db.get(GraphMappingAuthoritySnapshot, authority.authority_id)
                assert authority_row is not None
                authority_row.source_key = "tampered"
                with pytest.raises(DBAPIError):
                    await db.commit()
                await db.rollback()
                authority_row = await db.get(GraphMappingAuthoritySnapshot, authority.authority_id)
                assert authority_row is not None
                await db.delete(authority_row)
                with pytest.raises(DBAPIError):
                    await db.commit()
                await db.rollback()

                tampered = _row_from_result(
                    _build(input_value, mapping_version=20),
                    authority=authority,
                )
                tampered_projection = dict(tampered.result_projection)
                tampered_claim_snapshot = dict(tampered_projection["claim_snapshot"])
                tampered_claim_snapshot["raw_predicate_sha256"] = "e" * 64
                tampered_projection["claim_snapshot"] = tampered_claim_snapshot
                tampered.result_projection = tampered_projection
                savepoint = await db.begin_nested()
                db.add(tampered)
                with pytest.raises(DBAPIError):
                    await db.flush()
                await savepoint.rollback()

                provenance_tampered = _row_from_result(
                    _build(input_value, mapping_version=23),
                    authority=authority,
                )
                provenance_projection = dict(provenance_tampered.result_projection)
                source_projection = dict(provenance_projection["source_provenance"])
                source_projection["source_hash"] = "f" * 64
                provenance_projection["source_provenance"] = source_projection
                actor_projection = dict(provenance_projection["actor_provenance"])
                actor_projection["actor_key"] = "tampered"
                provenance_projection["actor_provenance"] = actor_projection
                provenance_tampered.result_projection = provenance_projection
                savepoint = await db.begin_nested()
                db.add(provenance_tampered)
                with pytest.raises(DBAPIError):
                    await db.flush()
                await savepoint.rollback()

                job = await db.get(GraphExtractionJob, input_value.job_id)
                assert job is not None
                job.ontology_snapshot_hash = "f" * 64
                with pytest.raises(DBAPIError):
                    await db.flush()
                await db.rollback()

                invalid_matrix = _row_from_result(
                    _build(
                        input_value,
                        outcome="blocked",
                        reason_code="no_explicit_mapping",
                        proposal=None,
                        mapping_confidence=None,
                        semantic_status="blocked",
                        auto_proposal=False,
                        mapping_version=21,
                    ),
                    authority=authority,
                )
                invalid_matrix.mapping_confidence = 0.5
                invalid_matrix_projection = dict(invalid_matrix.result_projection)
                invalid_matrix_projection["mapping_confidence"] = 0.5
                invalid_matrix.result_projection = invalid_matrix_projection
                db.add(invalid_matrix)
                with pytest.raises(DBAPIError):
                    await db.flush()
                await db.rollback()

                ambiguous = _row_from_result(
                    _build(
                        input_value,
                        outcome="ambiguous",
                        reason_code="ambiguous_mapping",
                        proposal=None,
                        mapping_confidence=0.5,
                        semantic_status="ambiguous",
                        auto_proposal=False,
                        mapping_version=24,
                    ),
                    authority=authority,
                )
                savepoint = await db.begin_nested()
                db.add(ambiguous)
                await db.flush()
                await savepoint.rollback()

                endpoint_tampered = _row_from_result(
                    _build(input_value, mapping_version=25),
                    authority=authority,
                )
                endpoint_projection = dict(endpoint_tampered.result_projection)
                endpoint_source = dict(endpoint_projection["canonical_source_endpoint"])
                endpoint_source["role"] = "target"
                endpoint_projection["canonical_source_endpoint"] = endpoint_source
                endpoint_tampered.result_projection = endpoint_projection
                savepoint = await db.begin_nested()
                db.add(endpoint_tampered)
                with pytest.raises(DBAPIError):
                    await db.flush()
                await savepoint.rollback()

                authorization_tampered = _row_from_result(
                    _build(input_value, mapping_version=26),
                    authority=authority,
                )
                authorization_projection = dict(authorization_tampered.result_projection)
                authorization_provenance = dict(authorization_projection["authorization_provenance"])
                authorization_provenance["authorization_key"] = "forged-authority"
                authorization_projection["authorization_provenance"] = authorization_provenance
                authorization_tampered.result_projection = authorization_projection
                savepoint = await db.begin_nested()
                db.add(authorization_tampered)
                with pytest.raises(DBAPIError):
                    await db.flush()
                await savepoint.rollback()

                missing_projection_key = _row_from_result(
                    _build(input_value, mapping_version=22),
                    authority=authority,
                )
                missing_projection = dict(missing_projection_key.result_projection)
                del missing_projection["claim_snapshot"]
                missing_projection_key.result_projection = missing_projection
                db.add(missing_projection_key)
                with pytest.raises(DBAPIError):
                    await db.flush()
                await db.rollback()

                candidate = GraphEntityCandidate(
                    id=uuid.uuid4(),
                    job_id=input_value.job_id,
                    library_id=input_value.library_id,
                    ontology_version_id=input_value.ontology_version_id,
                    entity_type_key="fixture_entity",
                    canonical_name="fixture",
                    normalized_name="fixture",
                    proposed_aliases=[],
                    proposed_properties={},
                    external_mapping_hints={},
                    candidate_key="m3-candidate",
                    status="extracted",
                )
                db.add(candidate)
                await db.flush()
                await db.delete(candidate)
                await db.commit()
                assert await get_canonical_mapping(
                    db,
                    library_id=input_value.library_id,
                    document_revision_id=input_value.document_revision_id,
                    mapping_result_id=first.mapping.mapping_result_id,
                ) is not None

                row = await db.get(GraphClaimMapping, first.mapping.mapping_result_id)
                assert row is not None
                row.reason_code = "tampered"
                with pytest.raises(DBAPIError):
                    await db.commit()
                await db.rollback()
                row = await db.get(GraphClaimMapping, first.mapping.mapping_result_id)
                assert row is not None and row.reason_code is None

                decision_row = await db.get(GraphClaimDecision, input_value.decision_id)
                assert decision_row is not None
                decision_row.producer_key = "tampered"
                with pytest.raises(DBAPIError):
                    await db.commit()
                await db.rollback()
                decision_row = await db.get(GraphClaimDecision, input_value.decision_id)
                assert decision_row is not None and decision_row.producer_key != "tampered"

                for field, value in (
                    ("reason_code", "ambiguous_mapping"),
                    ("proposal", {"raw_predicate": "tampered"}),
                    ("claim_id", uuid.uuid4()),
                ):
                    decision_row = await db.get(GraphClaimDecision, input_value.decision_id)
                    assert decision_row is not None
                    setattr(decision_row, field, value)
                    with pytest.raises(DBAPIError):
                        await db.commit()
                    await db.rollback()

                raw_row = await db.get(GraphRawClaim, input_value.claim_id)
                assert raw_row is not None
                raw_row.raw_predicate = "tampered"
                with pytest.raises(DBAPIError):
                    await db.commit()
                await db.rollback()

                occurrence_row = (
                    await db.execute(
                        select(GraphRawClaimOccurrence).where(
                            GraphRawClaimOccurrence.extraction_occurrence_id
                            == input_value.extraction_occurrence_id
                        )
                    )
                ).scalar_one()
                occurrence_row.prompt_version = "tampered"
                with pytest.raises(DBAPIError):
                    await db.commit()
                await db.rollback()

                await db.delete(row)
                with pytest.raises(DBAPIError):
                    await db.commit()
                await db.rollback()
                assert await get_canonical_mapping(
                    db,
                    library_id=input_value.library_id,
                    document_revision_id=input_value.document_revision_id,
                    mapping_result_id=first.mapping.mapping_result_id,
                ) is not None

                async def concurrent_insert() -> bool:
                    concurrent_result = _build(input_value, mapping_version=10)
                    async with sessions() as concurrent_db:
                        write = await create_or_get_canonical_mapping(
                            concurrent_db,
                            input_value,
                            concurrent_result,
                            authority_id=authority.authority_id,
                        )
                        await concurrent_db.commit()
                        return write.mapping_created

                created_flags = await asyncio.gather(concurrent_insert(), concurrent_insert())
                assert sorted(created_flags) == [False, True]
                concurrent_mapping = _build(input_value, mapping_version=10)
                stored_concurrent = await get_canonical_mapping(
                    db,
                    library_id=input_value.library_id,
                    document_revision_id=input_value.document_revision_id,
                    mapping_result_id=concurrent_mapping.mapping_result_id,
                )
                assert stored_concurrent is not None

                dangling = _row_from_result(_build(input_value, mapping_version=3), authority=authority)
                dangling.claim_id = uuid.uuid4()
                dangling_projection = dict(dangling.result_projection)
                dangling_projection["claim_id"] = str(dangling.claim_id)
                dangling.result_projection = dangling_projection
                db.add(dangling)
                with pytest.raises(DBAPIError):
                    await db.flush()
                await db.rollback()

                db.add(
                    GraphEntityCandidate(
                        id=uuid.uuid4(),
                        job_id=input_value.job_id,
                        library_id=input_value.library_id,
                        ontology_version_id=input_value.ontology_version_id,
                        entity_type_key="fixture_entity",
                        canonical_name="retention",
                        normalized_name="retention",
                        proposed_aliases=[],
                        proposed_properties={},
                        external_mapping_hints={},
                        candidate_key="m3-retention-candidate",
                        status="extracted",
                    )
                )
                await db.commit()
                job = await db.get(GraphExtractionJob, input_value.job_id)
                assert job is not None
                await db.delete(job)
                with pytest.raises(IntegrityError):
                    await db.commit()
                await db.rollback()
                assert await _count(db, GraphClaimMapping) == 5

                for table in ("graph_claim_mappings", "graph_mapping_authority_snapshots"):
                    with pytest.raises(DBAPIError):
                        await db.execute(text(f"TRUNCATE {table}"))
                    await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(run())
