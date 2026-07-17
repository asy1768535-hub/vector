from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence
from app.models.user import User
from app.schemas.v06_graph_retrieval import GraphRetrievalQueryRequest
from app.services import graph_retrieval
from tests.test_v05_m3_publication_activation_pg import (
    SeededGraph,
    _activate,
    _add_manual_entity,
    _create_database,
    _drop_database,
    _plan,
    _publication_config,
    _seed_graph,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("VECTOR_KB_PG_TEST_DSN"),
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database for v0.6 M4 acceptance.",
)


@dataclass(frozen=True, slots=True)
class M4Fixture:
    graph: SeededGraph
    publication_id: uuid.UUID
    target_entity_id: uuid.UUID
    evidence_id: uuid.UUID
    document_id: uuid.UUID
    revision_id: uuid.UUID
    block_id: uuid.UUID
    cap_evidence_ids: tuple[uuid.UUID, ...]
    foreign_evidence_id: uuid.UUID
    foreign_block_id: uuid.UUID
    wrong_document_block_id: uuid.UUID
    wrong_revision_block_id: uuid.UUID


async def _expect_code(awaitable, code: str) -> None:
    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        await awaitable
    assert exc_info.value.code == code


def _request(fixture: M4Fixture, *, include_evidence_locators: bool = True):
    return GraphRetrievalQueryRequest(
        ontology_version_id=fixture.graph.ontology_id,
        expected_publication_id=fixture.publication_id,
        seeds=[
            {"entity_id": fixture.graph.first_entity_id},
            {"canonical_name": "Alice", "entity_type_key": "person"},
        ],
        direction="outbound",
        relation_type_keys=["member_of"],
        max_hops=2,
        max_nodes=100,
        max_relations=200,
        include_evidence_locators=include_evidence_locators,
    )


async def _prepare_frozen_support(Session, graph: SeededGraph):
    async with Session() as db:
        relation = await db.get(KnowledgeRelation, graph.relation_id)
        relation_support = (
            await db.execute(
                select(RelationEvidence).where(
                    RelationEvidence.relation_id == graph.relation_id,
                )
            )
        ).scalars().one()
        evidence = await db.get(EvidenceUnit, relation_support.evidence_id)
        document = await db.get(Document, evidence.document_id)
        revision = await db.get(DocumentRevision, evidence.document_revision_id)
        block_id = uuid.uuid4()
        db.add(
            DocumentBlock(
                id=block_id,
                library_id=graph.library_id,
                document_id=evidence.document_id,
                document_revision_id=evidence.document_revision_id,
                seq=0,
                block_kind="paragraph",
                page_start=3,
                page_end=4,
                source_start=7,
                source_end=19,
                text="private-source-text",
                content={"secret": "private-block-content"},
                parser_name="plain",
                parser_version="v1",
            )
        )
        evidence.document_block_id = block_id
        evidence.page_start = 3
        evidence.page_end = 4
        evidence.source_start = 7
        evidence.source_end = 19
        evidence.text_quote = "private-evidence-quote"
        evidence.evidence_metadata = {"secret": "private-evidence-metadata"}
        document.title = "private-document-title"
        document.doc_metadata = {"secret": "private-document-metadata"}
        revision.normalized_text = "private-revision-text"
        for index in range(2):
            db.add(
                EntityMention(
                    library_id=graph.library_id,
                    entity_id=graph.first_entity_id,
                    evidence_id=evidence.id,
                    document_id=evidence.document_id,
                    document_revision_id=evidence.document_revision_id,
                    mention_text=f"Alice duplicate {index}",
                    quote_text="private-mention-quote",
                    evidence_text_snapshot="private-mention-snapshot",
                    source_type="extracted",
                    status="active",
                )
            )
        await db.commit()
        return (
            relation.target_entity_id,
            evidence.id,
            evidence.document_id,
            evidence.document_revision_id,
            block_id,
        )


async def _add_post_publication_evidence(
    Session,
    graph: SeededGraph,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> tuple[uuid.UUID, ...]:
    evidence_ids = tuple(sorted((uuid.uuid4() for _ in range(21)), key=str))
    async with Session() as db:
        for index, evidence_id in enumerate(evidence_ids):
            db.add(
                EvidenceUnit(
                    id=evidence_id,
                    library_id=graph.library_id,
                    document_id=document_id,
                    document_revision_id=revision_id,
                    evidence_kind="text",
                    page_start=index + 1,
                    page_end=index + 1,
                    source_start=index,
                    source_end=index + 1,
                    text_quote="post-publication-private-text",
                    evidence_metadata={"secret": index},
                    status="active",
                )
            )
        await db.flush()
        live_only_id = evidence_ids[0]
        db.add(
            EntityMention(
                library_id=graph.library_id,
                entity_id=graph.first_entity_id,
                evidence_id=live_only_id,
                document_id=document_id,
                document_revision_id=revision_id,
                mention_text="live-only entity support",
                quote_text="live-only-private-quote",
                source_type="extracted",
                status="active",
            )
        )
        db.add(
            RelationEvidence(
                library_id=graph.library_id,
                relation_id=graph.relation_id,
                evidence_id=live_only_id,
                document_id=document_id,
                document_revision_id=revision_id,
                support_type="supports",
                quote_text="live-only-private-relation-quote",
                status="active",
            )
        )
        await db.commit()
    return evidence_ids


async def _add_bad_blocks(
    Session,
    fixture_scope: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    foreign_graph: SeededGraph,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    library_id, document_id, revision_id = fixture_scope
    foreign_block_id = uuid.uuid4()
    wrong_document_block_id = uuid.uuid4()
    wrong_revision_block_id = uuid.uuid4()
    async with Session() as db:
        for block_id, block_library_id, block_document_id, block_revision_id in (
            (foreign_block_id, foreign_graph.library_id, document_id, revision_id),
            (wrong_document_block_id, library_id, uuid.uuid4(), revision_id),
            (wrong_revision_block_id, library_id, document_id, uuid.uuid4()),
        ):
            db.add(
                DocumentBlock(
                    id=block_id,
                    library_id=block_library_id,
                    document_id=block_document_id,
                    document_revision_id=block_revision_id,
                    seq=0,
                    block_kind="paragraph",
                    text="private-bad-block-text",
                    parser_name="plain",
                    parser_version="v1",
                )
            )
        await db.commit()
    return foreign_block_id, wrong_document_block_id, wrong_revision_block_id


async def _publication_item(
    db,
    publication_id: uuid.UUID,
    item_kind: str,
    fact_id: uuid.UUID,
) -> GraphPublicationItem:
    target = (
        GraphPublicationItem.entity_id
        if item_kind == "entity"
        else GraphPublicationItem.relation_id
    )
    return (
        await db.execute(
            select(GraphPublicationItem).where(
                GraphPublicationItem.publication_id == publication_id,
                GraphPublicationItem.item_kind == item_kind,
                target == fact_id,
            )
        )
    ).scalars().one()


async def _set_item_support(
    Session,
    fixture: M4Fixture,
    value: Any,
) -> None:
    async with Session() as db:
        item = await _publication_item(
            db,
            fixture.publication_id,
            "entity",
            fixture.graph.first_entity_id,
        )
        item.support_evidence_ids = value
        await db.commit()


async def _set_attribute(Session, model, row_id, attribute: str, value: Any) -> None:
    async with Session() as db:
        row = await db.get(model, row_id)
        setattr(row, attribute, value)
        await db.commit()


async def _execute(
    Session,
    fixture: M4Fixture,
    *,
    include_evidence_locators=True,
    statement_log: list[str] | None = None,
):
    async with Session() as db:
        library = await db.get(Library, fixture.graph.library_id)
        if statement_log is not None:
            statement_log.clear()
        response = await graph_retrieval.execute_graph_retrieval_query(
            db,
            library,
            _request(
                fixture,
                include_evidence_locators=include_evidence_locators,
            ),
            config=Settings(_env_file=None),
        )
        assert not db.new and not db.dirty and not db.deleted
        return response


async def _execute_expect_code(Session, fixture: M4Fixture, code: str) -> None:
    async with Session() as db:
        library = await db.get(Library, fixture.graph.library_id)
        await _expect_code(
            graph_retrieval.execute_graph_retrieval_query(
                db,
                library,
                _request(fixture),
                config=Settings(_env_file=None),
            ),
            code,
        )


async def _seed_fixture(Session) -> M4Fixture:
    graph = await _seed_graph(
        Session,
        "v06_m4_" + uuid.uuid4().hex[:8],
        with_relation=True,
    )
    foreign_graph = await _seed_graph(
        Session,
        "v06_m4_foreign_" + uuid.uuid4().hex[:8],
        with_relation=True,
    )
    target_id, evidence_id, document_id, revision_id, block_id = (
        await _prepare_frozen_support(Session, graph)
    )
    publication_id = await _plan(
        Session,
        graph,
        "v06-m4-base",
        _publication_config(),
    )
    assert (await _activate(Session, publication_id, _publication_config()))[0] == "active"
    cap_evidence_ids = await _add_post_publication_evidence(
        Session,
        graph,
        document_id,
        revision_id,
    )
    async with Session() as db:
        foreign_evidence_id = (
            await db.execute(
                select(RelationEvidence.evidence_id).where(
                    RelationEvidence.relation_id == foreign_graph.relation_id,
                )
            )
        ).scalar_one()
    bad_blocks = await _add_bad_blocks(
        Session,
        (graph.library_id, document_id, revision_id),
        foreign_graph,
    )
    return M4Fixture(
        graph=graph,
        publication_id=publication_id,
        target_entity_id=target_id,
        evidence_id=evidence_id,
        document_id=document_id,
        revision_id=revision_id,
        block_id=block_id,
        cap_evidence_ids=cap_evidence_ids,
        foreign_evidence_id=foreign_evidence_id,
        foreign_block_id=bad_blocks[0],
        wrong_document_block_id=bad_blocks[1],
        wrong_revision_block_id=bad_blocks[2],
    )


async def _run_pg_acceptance(dsn: str, monkeypatch) -> None:
    engine = create_async_engine(dsn, pool_size=4, max_overflow=2)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    try:
        fixture = await _seed_fixture(Session)
        base_support = str(fixture.evidence_id)
        async with Session() as db:
            first_item = await _publication_item(
                db,
                fixture.publication_id,
                "entity",
                fixture.graph.first_entity_id,
            )
            empty_item = await _publication_item(
                db,
                fixture.publication_id,
                "entity",
                fixture.target_entity_id,
            )
            relation_item = await _publication_item(
                db,
                fixture.publication_id,
                "relation",
                fixture.graph.relation_id,
            )
            assert first_item.support_evidence_ids == [base_support, base_support]
            assert empty_item.support_evidence_ids == []
            assert relation_item.support_evidence_ids == [base_support]
            audit_before = (
                await db.execute(select(func.count()).select_from(AuditLog))
            ).scalar_one()
            graph_counts_before = (
                (await db.execute(select(func.count()).select_from(Entity))).scalar_one(),
                (
                    await db.execute(select(func.count()).select_from(KnowledgeRelation))
                ).scalar_one(),
                (
                    await db.execute(select(func.count()).select_from(GraphPublicationItem))
                ).scalar_one(),
            )

        async with Session() as db:
            library = await db.get(Library, fixture.graph.library_id)
            initial_traversal = (
                await graph_retrieval._resolve_and_traverse_graph_retrieval_query_unfenced(
                    db,
                    library,
                    _request(fixture),
                    config=Settings(_env_file=None),
                )
            )
            initial_facts = graph_retrieval._selected_graph_facts(
                initial_traversal.traversal
            )
            initial_support_rows = (
                await db.execute(
                    graph_retrieval._fact_support_statement(
                        initial_traversal.resolution.snapshot,
                        initial_facts,
                        max_evidence_per_fact=20,
                    )
                )
            ).all()
            initial_support_shape = [
                (
                    row.output_index,
                    row.item_kind,
                    row.support_count,
                    row.support_position,
                    row.selected_evidence_id_text is None,
                )
                for row in initial_support_rows
            ]
            assert initial_support_shape == [
                (0, "entity", 2, 1, False),
                (0, "entity", 2, 2, False),
                (1, "entity", 0, None, True),
                (2, "relation", 1, 1, False),
            ]

        statements.clear()
        response = await _execute(Session, fixture, statement_log=statements)
        assert len(statements) == 12
        assert any("LEFT OUTER JOIN LATERAL" in sql for sql in statements)
        assert any("generate_series" in sql for sql in statements)
        assert all("jsonb_array_elements" not in sql for sql in statements)
        assert all("entity_mentions" not in sql for sql in statements)
        assert all("relation_evidence" not in sql for sql in statements)
        assert all("fact_snapshot" not in sql for sql in statements)
        assert all("support_counts" not in sql for sql in statements)
        assert all("text_quote" not in sql for sql in statements)
        assert all("normalized_text" not in sql for sql in statements)
        assert not any(
            sql.lstrip().lower().startswith(("insert", "update", "delete"))
            for sql in statements
        )

        first = next(row for row in response.nodes if row.id == fixture.graph.first_entity_id)
        empty = next(row for row in response.nodes if row.id == fixture.target_entity_id)
        relation = response.relations[0]
        assert [row.evidence_id for row in first.evidence] == [fixture.evidence_id]
        assert empty.evidence == []
        assert [row.evidence_id for row in relation.evidence] == [fixture.evidence_id]
        assert first.evidence[0].model_dump(mode="json") == {
            "evidence_id": str(fixture.evidence_id),
            "document_id": str(fixture.document_id),
            "document_revision_id": str(fixture.revision_id),
            "document_block_id": str(fixture.block_id),
            "evidence_kind": "text",
            "page_start": 3,
            "page_end": 4,
            "source_start": 7,
            "source_end": 19,
        }
        assert response.counts.evidence_locators == 2
        assert response.truncated.evidence is False
        payload_text = str(response.model_dump(mode="json")).lower()
        for private_value in (
            "private-source-text",
            "private-evidence-quote",
            "private-document-title",
            "private-revision-text",
            "live-only-private-quote",
        ):
            assert private_value not in payload_text

        async with Session() as db:
            library = await db.get(Library, fixture.graph.library_id)
            traversal = await graph_retrieval._resolve_and_traverse_graph_retrieval_query_unfenced(
                db,
                library,
                _request(fixture),
                config=Settings(_env_file=None),
            )
            facts = graph_retrieval._selected_graph_facts(traversal.traversal)
            support_rows = (
                await db.execute(
                    graph_retrieval._fact_support_statement(
                        traversal.resolution.snapshot,
                        facts,
                        max_evidence_per_fact=20,
                    )
                )
            ).all()
            empty_rows = [
                row
                for row in support_rows
                if row.item_kind == "entity" and row.fact_id == fixture.target_entity_id
            ]
            assert len(empty_rows) == 1
            assert empty_rows[0].support_count == 0
            assert empty_rows[0].support_position is None
            assert empty_rows[0].selected_evidence_id_text is None

        exact_ids = fixture.cap_evidence_ids[:20]
        await _set_item_support(Session, fixture, [str(value) for value in exact_ids])
        statements.clear()
        exact = await _execute(Session, fixture, statement_log=statements)
        exact_node = next(
            row for row in exact.nodes if row.id == fixture.graph.first_entity_id
        )
        assert [row.evidence_id for row in exact_node.evidence] == list(exact_ids)
        assert exact.truncated.evidence is False
        assert len(statements) == 12

        over_ids = fixture.cap_evidence_ids
        await _set_item_support(Session, fixture, [str(value) for value in over_ids])
        statements.clear()
        over = await _execute(Session, fixture, statement_log=statements)
        over_node = next(row for row in over.nodes if row.id == fixture.graph.first_entity_id)
        assert [row.evidence_id for row in over_node.evidence] == list(over_ids[:20])
        assert over.truncated.evidence is True
        assert len(statements) == 12

        await _set_item_support(Session, fixture, [base_support] * 5000)
        async with Session() as db:
            library = await db.get(Library, fixture.graph.library_id)
            traversal = await graph_retrieval._resolve_and_traverse_graph_retrieval_query_unfenced(
                db,
                library,
                _request(fixture),
                config=Settings(_env_file=None),
            )
            facts = graph_retrieval._selected_graph_facts(traversal.traversal)
            bounded_rows = (
                await db.execute(
                    graph_retrieval._fact_support_statement(
                        traversal.resolution.snapshot,
                        facts,
                        max_evidence_per_fact=20,
                    )
                )
            ).all()
            first_rows = [row for row in bounded_rows if row.output_index == 0]
            assert len(first_rows) == 20
            assert all(row.support_count == 5000 for row in first_rows)
        bounded = await _execute(Session, fixture)
        bounded_node = next(
            row for row in bounded.nodes if row.id == fixture.graph.first_entity_id
        )
        assert [row.evidence_id for row in bounded_node.evidence] == [fixture.evidence_id]
        assert bounded.truncated.evidence is True
        await _set_item_support(Session, fixture, [base_support, base_support])

        statements.clear()
        without_locators = await _execute(
            Session,
            fixture,
            include_evidence_locators=False,
            statement_log=statements,
        )
        assert len(statements) == 10
        assert all("support_evidence_ids" not in sql for sql in statements)
        assert all(not row.evidence for row in (*without_locators.nodes, *without_locators.relations))
        assert without_locators.truncated.evidence is False

        current_ready_failures = (
            (EvidenceUnit, fixture.evidence_id, "status", "stale", "active"),
            (Document, fixture.document_id, "current_revision_id", uuid.uuid4(), fixture.revision_id),
            (Document, fixture.document_id, "status", "processing", "ready"),
            (DocumentRevision, fixture.revision_id, "status", "processing", "ready"),
            (
                Document,
                fixture.document_id,
                "deleted_at",
                datetime.now(timezone.utc),
                None,
            ),
        )
        for model, row_id, attribute, invalid, valid in current_ready_failures:
            await _set_attribute(Session, model, row_id, attribute, invalid)
            await _execute_expect_code(
                Session,
                fixture,
                "graph_publication_invariant_failed",
            )
            await _set_attribute(Session, model, row_id, attribute, valid)

        for invalid_block_id in (
            uuid.uuid4(),
            fixture.foreign_block_id,
            fixture.wrong_document_block_id,
            fixture.wrong_revision_block_id,
        ):
            await _set_attribute(
                Session,
                EvidenceUnit,
                fixture.evidence_id,
                "document_block_id",
                invalid_block_id,
            )
            await _execute_expect_code(
                Session,
                fixture,
                "graph_publication_invariant_failed",
            )
        await _set_attribute(
            Session,
            EvidenceUnit,
            fixture.evidence_id,
            "document_block_id",
            fixture.block_id,
        )

        for corrupt_support in (
            {"secret": "not-an-array"},
            [str(fixture.cap_evidence_ids[1]), str(fixture.cap_evidence_ids[0])],
            ["not-a-canonical-uuid"],
            [str(fixture.foreign_evidence_id)],
        ):
            await _set_item_support(Session, fixture, corrupt_support)
            await _execute_expect_code(
                Session,
                fixture,
                "graph_publication_invariant_failed",
            )
        await _set_item_support(Session, fixture, [base_support, base_support])

        async with Session() as db:
            library = await db.get(Library, fixture.graph.library_id)
            traversal = await graph_retrieval._resolve_and_traverse_graph_retrieval_query_unfenced(
                db,
                library,
                _request(fixture),
                config=Settings(_env_file=None),
            )
            async with Session() as writer:
                item = await _publication_item(
                    writer,
                    fixture.publication_id,
                    "entity",
                    fixture.graph.first_entity_id,
                )
                original_hash = item.item_hash
                item.item_hash = uuid.uuid4().hex * 2
                await writer.commit()
            await _expect_code(
                graph_retrieval.hydrate_graph_evidence_locators(
                    db,
                    library,
                    traversal,
                    max_evidence_per_fact=20,
                ),
                "graph_publication_invariant_failed",
            )
        await _set_attribute(
            Session,
            GraphPublicationItem,
            item.id,
            "item_hash",
            original_hash,
        )

        monkeypatch.setattr(settings, "graph_retrieval_enabled", True)
        monkeypatch.setattr(settings, "graph_retrieval_timeout_seconds", 3.0)

        async def override_db():
            async with Session() as db:
                yield db

        async def override_user():
            return User(
                id=uuid.uuid4(),
                email="v06-m4-pg@example.com",
                is_superuser=True,
                is_active=True,
            )

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[current_active_user] = override_user
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                async with Session() as db:
                    library = await db.get(Library, fixture.graph.library_id)
                    path = f"/libraries/{library.slug}/v06/graph/query"

                success = await client.post(
                    path,
                    json=_request(fixture).model_dump(mode="json"),
                )
                assert success.status_code == 200
                assert success.json()["counts"]["evidence_locators"] == 2

                missing_body = _request(fixture).model_dump(mode="json")
                missing_body["seeds"] = [{"entity_id": str(uuid.uuid4())}]
                missing = await client.post(path, json=missing_body)
                assert missing.status_code == 404
                assert missing.json() == {"detail": "seed_not_found", "candidates": []}

                await _set_item_support(
                    Session,
                    fixture,
                    [str(fixture.foreign_evidence_id)],
                )
                foreign = await client.post(
                    path,
                    json=_request(fixture).model_dump(mode="json"),
                )
                assert foreign.status_code == 503
                assert foreign.json() == {
                    "detail": "graph_publication_invariant_failed",
                    "candidates": [],
                }
                assert str(fixture.foreign_evidence_id) not in foreign.text
                await _set_item_support(Session, fixture, [base_support, base_support])

                original_execute = graph_retrieval.execute_graph_retrieval_query

                async def pg_delay(db, *_args, **_kwargs):
                    await db.execute(text("SELECT pg_sleep(1)"))

                monkeypatch.setattr(
                    graph_retrieval,
                    "execute_graph_retrieval_query",
                    pg_delay,
                )
                monkeypatch.setattr(settings, "graph_retrieval_timeout_seconds", 0.05)
                timeout_response = await client.post(
                    path,
                    json=_request(fixture).model_dump(mode="json"),
                )
                assert timeout_response.status_code == 504
                assert timeout_response.json() == {
                    "detail": "graph_retrieval_timeout",
                    "candidates": [],
                }

                reuse_probe: list[int] = []

                async def probe_reused_pool(db, *_args, **_kwargs):
                    reuse_probe.append((await db.execute(text("SELECT 1"))).scalar_one())
                    raise graph_retrieval.GraphRetrievalServiceError("seed_not_found")

                monkeypatch.setattr(
                    graph_retrieval,
                    "execute_graph_retrieval_query",
                    probe_reused_pool,
                )
                probe = await client.post(
                    path,
                    json=_request(fixture).model_dump(mode="json"),
                )
                assert probe.status_code == 404
                assert reuse_probe == [1]
                monkeypatch.setattr(
                    graph_retrieval,
                    "execute_graph_retrieval_query",
                    original_execute,
                )
                monkeypatch.setattr(settings, "graph_retrieval_timeout_seconds", 3.0)
        finally:
            app.dependency_overrides.clear()

        async with Session() as db:
            audit_after = (
                await db.execute(select(func.count()).select_from(AuditLog))
            ).scalar_one()
            graph_counts_after = (
                (await db.execute(select(func.count()).select_from(Entity))).scalar_one(),
                (
                    await db.execute(select(func.count()).select_from(KnowledgeRelation))
                ).scalar_one(),
                (
                    await db.execute(select(func.count()).select_from(GraphPublicationItem))
                ).scalar_one(),
            )
        assert audit_after == audit_before
        assert graph_counts_after == graph_counts_before

        original_hydrate = graph_retrieval.hydrate_graph_evidence_locators

        async def hydrate_then_degrade(*args, **kwargs):
            await original_hydrate(*args, **kwargs)
            await _set_attribute(
                Session,
                GraphPublication,
                fixture.publication_id,
                "status",
                "degraded",
            )
            raise graph_retrieval.GraphRetrievalServiceError(
                "graph_publication_invariant_failed"
            )

        monkeypatch.setattr(
            graph_retrieval,
            "hydrate_graph_evidence_locators",
            hydrate_then_degrade,
        )
        await _execute_expect_code(Session, fixture, "graph_publication_unavailable")
        await _set_attribute(
            Session,
            GraphPublication,
            fixture.publication_id,
            "status",
            "active",
        )
        monkeypatch.setattr(
            graph_retrieval,
            "hydrate_graph_evidence_locators",
            original_hydrate,
        )

        switched = False

        async def hydrate_then_switch(*args, **kwargs):
            nonlocal switched
            hydration = await original_hydrate(*args, **kwargs)
            if not switched:
                switched = True
                await _add_manual_entity(Session, fixture.graph, "M4 Replacement")
                replacement_id = await _plan(
                    Session,
                    fixture.graph,
                    "v06-m4-replacement",
                    _publication_config(),
                )
                assert (
                    await _activate(Session, replacement_id, _publication_config())
                )[0] == "active"
            return hydration

        monkeypatch.setattr(
            graph_retrieval,
            "hydrate_graph_evidence_locators",
            hydrate_then_switch,
        )
        await _execute_expect_code(Session, fixture, "publication_changed")
        assert switched is True
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


def test_v06_m4_api_evidence_on_postgres(monkeypatch):
    name, dsn = _create_database(monkeypatch, "vkt_v06_m4")
    try:
        command.upgrade(Config("alembic.ini"), "0023")
        asyncio.run(_run_pg_acceptance(dsn, monkeypatch))
    finally:
        _drop_database(name)
