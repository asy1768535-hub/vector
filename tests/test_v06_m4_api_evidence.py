from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.auth.backend import current_active_user
from app.config import Settings, settings
from app.db import get_db
from app.main import app
from app.models.user import User
from app.schemas.v06_graph_retrieval import (
    GraphRetrievalAmbiguousCandidate,
    GraphRetrievalErrorResponse,
    GraphRetrievalEvidenceLocator,
    GraphRetrievalQueryRequest,
)
from app.services import graph_retrieval
from tests.test_v06_m2_snapshot_resolver import (
    ENTITY_ID,
    ONTOLOGY_ID,
    PUBLICATION_ID,
    SECOND_ENTITY_ID,
    _library,
    _snapshot,
    _Result,
    FakeDB,
)
from tests.test_v06_m3_traversal import RELATION_IDS, _relation_type, _resolved


EVIDENCE_IDS = tuple(
    uuid.UUID(f"63000000-0000-0000-0000-{index:012d}") for index in range(1, 4)
)
DOCUMENT_ID = uuid.UUID("64000000-0000-0000-0000-000000000001")
REVISION_ID = uuid.UUID("64000000-0000-0000-0000-000000000002")
BLOCK_ID = uuid.UUID("64000000-0000-0000-0000-000000000003")
USER_ID = uuid.UUID("65000000-0000-0000-0000-000000000001")


def _m3_result() -> graph_retrieval.GraphRetrievalTraversalResolution:
    seed = _resolved(ENTITY_ID, name="Alice")
    second = graph_retrieval.TraversedPublishedEntity(
        entity_id=SECOND_ENTITY_ID,
        item_hash="d" * 64,
        entity_type_id=seed.entity_type_id,
        entity_type_key=seed.entity_type_key,
        entity_type_label=seed.entity_type_label,
        canonical_name="Bob",
        normalized_name="bob",
        source_type="manual",
        confidence=None,
        depth=1,
    )
    relation_type = _relation_type(key="member_of")
    relation = graph_retrieval.TraversedPublishedRelation(
        relation_id=RELATION_IDS[0],
        item_hash="e" * 64,
        relation_type_id=relation_type.relation_type_id,
        relation_type_key=relation_type.key,
        relation_type_label=relation_type.label,
        relation_type_direction=relation_type.direction,
        source_entity_id=ENTITY_ID,
        target_entity_id=SECOND_ENTITY_ID,
        source_type="manual",
        confidence=None,
        depth=1,
    )
    resolution = graph_retrieval.GraphRetrievalResolution(
        snapshot=_snapshot(),
        seeds=(seed,),
        relation_types=(relation_type,),
    )
    traversal = graph_retrieval.PublishedGraphTraversal(
        nodes=(graph_retrieval._traversed_seed(seed), second),
        relations=(relation,),
        truncated=graph_retrieval.GraphTraversalTruncation(False, False),
    )
    return graph_retrieval.GraphRetrievalTraversalResolution(
        resolution=resolution,
        traversal=traversal,
    )


def _support_row(
    output_index: int,
    item_kind: str,
    fact_id: uuid.UUID,
    *,
    support_count: int = 0,
    support_position: int | None = None,
    evidence_id: uuid.UUID | str | None = None,
    support_shape_valid: bool = True,
):
    return SimpleNamespace(
        output_index=output_index,
        item_kind=item_kind,
        fact_id=fact_id,
        support_shape_valid=support_shape_valid,
        support_count=support_count,
        support_position=support_position,
        selected_evidence_id_text=(str(evidence_id) if evidence_id is not None else None),
    )


def _empty_support_rows():
    return [
        _support_row(0, "entity", ENTITY_ID),
        _support_row(1, "entity", SECOND_ENTITY_ID),
        _support_row(2, "relation", RELATION_IDS[0]),
    ]


def _evidence_row(
    evidence_id: uuid.UUID,
    *,
    block_id: uuid.UUID | None = BLOCK_ID,
    joined_block_id: uuid.UUID | None = BLOCK_ID,
    page_start: int | None = 1,
    page_end: int | None = 1,
):
    return SimpleNamespace(
        evidence_id=evidence_id,
        document_id=DOCUMENT_ID,
        document_revision_id=REVISION_ID,
        document_block_id=block_id,
        joined_document_block_id=joined_block_id,
        evidence_kind="chunk",
        page_start=page_start,
        page_end=page_end,
        source_start=0,
        source_end=12,
    )


def _request_payload(**overrides):
    payload = {
        "ontology_version_id": str(ONTOLOGY_ID),
        "expected_publication_id": str(PUBLICATION_ID),
        "seeds": [{"entity_id": str(ENTITY_ID)}],
        "direction": "both",
        "relation_type_keys": [],
        "max_hops": 1,
        "max_nodes": 100,
        "max_relations": 200,
        "include_evidence_locators": True,
    }
    payload.update(overrides)
    return payload


def _user(*, superuser=True):
    return User(
        id=USER_ID,
        email="v06@example.com",
        is_superuser=superuser,
        is_active=True,
    )


def _client(db, *, superuser=True):
    async def override_db():
        return db

    async def override_user():
        return _user(superuser=superuser)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_active_user] = override_user
    return TestClient(app)


def _clear_overrides():
    app.dependency_overrides.clear()


def test_m4_error_forward_fix_is_strict_and_sanitized():
    for code in ("graph_retrieval_invalid_request", "graph_retrieval_internal_error"):
        payload = GraphRetrievalErrorResponse(detail=code).model_dump(mode="json")
        assert payload == {"detail": code, "candidates": []}


def test_m4_support_sql_is_left_lateral_bounded_and_keeps_empty_facts():
    result = _m3_result()
    selected = graph_retrieval._selected_graph_facts(result.traversal)
    statement = graph_retrieval._fact_support_statement(
        result.resolution.snapshot,
        selected,
        max_evidence_per_fact=20,
    )
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert "left outer join lateral" in sql
    assert "generate_series" in sql
    assert "least" in sql
    assert "jsonb_array_elements" not in sql
    assert sql.count("graph_publication_items as selected_fact_item") == 1
    assert "fact_snapshot" not in sql
    assert "support_counts" not in sql
    assert ".properties" not in sql

    db = FakeDB([_Result(_empty_support_rows())])
    hydration = asyncio.run(
        graph_retrieval.hydrate_graph_evidence_locators(
            db,
            _library(),
            result,
            max_evidence_per_fact=20,
        )
    )
    assert len(hydration.facts) == 3
    assert all(not fact.locators for fact in hydration.facts)
    assert hydration.truncated is False
    assert len(db.statements) == 1


def test_m4_support_prefix_deduplicates_locators_and_hydrates_once():
    result = _m3_result()
    support_rows = [
        _support_row(0, "entity", ENTITY_ID, support_count=3, support_position=1, evidence_id=EVIDENCE_IDS[0]),
        _support_row(0, "entity", ENTITY_ID, support_count=3, support_position=2, evidence_id=EVIDENCE_IDS[0]),
        _support_row(0, "entity", ENTITY_ID, support_count=3, support_position=3, evidence_id=EVIDENCE_IDS[1]),
        _support_row(1, "entity", SECOND_ENTITY_ID),
        _support_row(2, "relation", RELATION_IDS[0]),
    ]
    db = FakeDB(
        [
            _Result(support_rows),
            _Result([_evidence_row(EVIDENCE_IDS[1]), _evidence_row(EVIDENCE_IDS[0])]),
        ]
    )

    hydration = asyncio.run(
        graph_retrieval.hydrate_graph_evidence_locators(
            db,
            _library(),
            result,
            max_evidence_per_fact=3,
        )
    )

    assert [row.evidence_id for row in hydration.facts[0].locators] == [
        EVIDENCE_IDS[0],
        EVIDENCE_IDS[1],
    ]
    assert hydration.facts[1].locators == hydration.facts[2].locators == ()
    assert len(db.statements) == 2


@pytest.mark.parametrize(
    "support_rows",
    [
        [
            _support_row(0, "entity", ENTITY_ID, support_shape_valid=False),
            *_empty_support_rows()[1:],
        ],
        [
            _support_row(0, "entity", ENTITY_ID, support_count=1, support_position=1, evidence_id="not-a-uuid"),
            *_empty_support_rows()[1:],
        ],
        [
            _support_row(0, "entity", ENTITY_ID, support_count=2, support_position=1, evidence_id=EVIDENCE_IDS[1]),
            _support_row(0, "entity", ENTITY_ID, support_count=2, support_position=2, evidence_id=EVIDENCE_IDS[0]),
            *_empty_support_rows()[1:],
        ],
        _empty_support_rows()[:2],
    ],
)
def test_m4_invalid_or_missing_frozen_support_fails_closed(support_rows):
    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        asyncio.run(
            graph_retrieval.hydrate_graph_evidence_locators(
                FakeDB([_Result(support_rows)]),
                _library(),
                _m3_result(),
                max_evidence_per_fact=20,
            )
        )
    assert exc_info.value.code == "graph_publication_invariant_failed"


def test_m4_missing_or_invalid_block_evidence_fails_closed():
    support_rows = [
        _support_row(0, "entity", ENTITY_ID, support_count=1, support_position=1, evidence_id=EVIDENCE_IDS[0]),
        *_empty_support_rows()[1:],
    ]
    for evidence_rows in ([], [_evidence_row(EVIDENCE_IDS[0], joined_block_id=None)]):
        with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
            asyncio.run(
                graph_retrieval.hydrate_graph_evidence_locators(
                    FakeDB([_Result(support_rows), _Result(evidence_rows)]),
                    _library(),
                    _m3_result(),
                    max_evidence_per_fact=20,
                )
            )
        assert exc_info.value.code == "graph_publication_invariant_failed"


def test_m4_response_assembly_preserves_order_counts_and_privacy():
    locator = GraphRetrievalEvidenceLocator(
        evidence_id=EVIDENCE_IDS[0],
        document_id=DOCUMENT_ID,
        document_revision_id=REVISION_ID,
        document_block_id=BLOCK_ID,
        evidence_kind="chunk",
        page_start=1,
        page_end=1,
        source_start=0,
        source_end=12,
    )
    hydration = graph_retrieval.GraphEvidenceHydration(
        facts=(
            graph_retrieval.HydratedFactEvidence("entity", ENTITY_ID, (locator,)),
            graph_retrieval.HydratedFactEvidence("entity", SECOND_ENTITY_ID, ()),
            graph_retrieval.HydratedFactEvidence("relation", RELATION_IDS[0], (locator,)),
        ),
        truncated=True,
    )

    response = graph_retrieval.build_graph_retrieval_response(
        _m3_result(),
        hydration,
        contract_version="v1",
    )
    payload = response.model_dump(mode="json")

    assert payload["publication"]["id"] == str(PUBLICATION_ID)
    assert [row["id"] for row in payload["nodes"]] == [str(ENTITY_ID), str(SECOND_ENTITY_ID)]
    assert payload["relations"][0]["id"] == str(RELATION_IDS[0])
    assert payload["counts"] == {
        "seeds": 1,
        "nodes": 2,
        "relations": 1,
        "evidence_locators": 2,
    }
    assert payload["truncated"] == {"nodes": False, "relations": False, "evidence": True}
    for forbidden in ("properties", "quote_text", "evidence_text_snapshot", "secret"):
        assert forbidden not in str(payload).lower()


def test_m4_locator_disabled_skips_hydration_and_fences_last(monkeypatch):
    calls = []
    result = _m3_result()

    async def core(*_args, **_kwargs):
        calls.append("core")
        return result

    async def no_hydration(*_args, **_kwargs):
        raise AssertionError("locator-disabled request must skip hydration")

    async def fence(*_args, **_kwargs):
        calls.append("fence")

    monkeypatch.setattr(graph_retrieval, "_resolve_and_traverse_graph_retrieval_query_unfenced", core)
    monkeypatch.setattr(graph_retrieval, "hydrate_graph_evidence_locators", no_hydration)
    monkeypatch.setattr(graph_retrieval, "assert_graph_snapshot_still_current", fence)
    request = GraphRetrievalQueryRequest.model_validate(
        _request_payload(include_evidence_locators=False)
    )

    response = asyncio.run(
        graph_retrieval.execute_graph_retrieval_query(
            FakeDB([]),
            _library(),
            request,
            config=Settings(_env_file=None),
        )
    )

    assert calls == ["core", "fence"]
    assert all(not row.evidence for row in (*response.nodes, *response.relations))
    assert response.truncated.evidence is False


def test_m4_route_is_mounted_and_validation_is_route_local():
    from app.api.v06_graph_retrieval import router as v06_router

    openapi = app.openapi()
    paths = openapi["paths"]
    assert "/libraries/{slug}/v06/graph/query" in paths
    operation = paths["/libraries/{slug}/v06/graph/query"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GraphRetrievalQueryRequest"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GraphRetrievalQueryResponse"
    }
    for status_code in ("404", "409", "422", "500", "503", "504"):
        assert operation["responses"][status_code]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/GraphRetrievalErrorResponse"}
    request_properties = openapi["components"]["schemas"]["GraphRetrievalQueryRequest"][
        "properties"
    ]
    assert "properties" not in request_properties
    assert "include_properties" not in request_properties
    route = next(
        row
        for row in v06_router.routes
        if getattr(row, "path", None) == "/libraries/{slug}/v06/graph/query"
    )
    assert type(route).__name__ == "SanitizedGraphRetrievalRoute"
    assert len(v06_router.routes) == 1


def test_m4_route_validation_never_echoes_nested_private_input(monkeypatch):
    monkeypatch.setattr(settings, "graph_retrieval_enabled", True)
    db = AsyncMock()
    secret = "must-never-echo"
    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())):
            response = _client(db).post(
                "/libraries/m2/v06/graph/query",
                json=_request_payload(properties={"nested": {"secret": secret}}),
            )
    finally:
        _clear_overrides()

    assert response.status_code == 422
    assert response.json() == {
        "detail": "graph_retrieval_invalid_request",
        "candidates": [],
    }
    assert secret not in response.text


def test_m4_route_does_not_rewrite_existing_auth_or_permission_errors(monkeypatch):
    monkeypatch.setattr(settings, "graph_retrieval_enabled", True)
    unauthenticated = TestClient(app).post(
        "/libraries/m2/v06/graph/query",
        json=_request_payload(),
    )
    assert unauthenticated.status_code == 401

    db = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.deps.has_permission", return_value=False),
        ):
            forbidden = _client(db, superuser=False).post(
                "/libraries/m2/v06/graph/query",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "forbidden"}


def test_m4_disabled_route_executes_no_graph_service(monkeypatch):
    monkeypatch.setattr(settings, "graph_retrieval_enabled", False)
    service = AsyncMock()
    db = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.api.v06_graph_retrieval.graph_retrieval.execute_graph_retrieval_query", service),
        ):
            response = _client(db).post(
                "/libraries/m2/v06/graph/query",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()

    assert response.status_code == 503
    assert response.json()["detail"] == "graph_retrieval_disabled"
    service.assert_not_awaited()


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("seed_not_found", 404),
        ("seed_ambiguous", 409),
        ("publication_changed", 409),
        ("relation_type_not_found", 409),
        ("graph_retrieval_limit_exceeded", 422),
        ("graph_publication_unavailable", 503),
        ("graph_publication_invariant_failed", 503),
    ],
)
def test_m4_service_errors_are_exact_and_rollback(monkeypatch, code, expected_status):
    monkeypatch.setattr(settings, "graph_retrieval_enabled", True)
    candidates = (
        GraphRetrievalAmbiguousCandidate(entity_id=ENTITY_ID, entity_type_key="node"),
    ) if code == "seed_ambiguous" else ()
    service = AsyncMock(side_effect=graph_retrieval.GraphRetrievalServiceError(code, candidates=candidates))
    db = AsyncMock()
    db.rollback = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.api.v06_graph_retrieval.graph_retrieval.execute_graph_retrieval_query", service),
        ):
            response = _client(db).post(
                "/libraries/m2/v06/graph/query",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()

    assert response.status_code == expected_status
    assert response.json()["detail"] == code
    assert bool(response.json()["candidates"]) == (code == "seed_ambiguous")
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_m4_timeout_and_unknown_errors_are_sanitized_and_rollback(monkeypatch, caplog):
    monkeypatch.setattr(settings, "graph_retrieval_enabled", True)
    monkeypatch.setattr(settings, "graph_retrieval_timeout_seconds", 0.001)
    db = AsyncMock()
    db.rollback = AsyncMock()

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(1)

    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.api.v06_graph_retrieval.graph_retrieval.execute_graph_retrieval_query", new=slow),
        ):
            timeout_response = _client(db).post(
                "/libraries/m2/v06/graph/query",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()
    assert timeout_response.status_code == 504
    assert timeout_response.json()["detail"] == "graph_retrieval_timeout"
    db.rollback.assert_awaited_once()

    secret = "secret-sql-parameter"
    db.reset_mock()
    db.rollback = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v06_graph_retrieval.graph_retrieval.execute_graph_retrieval_query",
                new=AsyncMock(side_effect=RuntimeError(secret)),
            ),
        ):
            unknown_response = _client(db).post(
                "/libraries/m2/v06/graph/query",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()
    assert unknown_response.status_code == 500
    assert unknown_response.json() == {
        "detail": "graph_retrieval_internal_error",
        "candidates": [],
    }
    assert secret not in unknown_response.text
    assert secret not in caplog.text
    db.rollback.assert_awaited_once()
