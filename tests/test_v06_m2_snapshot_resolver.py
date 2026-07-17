from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models.library import Library
from app.schemas.v06_graph_retrieval import GraphRetrievalQueryRequest, GraphRetrievalSeed
from app.services import graph_retrieval


LIBRARY_ID = uuid.UUID("61000000-0000-0000-0000-000000000001")
ONTOLOGY_ID = uuid.UUID("61000000-0000-0000-0000-000000000002")
PUBLICATION_ID = uuid.UUID("61000000-0000-0000-0000-000000000003")
ENTITY_ID = uuid.UUID("61000000-0000-0000-0000-000000000004")
SECOND_ENTITY_ID = uuid.UUID("61000000-0000-0000-0000-000000000005")
ENTITY_TYPE_ID = uuid.UUID("61000000-0000-0000-0000-000000000006")
SECOND_TYPE_ID = uuid.UUID("61000000-0000-0000-0000-000000000007")
RELATION_TYPE_ID = uuid.UUID("61000000-0000-0000-0000-000000000008")


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class FakeDB:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.results:
            raise AssertionError("unexpected database statement")
        return self.results.pop(0)


class NoQueryDB:
    async def execute(self, _statement):  # pragma: no cover - assertion path
        raise AssertionError("invalid limits must fail before database access")


def _library() -> Library:
    return Library(
        id=LIBRARY_ID,
        slug="m2",
        name="M2",
        qdrant_collection="m2",
        embedding_model="bge-m3",
        embedding_dim=1024,
    )


def _current_row(**overrides):
    values = {
        "publication_id": PUBLICATION_ID,
        "library_id": LIBRARY_ID,
        "ontology_version_id": ONTOLOGY_ID,
        "publication_status": "active",
        "manifest_version": "v1",
        "manifest_hash": "a" * 64,
        "activated_at": "2026-07-16T00:00:00+00:00",
        "entity_count": 2,
        "relation_count": 1,
        "ontology_status": "active",
        "ontology_library_id": LIBRARY_ID,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _item_aggregate(**overrides):
    values = {
        "total_count": 3,
        "entity_count": 2,
        "relation_count": 1,
        "active_count": 3,
        "wrong_scope_count": 0,
        "invalid_shape_count": 0,
        "invalid_hash_count": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _healthy_aggregate(count: int):
    return SimpleNamespace(healthy_count=count)


def _snapshot():
    return graph_retrieval.HealthyGraphSnapshot(
        publication_id=PUBLICATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        manifest_version="v1",
        manifest_hash="a" * 64,
        activated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        entity_count=2,
        relation_count=1,
    )


def _entity_row(
    input_index: int,
    *,
    entity_id: uuid.UUID = ENTITY_ID,
    type_id: uuid.UUID = ENTITY_TYPE_ID,
    type_key: str = "person",
    name: str = "Alice",
):
    return SimpleNamespace(
        input_index=input_index,
        entity_id=entity_id,
        item_hash="b" * 64,
        entity_type_id=type_id,
        entity_type_key=type_key,
        entity_type_label=type_key.title(),
        canonical_name=name,
        normalized_name=name.casefold(),
        source_type="manual",
        confidence=None,
    )


def _relation_type_row(key: str = "member_of"):
    return SimpleNamespace(
        relation_type_id=RELATION_TYPE_ID,
        key=key,
        label="Member Of",
        direction="directed",
    )


def _request(**overrides) -> GraphRetrievalQueryRequest:
    payload = {
        "ontology_version_id": ONTOLOGY_ID,
        "expected_publication_id": PUBLICATION_ID,
        "seeds": [{"entity_id": ENTITY_ID}],
        "relation_type_keys": [],
    }
    payload.update(overrides)
    return GraphRetrievalQueryRequest.model_validate(payload)


def _load_results(**item_overrides):
    return [
        _Result([_current_row()]),
        _Result([_item_aggregate(**item_overrides)]),
        _Result([_healthy_aggregate(2)]),
        _Result([_healthy_aggregate(1)]),
    ]


def test_m2_runtime_limits_fail_before_database_access():
    config = Settings(_env_file=None, graph_retrieval_max_seeds=1)
    request = _request(seeds=[{"entity_id": ENTITY_ID}, {"entity_id": SECOND_ENTITY_ID}])

    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        asyncio.run(graph_retrieval.resolve_graph_retrieval_query(NoQueryDB(), _library(), request, config=config))

    assert exc_info.value.code == "graph_retrieval_limit_exceeded"
    assert str(exc_info.value) == "graph_retrieval_limit_exceeded"

    from app.schemas.v06_graph_retrieval import GraphRetrievalErrorResponse

    assert (
        GraphRetrievalErrorResponse(detail="graph_retrieval_limit_exceeded").detail
        == "graph_retrieval_limit_exceeded"
    )


def test_m2_loads_healthy_snapshot_with_aggregate_invariants():
    db = FakeDB(_load_results())

    snapshot = asyncio.run(
        graph_retrieval.load_healthy_graph_snapshot(
            db,
            _library(),
            ONTOLOGY_ID,
            expected_publication_id=PUBLICATION_ID,
        )
    )

    assert snapshot == _snapshot()
    assert len(db.statements) == 4
    sql = "\n".join(str(statement).lower() for statement in db.statements)
    assert "entity_alias" not in sql
    assert ".properties" not in sql


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    [
        ([], "graph_publication_unavailable"),
        ([_current_row(publication_status="degraded")], "graph_publication_unavailable"),
        ([_current_row(manifest_version="v2")], "graph_publication_invariant_failed"),
        ([_current_row(manifest_hash="invalid")], "graph_publication_invariant_failed"),
        ([_current_row(ontology_status="disabled")], "graph_publication_invariant_failed"),
        ([_current_row(), _current_row()], "graph_publication_invariant_failed"),
    ],
)
def test_m2_start_snapshot_fails_closed(rows, expected_code):
    db = FakeDB([_Result(rows)])
    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        asyncio.run(graph_retrieval.load_healthy_graph_snapshot(db, _library(), ONTOLOGY_ID))
    assert exc_info.value.code == expected_code


def test_m2_expected_publication_mismatch_is_409_contract():
    db = FakeDB([_Result([_current_row()])])
    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        asyncio.run(
            graph_retrieval.load_healthy_graph_snapshot(
                db,
                _library(),
                ONTOLOGY_ID,
                expected_publication_id=uuid.UUID("61000000-0000-0000-0000-000000000099"),
            )
        )
    assert exc_info.value.code == "publication_changed"


@pytest.mark.parametrize(
    "results",
    [
        [
            _Result([_current_row()]),
            _Result([_item_aggregate(total_count=2)]),
        ],
        [
            _Result([_current_row()]),
            _Result([_item_aggregate()]),
            _Result([_healthy_aggregate(1)]),
        ],
        [
            _Result([_current_row()]),
            _Result([_item_aggregate()]),
            _Result([_healthy_aggregate(2)]),
            _Result([_healthy_aggregate(0)]),
        ],
    ],
)
def test_m2_partial_item_entity_or_relation_membership_fails_closed(results):
    db = FakeDB(results)
    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        asyncio.run(graph_retrieval.load_healthy_graph_snapshot(db, _library(), ONTOLOGY_ID))
    assert exc_info.value.code == "graph_publication_invariant_failed"


def test_m2_resolves_id_and_normalized_name_seeds_in_two_batch_queries(monkeypatch):
    calls = []
    monkeypatch.setattr(
        graph_retrieval,
        "normalize_graph_name_v1",
        lambda value: calls.append(value) or "alice",
    )
    db = FakeDB(
        [
            _Result([_entity_row(0)]),
            _Result([_entity_row(1, entity_id=SECOND_ENTITY_ID, type_id=SECOND_TYPE_ID)]),
        ]
    )
    seeds = [
        GraphRetrievalSeed(entity_id=ENTITY_ID),
        GraphRetrievalSeed(canonical_name=" Alice ", entity_type_key="team"),
    ]

    rows = asyncio.run(
        graph_retrieval.resolve_published_seeds(db, _library(), _snapshot(), seeds)
    )

    assert [row.input_index for row in rows] == [0, 1]
    assert [row.entity_id for row in rows] == [ENTITY_ID, SECOND_ENTITY_ID]
    assert calls == [" Alice "]
    assert len(db.statements) == 2
    sql = "\n".join(str(statement).lower() for statement in db.statements)
    assert "values" in sql
    assert "entity_alias" not in sql
    assert ".properties" not in sql


def test_m2_missing_seed_is_sanitized_not_found():
    db = FakeDB([_Result([])])
    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        asyncio.run(
            graph_retrieval.resolve_published_seeds(
                db,
                _library(),
                _snapshot(),
                [GraphRetrievalSeed(entity_id=ENTITY_ID)],
            )
        )
    assert exc_info.value.code == "seed_not_found"
    assert str(ENTITY_ID) not in str(exc_info.value)


def test_m2_ambiguous_seed_candidates_are_deterministic_and_bounded():
    rows = [
        _entity_row(0, entity_id=uuid.UUID(int=index + 1), type_id=SECOND_TYPE_ID, type_key=f"type_{index:02d}")
        for index in reversed(range(12))
    ]
    db = FakeDB([_Result(rows)])

    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        asyncio.run(
            graph_retrieval.resolve_published_seeds(
                db,
                _library(),
                _snapshot(),
                [GraphRetrievalSeed(canonical_name="Alice")],
            )
        )

    error = exc_info.value
    assert error.code == "seed_ambiguous"
    assert len(error.candidates) == 10
    assert [row.entity_type_key for row in error.candidates] == [f"type_{index:02d}" for index in range(10)]


def test_m2_relation_type_filters_are_scoped_and_stable():
    db = FakeDB([_Result([_relation_type_row()])])
    rows = asyncio.run(
        graph_retrieval.resolve_relation_type_filters(
            db,
            _library(),
            _snapshot(),
            ["member_of", "member_of"],
        )
    )
    assert len(rows) == 1
    assert rows[0].key == "member_of"

    assert asyncio.run(
        graph_retrieval.resolve_relation_type_filters(
            FakeDB([]),
            _library(),
            _snapshot(),
            [],
        )
    ) == ()

    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        asyncio.run(
            graph_retrieval.resolve_relation_type_filters(
                FakeDB([_Result([])]),
                _library(),
                _snapshot(),
                ["foreign_only"],
            )
        )
    assert exc_info.value.code == "relation_type_not_found"


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    [
        ([], "publication_changed"),
        ([_current_row(publication_id=uuid.UUID(int=99))], "publication_changed"),
        ([_current_row(publication_status="degraded")], "graph_publication_unavailable"),
        ([_current_row(manifest_hash="f" * 64)], "graph_publication_invariant_failed"),
        ([_current_row(), _current_row()], "graph_publication_invariant_failed"),
    ],
)
def test_m2_end_fence_fails_closed(rows, expected_code):
    with pytest.raises(graph_retrieval.GraphRetrievalServiceError) as exc_info:
        asyncio.run(
            graph_retrieval.assert_graph_snapshot_still_current(
                FakeDB([_Result(rows)]),
                _library(),
                _snapshot(),
            )
        )
    assert exc_info.value.code == expected_code


def test_m2_orchestrator_has_fixed_eight_statement_mixed_request():
    db = FakeDB(
        [
            *_load_results(),
            _Result([_entity_row(0)]),
            _Result([_entity_row(1, entity_id=SECOND_ENTITY_ID, type_id=SECOND_TYPE_ID)]),
            _Result([_relation_type_row()]),
            _Result([_current_row()]),
        ]
    )
    request = _request(
        seeds=[{"entity_id": ENTITY_ID}, {"canonical_name": "Alice", "entity_type_key": "person"}],
        relation_type_keys=["member_of"],
    )

    result = asyncio.run(
        graph_retrieval.resolve_graph_retrieval_query(
            db,
            _library(),
            request,
            config=Settings(_env_file=None),
        )
    )

    assert len(db.statements) == 8
    assert len(result.seeds) == 2
    assert len(result.relation_types) == 1


def test_m2_source_has_no_route_traversal_alias_or_sensitive_projection():
    source = inspect.getsource(graph_retrieval).lower()
    for forbidden in (
        "entityalias",
        "entity_alias",
        "qdrant",
        "graphrag",
        "shortest_path",
        "select(entity)",
        "select(knowledgerelation)",
        "entity.properties",
        "knowledgerelation.properties",
    ):
        assert forbidden not in source
