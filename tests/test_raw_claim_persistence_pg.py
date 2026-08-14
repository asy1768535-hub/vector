from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.db import Base
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_candidates import GraphEntityCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.organization import DEFAULT_ORGANIZATION_ID, Organization
from app.schemas.raw_claim import RawClaimV1
from app.services.raw_claim_persistence import (
    RawClaimConflictError,
    RawClaimScopeError,
    create_or_get_raw_claim,
    get_raw_claim,
)


_PG_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL service",
)


async def _ensure_m2a_guards(engine) -> None:
    """Support both create_all-only and migration-backed disposable databases."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                DO $$
                BEGIN
                    IF to_regprocedure('graph_raw_claims_immutable_guard()') IS NULL THEN
                        CREATE FUNCTION graph_raw_claims_immutable_guard()
                        RETURNS trigger LANGUAGE plpgsql AS $fn$
                        BEGIN
                            RAISE EXCEPTION 'raw claim persistence rows are immutable';
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
                        WHERE tgname = 'graph_raw_claims_immutable_guard'
                          AND tgrelid = 'graph_raw_claims'::regclass
                    ) THEN
                        CREATE TRIGGER graph_raw_claims_immutable_guard
                        BEFORE UPDATE OR DELETE ON graph_raw_claims
                        FOR EACH ROW EXECUTE FUNCTION graph_raw_claims_immutable_guard();
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_trigger
                        WHERE tgname = 'graph_raw_claim_occurrences_immutable_guard'
                          AND tgrelid = 'graph_raw_claim_occurrences'::regclass
                    ) THEN
                        CREATE TRIGGER graph_raw_claim_occurrences_immutable_guard
                        BEFORE UPDATE OR DELETE ON graph_raw_claim_occurrences
                        FOR EACH ROW EXECUTE FUNCTION graph_raw_claims_immutable_guard();
                    END IF;
                END;
                $$;
                """
            )
        )


def _claim(
    *, ids: dict[str, uuid.UUID], model_name: str = "pg-model", raw_predicate: str = "supports"
) -> RawClaimV1:
    quote = "PG raw claim evidence"
    quote_hash = hashlib.sha256(quote.encode()).hexdigest()
    return RawClaimV1(
        claim_id=ids["claim"],
        library_id=ids["library"],
        document_id=ids["document"],
        document_revision_id=ids["revision"],
        revision_no=1,
        job_id=ids["job"],
        extraction_unit_id=ids["unit"],
        source_mention={"local_id": "source", "surface": "Source", "evidence_ref": "e1"},
        raw_predicate=raw_predicate,
        target_mention={"local_id": "target", "surface": "Target", "evidence_ref": "e1"},
        surface_direction="source_to_target",
        negation={"value": False, "evidence_ref": "e1"},
        modality={"value": "asserted", "evidence_ref": "e1"},
        qualifiers=[{"key": "basis", "value": "pg", "evidence_ref": "e1"}],
        valid_time=None,
        effective_time=None,
        evidence_refs=[
            {
                "ref_id": "e1",
                "evidence_id": ids["evidence"],
                "library_id": ids["library"],
                "document_id": ids["document"],
                "document_revision_id": ids["revision"],
                "revision_no": 1,
                "job_id": ids["job"],
                "extraction_unit_id": ids["unit"],
                "unit_id": ids["evidence"],
                "chunk_id": ids["chunk"],
                "quote_sha256": quote_hash,
                "unit_text_sha256": quote_hash,
                "source_span": {"start": 0, "end": len(quote)},
            }
        ],
        extractor_version="extractor-v1",
        prompt_version="prompt-v1",
        model_provider="fixture-provider",
        model_name=model_name,
        model_config_hash="a" * 64,
        prompt_content_hash="b" * 64,
        parser_version="parser-v1",
        normalization_rule_version="normalization-v1",
        ontology_snapshot_hash="c" * 64,
        extraction_occurrence_id=ids["occurrence"],
    )


def _ids() -> dict[str, uuid.UUID]:
    return {name: uuid.uuid4() for name in (
        "library", "document", "revision", "ontology", "job", "unit",
        "evidence", "chunk", "claim", "occurrence",
    )}


async def _run_acceptance() -> None:
    engine = create_async_engine(_PG_DSN, pool_size=2, max_overflow=0)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    ids = _ids()
    body = "PG raw claim evidence"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await _ensure_m2a_guards(engine)
        async with session_factory() as db:
            if await db.get(Organization, DEFAULT_ORGANIZATION_ID) is None:
                db.add(Organization(
                    id=DEFAULT_ORGANIZATION_ID,
                    slug="raw-claim-pg",
                    name="Raw claim PG acceptance",
                    deployment_profile="private",
                    status="active",
                ))
                await db.flush()
            library = Library(
                id=ids["library"], slug="raw-claim-" + uuid.uuid4().hex[:8],
                name="Raw claim acceptance", embedding_model="fixture",
                embedding_dim=3, qdrant_collection="unused",
                lifecycle_mode="managed", index_state="ready",
            )
            document = Document(
                id=ids["document"], library_id=ids["library"],
                title="PG claim", external_id="pg-claim",
                content_hash=hashlib.sha256(body.encode()).hexdigest(),
                current_revision=1, status="ready",
            )
            db.add(library)
            await db.flush()
            db.add(document)
            await db.flush()
            revision = DocumentRevision(
                id=ids["revision"], document_id=ids["document"], library_id=ids["library"],
                revision_no=1, title="PG claim", content_hash=document.content_hash,
                normalized_text=body, parser_name="fixture", parser_version="v1",
                chunking_strategy="manual", chunking_strategy_version="v1", status="ready",
            )
            ontology = OntologyVersion(
                id=ids["ontology"], library_id=ids["library"], version_key="pg",
                version_no=1, status="active", origin="user", confirmed=True,
            )
            job = GraphExtractionJob(
                id=ids["job"], library_id=ids["library"], document_id=ids["document"],
                document_revision_id=ids["revision"], ontology_version_id=ids["ontology"],
                trigger_type="eval", execution_mode="eval", status="succeeded",
                input_fingerprint="d" * 64, idempotency_key="pg-job-" + uuid.uuid4().hex,
                model_provider="fixture", model_name="pg-model", prompt_version="v1",
                extractor_version="v1", output_parser_version="v1", context_policy_version="v1",
                extraction_policy_version="v1", normalization_rule_version="v1",
                confidence_policy_version="v1", document_parser_version="v1",
                chunking_strategy_version="v1", model_config_snapshot={},
                policy_config_snapshot={}, model_config_hash="a" * 64,
                policy_config_hash="b" * 64, ontology_snapshot={}, ontology_snapshot_hash="c" * 64,
                prompt_content_hash="d" * 64,
            )
            evidence = EvidenceUnit(
                id=ids["evidence"], library_id=ids["library"], document_id=ids["document"],
                document_revision_id=ids["revision"], evidence_kind="chunk", source_start=0,
                source_end=len(body), text_quote=body,
                text_quote_hash=hashlib.sha256(body.encode()).hexdigest(), status="active",
            )
            chunk = Chunk(
                id=ids["chunk"], library_id=ids["library"], document_id=ids["document"],
                document_revision_id=ids["revision"], evidence_id=ids["evidence"], seq=0,
                chunk_kind="text", text=body, token_count=len(body), source_start=0,
                source_end=len(body),
            )
            unit = GraphExtractionUnit(
                id=ids["unit"], job_id=ids["job"], library_id=ids["library"],
                document_revision_id=ids["revision"], ordinal=0, center_chunk_id=ids["chunk"],
                center_evidence_id=ids["evidence"], unit_fingerprint="e" * 64, status="succeeded",
            )
            db.add_all([revision, ontology])
            await db.flush()
            db.add(job)
            await db.flush()
            db.add(evidence)
            await db.flush()
            db.add(chunk)
            await db.flush()
            db.add(unit)
            await db.commit()

        claim = _claim(ids=ids)
        async with session_factory() as db:
            first = await create_or_get_raw_claim(db, claim)
            await db.commit()
            second = await create_or_get_raw_claim(db, claim)
            await db.commit()
            assert first.claim_created and first.occurrence_created
            assert not second.claim_created and not second.occurrence_created

            second_ids = dict(ids)
            second_ids["claim"] = uuid.uuid4()
            second_ids["occurrence"] = uuid.uuid4()
            second_claim = _claim(ids=second_ids, model_name="pg-model-rerun")
            rerun = await create_or_get_raw_claim(db, second_claim)
            await db.commit()
            assert not rerun.claim_created and rerun.occurrence_created

            conflicting_ids = dict(ids)
            conflicting_ids["claim"] = uuid.uuid4()
            with pytest.raises(RawClaimConflictError, match="existing occurrence conflicts"):
                await create_or_get_raw_claim(
                    db,
                    _claim(
                        ids=conflicting_ids,
                        raw_predicate="conflicts",
                    ),
                )
            await db.rollback()
            assert await get_raw_claim(
                db,
                library_id=ids["library"],
                document_revision_id=ids["revision"],
                claim_id=conflicting_ids["claim"],
            ) is None

        race_ids = dict(ids)
        race_ids["claim"] = uuid.uuid4()
        race_ids["occurrence"] = uuid.uuid4()
        race_claim = _claim(
            ids=race_ids, model_name="pg-race-model", raw_predicate="references"
        )

        async def _write_race():
            async with session_factory() as race_db:
                try:
                    result = await create_or_get_raw_claim(race_db, race_claim)
                    await race_db.commit()
                    return result
                except Exception:
                    await race_db.rollback()
                    raise

        race_results = await asyncio.gather(_write_race(), _write_race())
        assert sum(result.claim_created for result in race_results) == 1
        assert sum(result.occurrence_created for result in race_results) == 1

        async with session_factory() as db:
            missing = second_claim.model_copy(update={"evidence_refs": ()})
            del missing  # M0 rejects an empty evidence set before the DB boundary.
            bad_ids = dict(ids)
            bad_ids["evidence"] = uuid.uuid4()
            with pytest.raises(RawClaimScopeError, match="missing EvidenceUnit"):
                await create_or_get_raw_claim(db, _claim(ids=bad_ids))
            await db.rollback()

            row = await db.get(type(first.claim), ids["claim"])
            row.raw_predicate = "mutated"
            with pytest.raises(DBAPIError):
                await db.commit()
            await db.rollback()
            row = await db.get(type(first.claim), ids["claim"])
            assert row.raw_predicate == "supports"

            await db.delete(row)
            with pytest.raises(DBAPIError):
                await db.commit()
            await db.rollback()
            assert await get_raw_claim(
                db, library_id=ids["library"], document_revision_id=ids["revision"], claim_id=ids["claim"]
            ) is not None

            candidate = GraphEntityCandidate(
                id=uuid.uuid4(), job_id=ids["job"], library_id=ids["library"],
                ontology_version_id=ids["ontology"], entity_type_key="fixture_entity",
                canonical_name="candidate", normalized_name="candidate", proposed_aliases=[],
                proposed_properties={}, external_mapping_hints={}, candidate_key="pg-candidate",
                status="extracted",
            )
            db.add(candidate)
            await db.flush()
            await db.delete(candidate)
            await db.commit()
            assert await get_raw_claim(
                db, library_id=ids["library"], document_revision_id=ids["revision"], claim_id=ids["claim"]
            ) is not None

            unit = await db.get(GraphExtractionUnit, ids["unit"])
            await db.delete(unit)
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()
    finally:
        await engine.dispose()


def test_raw_claim_pg_service_constraints_and_retention():
    asyncio.run(_run_acceptance())
