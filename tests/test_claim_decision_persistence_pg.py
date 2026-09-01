from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.db import Base
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
from app.models.organization import Organization
from app.schemas.claim_decision import ClaimDecisionProjectionV1
from app.services.claim_decision_persistence import (
    ClaimDecisionScopeError,
    create_or_get_claim_decision,
    get_claim_decision,
    list_claim_decisions,
)
from app.services.raw_claim_persistence import create_or_get_raw_claim
from tests.test_claim_decision import _build_production, _extension_proposal
from tests.test_raw_claim_persistence_pg import _claim, _ids


_PG_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL service",
)


async def _ensure_decision_guard(engine) -> None:
    """Support both create_all-only and migration-backed disposable databases."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                DO $$
                BEGIN
                    IF to_regprocedure('graph_claim_decisions_immutable_guard()') IS NULL THEN
                        CREATE FUNCTION graph_claim_decisions_immutable_guard()
                        RETURNS trigger LANGUAGE plpgsql AS $fn$
                        BEGIN
                            RAISE EXCEPTION 'claim decision projection rows are immutable';
                        END;
                        $fn$;
                    END IF;
                END;
                $$;
                """
            )
        )
        await connection.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_trigger
                        WHERE tgname = 'graph_claim_decisions_immutable_guard'
                          AND tgrelid = 'graph_claim_decisions'::regclass
                    ) THEN
                        CREATE TRIGGER graph_claim_decisions_immutable_guard
                        BEFORE UPDATE OR DELETE ON graph_claim_decisions
                        FOR EACH ROW EXECUTE FUNCTION graph_claim_decisions_immutable_guard();
                    END IF;
                END;
                $$;
                """
            )
        )


async def _run_acceptance() -> None:
    engine = create_async_engine(_PG_DSN, pool_size=3, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = _ids()
    organization_id = uuid.uuid4()
    body = "PG raw claim evidence"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await _ensure_decision_guard(engine)

        async with sessions() as db:
            db.add(
                Organization(
                    id=organization_id,
                    slug="decision-pg-" + uuid.uuid4().hex[:10],
                    name="Claim decision acceptance",
                    deployment_profile="private",
                    status="active",
                )
            )
            await db.flush()
            db.add(
                Library(
                    id=ids["library"],
                    organization_id=organization_id,
                    slug="decision-pg-" + uuid.uuid4().hex[:10],
                    name="Claim decision acceptance",
                    embedding_model="fixture",
                    embedding_dim=3,
                    qdrant_collection="decision-pg-unused",
                    lifecycle_mode="managed",
                    index_state="ready",
                )
            )
            await db.flush()
            document = Document(
                id=ids["document"],
                library_id=ids["library"],
                title="Decision document",
                external_id="decision-pg",
                content_hash=hashlib.sha256(body.encode()).hexdigest(),
                current_revision=1,
                status="ready",
            )
            db.add(document)
            await db.flush()
            db.add(
                DocumentRevision(
                    id=ids["revision"],
                    document_id=ids["document"],
                    library_id=ids["library"],
                    revision_no=1,
                    title="Decision document",
                    content_hash=document.content_hash,
                    normalized_text=body,
                    parser_name="fixture",
                    parser_version="v1",
                    chunking_strategy="manual",
                    chunking_strategy_version="v1",
                    status="ready",
                )
            )
            ontology = OntologyVersion(
                id=ids["ontology"],
                library_id=ids["library"],
                version_key="decision-pg",
                version_no=1,
                status="active",
                origin="user",
                confirmed=True,
            )
            db.add(ontology)
            await db.flush()
            db.add(
                GraphExtractionJob(
                    id=ids["job"],
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    ontology_version_id=ids["ontology"],
                    trigger_type="eval",
                    execution_mode="eval",
                    status="succeeded",
                    input_fingerprint="d" * 64,
                    idempotency_key="decision-job-" + uuid.uuid4().hex,
                    model_provider="fixture",
                    model_name="decision-model",
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
                    model_config_hash="a" * 64,
                    policy_config_hash="b" * 64,
                    ontology_snapshot={},
                    ontology_snapshot_hash="c" * 64,
                    prompt_content_hash="d" * 64,
                )
            )
            await db.flush()
            quote_hash = hashlib.sha256(body.encode()).hexdigest()
            db.add(
                EvidenceUnit(
                    id=ids["evidence"],
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    evidence_kind="chunk",
                    source_start=0,
                    source_end=len(body),
                    text_quote=body,
                    text_quote_hash=quote_hash,
                    status="active",
                )
            )
            await db.flush()
            db.add(
                Chunk(
                    id=ids["chunk"],
                    library_id=ids["library"],
                    document_id=ids["document"],
                    document_revision_id=ids["revision"],
                    evidence_id=ids["evidence"],
                    seq=0,
                    chunk_kind="text",
                    text=body,
                    token_count=len(body),
                    source_start=0,
                    source_end=len(body),
                )
            )
            await db.flush()
            db.add(
                GraphExtractionUnit(
                    id=ids["unit"],
                    job_id=ids["job"],
                    library_id=ids["library"],
                    document_revision_id=ids["revision"],
                    ordinal=0,
                    center_chunk_id=ids["chunk"],
                    center_evidence_id=ids["evidence"],
                    unit_fingerprint="e" * 64,
                    status="succeeded",
                )
            )
            await db.commit()

        claim = _claim(ids=ids)
        async with sessions() as db:
            raw_result = await create_or_get_raw_claim(db, claim)
            await db.commit()
            stored_claim = await db.get(type(raw_result.claim), claim.claim_id)
            assert (
                str(stored_claim.library_id),
                str(stored_claim.document_id),
                str(stored_claim.document_revision_id),
                stored_claim.revision_no,
            ) == (
                str(claim.library_id),
                str(claim.document_id),
                str(claim.document_revision_id),
                claim.revision_no,
            )
            decision = _build_production(claim, extraction_occurrence_id=claim.extraction_occurrence_id)
            first = await create_or_get_claim_decision(db, decision)
            await db.commit()
            duplicate = await create_or_get_claim_decision(
                db,
                _build_production(
                    claim,
                    extraction_occurrence_id=claim.extraction_occurrence_id,
                    created_at=decision.created_at.replace(day=8),
                ),
            )
            await db.commit()
            versioned = await create_or_get_claim_decision(
                db,
                _build_production(
                    claim,
                    extraction_occurrence_id=claim.extraction_occurrence_id,
                    decision_version=2,
                ),
            )
            await db.commit()
            extension = await create_or_get_claim_decision(
                db,
                _build_production(
                    claim,
                    extraction_occurrence_id=claim.extraction_occurrence_id,
                    decision_kind="schema_extension_candidate",
                    reason_code="unknown_source_type",
                    proposal=_extension_proposal(claim),
                ),
            )
            await db.commit()

            assert raw_result.claim_created and raw_result.occurrence_created
            assert first.decision_created
            assert not duplicate.decision_created
            assert versioned.decision_created
            assert extension.decision_created
            assert duplicate.decision.created_at == first.decision.created_at
            assert (
                await get_claim_decision(
                    db,
                    library_id=claim.library_id,
                    document_revision_id=claim.document_revision_id,
                    decision_id=decision.decision_id,
                )
            ).proposal.raw_predicate == claim.raw_predicate
            assert len(
                await list_claim_decisions(
                    db,
                    library_id=claim.library_id,
                    document_revision_id=claim.document_revision_id,
                )
            ) == 3

            bad_scope = decision
            bad_payload = bad_scope.model_dump(mode="json")
            bad_payload.update(library_id=str(uuid.uuid4()), decision_fingerprint=None)
            with pytest.raises(ClaimDecisionScopeError, match="scope"):
                await create_or_get_claim_decision(
                    db,
                    ClaimDecisionProjectionV1.model_validate(bad_payload),
                )
            await db.rollback()

            other_ids = dict(ids)
            other_ids["claim"] = uuid.uuid4()
            other_ids["occurrence"] = uuid.uuid4()
            rerun = await create_or_get_raw_claim(
                db, _claim(ids=other_ids, model_name="decision-rerun", raw_predicate="references")
            )
            await db.commit()
            cross_payload = decision.model_dump(mode="json")
            cross_payload.update(
                extraction_occurrence_id=str(rerun.occurrence.extraction_occurrence_id),
                decision_fingerprint=None,
            )
            with pytest.raises(ClaimDecisionScopeError, match="belong"):
                await create_or_get_claim_decision(
                    db,
                    ClaimDecisionProjectionV1.model_validate(cross_payload),
                )
            await db.rollback()

            candidate = GraphEntityCandidate(
                id=uuid.uuid4(),
                job_id=ids["job"],
                library_id=ids["library"],
                ontology_version_id=ids["ontology"],
                entity_type_key="fixture_entity",
                canonical_name="candidate",
                normalized_name="candidate",
                proposed_aliases=[],
                proposed_properties={},
                external_mapping_hints={},
                candidate_key="decision-candidate",
                status="extracted",
            )
            db.add(candidate)
            await db.flush()
            await db.delete(candidate)
            await db.commit()
            assert (
                await get_claim_decision(
                    db,
                    library_id=claim.library_id,
                    document_revision_id=claim.document_revision_id,
                    decision_id=decision.decision_id,
                )
            ) is not None

            row = await db.get(GraphClaimDecision, decision.decision_id)
            row.producer_key = "mutated"
            with pytest.raises(DBAPIError):
                await db.commit()
            await db.rollback()
            row = await db.get(GraphClaimDecision, decision.decision_id)
            assert row.producer_key == decision.producer_key

            await db.delete(row)
            with pytest.raises(DBAPIError):
                await db.commit()
            await db.rollback()
            assert (
                await get_claim_decision(
                    db,
                    library_id=claim.library_id,
                    document_revision_id=claim.document_revision_id,
                    decision_id=decision.decision_id,
                )
            ) is not None

            fk_rows = (
                await db.execute(
                    text(
                        "SELECT conname, confdeltype FROM pg_constraint "
                        "WHERE conname IN "
                        "('fk_graph_claim_decisions_claim', 'fk_graph_claim_decisions_occurrence')"
                    )
                )
            ).all()
            assert {
                name: delete_type.decode() if isinstance(delete_type, bytes) else delete_type
                for name, delete_type in fk_rows
            } == {
                "fk_graph_claim_decisions_claim": "r",
                "fk_graph_claim_decisions_occurrence": "r",
            }

            occurrence_row = (
                await db.execute(
                    select(type(raw_result.occurrence)).where(
                        type(raw_result.occurrence).extraction_occurrence_id
                        == claim.extraction_occurrence_id
                    )
                )
            ).scalar_one()
            await db.delete(occurrence_row)
            with pytest.raises(DBAPIError):
                await db.commit()
            await db.rollback()
            claim_row = await db.get(type(raw_result.claim), claim.claim_id)
            await db.delete(claim_row)
            with pytest.raises(DBAPIError):
                await db.commit()
            await db.rollback()
    finally:
        await engine.dispose()


def test_claim_decision_pg_persistence_and_retention():
    asyncio.run(_run_acceptance())
