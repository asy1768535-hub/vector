from __future__ import annotations

import inspect
import math
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError


ONTOLOGY_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
PUBLICATION_ID = uuid.UUID("60000000-0000-0000-0000-000000000002")
ENTITY_ID = uuid.UUID("60000000-0000-0000-0000-000000000003")
ENTITY_TYPE_ID = uuid.UUID("60000000-0000-0000-0000-000000000004")
RELATION_ID = uuid.UUID("60000000-0000-0000-0000-000000000005")
RELATION_TYPE_ID = uuid.UUID("60000000-0000-0000-0000-000000000006")
EVIDENCE_ID = uuid.UUID("60000000-0000-0000-0000-000000000007")
DOCUMENT_ID = uuid.UUID("60000000-0000-0000-0000-000000000008")
REVISION_ID = uuid.UUID("60000000-0000-0000-0000-000000000009")


def _schemas():
    from app.schemas.v06_graph_retrieval import (
        GraphRetrievalAmbiguousCandidate,
        GraphRetrievalCounts,
        GraphRetrievalEntityTypeRead,
        GraphRetrievalErrorResponse,
        GraphRetrievalEvidenceLocator,
        GraphRetrievalNodeRead,
        GraphRetrievalPublicationRead,
        GraphRetrievalQueryRequest,
        GraphRetrievalQueryResponse,
        GraphRetrievalRelationRead,
        GraphRetrievalRelationTypeRead,
        GraphRetrievalSeed,
        GraphRetrievalSeedMatch,
        GraphRetrievalTruncation,
    )

    return {
        model.__name__: model
        for model in (
            GraphRetrievalAmbiguousCandidate,
            GraphRetrievalCounts,
            GraphRetrievalEntityTypeRead,
            GraphRetrievalErrorResponse,
            GraphRetrievalEvidenceLocator,
            GraphRetrievalNodeRead,
            GraphRetrievalPublicationRead,
            GraphRetrievalQueryRequest,
            GraphRetrievalQueryResponse,
            GraphRetrievalRelationRead,
            GraphRetrievalRelationTypeRead,
            GraphRetrievalSeed,
            GraphRetrievalSeedMatch,
            GraphRetrievalTruncation,
        )
    }


def _valid_request_payload() -> dict:
    return {
        "ontology_version_id": str(ONTOLOGY_ID),
        "expected_publication_id": str(PUBLICATION_ID),
        "seeds": [{"entity_id": str(ENTITY_ID)}],
        "direction": "both",
        "relation_type_keys": ["belongs_to"],
        "max_hops": 1,
        "max_nodes": 100,
        "max_relations": 200,
        "include_evidence_locators": True,
    }


def test_v06_request_accepts_exact_id_and_name_seeds():
    request_model = _schemas()["GraphRetrievalQueryRequest"]

    by_id = request_model.model_validate(_valid_request_payload())
    assert by_id.seeds[0].entity_id == ENTITY_ID
    assert by_id.seeds[0].canonical_name is None

    payload = _valid_request_payload()
    payload["seeds"] = [{"canonical_name": "Alice", "entity_type_key": "person"}]
    by_name = request_model.model_validate(payload)
    assert by_name.seeds[0].canonical_name == "Alice"
    assert by_name.seeds[0].entity_type_key == "person"


@pytest.mark.parametrize(
    "seed",
    [
        {},
        {"entity_id": str(ENTITY_ID), "canonical_name": "Alice"},
        {"entity_id": str(ENTITY_ID), "entity_type_key": "person"},
        {"canonical_name": "   "},
        {"canonical_name": "Alice", "entity_type_key": "   "},
    ],
)
def test_v06_seed_contract_rejects_missing_ambiguous_or_blank_selectors(seed):
    seed_model = _schemas()["GraphRetrievalSeed"]
    with pytest.raises(ValidationError):
        seed_model.model_validate(seed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seeds", []),
        ("seeds", [{"entity_id": str(ENTITY_ID)} for _ in range(11)]),
        ("relation_type_keys", [f"type_{index}" for index in range(65)]),
        ("max_hops", -1),
        ("max_hops", 3),
        ("max_nodes", 0),
        ("max_nodes", 101),
        ("max_relations", 0),
        ("max_relations", 201),
        (
            "seeds",
            [{"entity_id": str(ENTITY_ID)} for _ in range(2)],
        ),
    ],
)
def test_v06_request_enforces_absolute_v1_bounds(field, value):
    request_model = _schemas()["GraphRetrievalQueryRequest"]
    payload = _valid_request_payload()
    payload[field] = value
    if field == "seeds" and len(value) == 2:
        payload["max_nodes"] = 1
    with pytest.raises(ValidationError):
        request_model.model_validate(payload)


def test_v06_request_rejects_properties_and_unknown_fields():
    request_model = _schemas()["GraphRetrievalQueryRequest"]
    for field in ("include_properties", "properties", "query", "cypher"):
        payload = _valid_request_payload()
        payload[field] = True
        with pytest.raises(ValidationError, match="extra_forbidden"):
            request_model.model_validate(payload)


def test_v06_response_contract_excludes_properties_and_sensitive_payload_fields():
    schemas = _schemas()
    forbidden = {
        "properties",
        "include_properties",
        "quote_text",
        "evidence_text_snapshot",
        "context_text",
        "raw_response",
        "candidate_payload",
        "prompt",
        "api_key",
        "secret",
    }
    for model in schemas.values():
        assert forbidden.isdisjoint(model.model_fields)
        assert model.model_config.get("extra") == "forbid"


def test_v06_response_serializes_only_the_frozen_public_contract():
    schemas = _schemas()
    publication = schemas["GraphRetrievalPublicationRead"](
        id=PUBLICATION_ID,
        ontology_version_id=ONTOLOGY_ID,
        manifest_version="v1",
        manifest_hash="a" * 64,
        activated_at="2026-07-16T00:00:00Z",
    )
    entity_type = schemas["GraphRetrievalEntityTypeRead"](
        id=ENTITY_TYPE_ID,
        key="person",
        label="Person",
    )
    relation_type = schemas["GraphRetrievalRelationTypeRead"](
        id=RELATION_TYPE_ID,
        key="belongs_to",
        label="Belongs To",
        direction="directed",
    )
    evidence = schemas["GraphRetrievalEvidenceLocator"](
        evidence_id=EVIDENCE_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=REVISION_ID,
        document_block_id=None,
        evidence_kind="direct_statement",
        page_start=1,
        page_end=1,
        source_start=0,
        source_end=12,
    )
    node = schemas["GraphRetrievalNodeRead"](
        id=ENTITY_ID,
        item_hash="b" * 64,
        entity_type=entity_type,
        canonical_name="Alice",
        normalized_name="alice",
        source_type="extracted",
        confidence=0.93,
        depth=0,
        evidence=[evidence],
    )
    relation = schemas["GraphRetrievalRelationRead"](
        id=RELATION_ID,
        item_hash="c" * 64,
        relation_type=relation_type,
        source_entity_id=ENTITY_ID,
        target_entity_id=ENTITY_ID,
        source_type="extracted",
        confidence=0.91,
        depth=1,
        evidence=[evidence],
    )
    response = schemas["GraphRetrievalQueryResponse"](
        contract_version="v1",
        publication=publication,
        seed_matches=[schemas["GraphRetrievalSeedMatch"](input_index=0, entity_id=ENTITY_ID)],
        nodes=[node],
        relations=[relation],
        counts=schemas["GraphRetrievalCounts"](
            seeds=1,
            nodes=1,
            relations=1,
            evidence_locators=2,
        ),
        truncated=schemas["GraphRetrievalTruncation"](
            nodes=False,
            relations=False,
            evidence=False,
        ),
    )

    payload = response.model_dump(mode="json")
    assert payload["contract_version"] == "v1"
    assert payload["publication"]["manifest_hash"] == "a" * 64
    assert payload["nodes"][0]["normalized_name"] == "alice"
    assert "properties" not in str(payload).lower()


def test_v06_error_contract_is_bounded_and_sanitized():
    schemas = _schemas()
    error = schemas["GraphRetrievalErrorResponse"](
        detail="seed_ambiguous",
        candidates=[
            schemas["GraphRetrievalAmbiguousCandidate"](
                entity_id=ENTITY_ID,
                entity_type_key="person",
            )
        ],
    )
    assert error.model_dump(mode="json") == {
        "detail": "seed_ambiguous",
        "candidates": [{"entity_id": str(ENTITY_ID), "entity_type_key": "person"}],
    }

    with pytest.raises(ValidationError):
        schemas["GraphRetrievalErrorResponse"](
            detail="seed_ambiguous",
            candidates=[
                {"entity_id": str(ENTITY_ID), "entity_type_key": "person"}
                for _ in range(11)
            ],
        )


def test_v06_config_defaults_and_startup_validation():
    from app.config import Settings, validate_graph_retrieval_startup

    config = Settings(_env_file=None)
    assert config.graph_retrieval_enabled is False
    assert config.graph_retrieval_contract_version == "v1"
    assert config.graph_retrieval_max_seeds == 10
    assert config.graph_retrieval_max_hops == 2
    assert config.graph_retrieval_max_nodes == 100
    assert config.graph_retrieval_max_relations == 200
    assert config.graph_retrieval_max_evidence_per_fact == 20
    assert config.graph_retrieval_timeout_seconds == 3.0
    validate_graph_retrieval_startup(config)


def test_v06_config_docs_and_env_example_match_defaults():
    env_text = Path(".env.example").read_text(encoding="utf-8")
    docs_text = Path("docs/05-configuration.md").read_text(encoding="utf-8")
    expected = {
        "GRAPH_RETRIEVAL_ENABLED": "false",
        "GRAPH_RETRIEVAL_CONTRACT_VERSION": "v1",
        "GRAPH_RETRIEVAL_MAX_SEEDS": "10",
        "GRAPH_RETRIEVAL_MAX_HOPS": "2",
        "GRAPH_RETRIEVAL_MAX_NODES": "100",
        "GRAPH_RETRIEVAL_MAX_RELATIONS": "200",
        "GRAPH_RETRIEVAL_MAX_EVIDENCE_PER_FACT": "20",
        "GRAPH_RETRIEVAL_TIMEOUT_SECONDS": "3.0",
    }
    for name, value in expected.items():
        assignment = f"{name}={value}"
        assert assignment in env_text
        assert assignment in docs_text

    index_text = Path("docs/README.md").read_text(encoding="utf-8")
    assert "2026-07-16-v0.6-published-graph-retrieval-m1.md" in index_text


@pytest.mark.parametrize(
    "overrides",
    [
        {"graph_retrieval_contract_version": ""},
        {"graph_retrieval_contract_version": "v2"},
        {"graph_retrieval_max_seeds": 0},
        {"graph_retrieval_max_seeds": 11},
        {"graph_retrieval_max_hops": 0},
        {"graph_retrieval_max_hops": 3},
        {"graph_retrieval_max_nodes": 0},
        {"graph_retrieval_max_nodes": 101},
        {"graph_retrieval_max_relations": 0},
        {"graph_retrieval_max_relations": 201},
        {"graph_retrieval_max_evidence_per_fact": 0},
        {"graph_retrieval_max_evidence_per_fact": 21},
        {"graph_retrieval_max_seeds": 10, "graph_retrieval_max_nodes": 9},
        {"graph_retrieval_timeout_seconds": 0},
        {"graph_retrieval_timeout_seconds": math.inf},
        {"graph_retrieval_timeout_seconds": math.nan},
    ],
)
def test_v06_startup_rejects_invalid_or_unbounded_config(overrides):
    from app.config import Settings, validate_graph_retrieval_startup

    with pytest.raises(RuntimeError, match="graph retrieval"):
        validate_graph_retrieval_startup(Settings(_env_file=None, **overrides))


def test_api_lifespan_calls_v06_startup_validator(monkeypatch):
    import app.main as main

    calls = []
    monkeypatch.setattr(main, "validate_graph_retrieval_startup", lambda config: calls.append(config))
    main.assert_graph_retrieval_startup_security()
    assert calls == [main.settings]
    assert "assert_graph_retrieval_startup_security" in inspect.getsource(main.lifespan)


def test_v06_m4_mounts_only_query_route_and_adds_no_migration():
    from app.main import app

    v06_paths = [path for path in app.openapi()["paths"] if "/v06/" in path]
    assert v06_paths == ["/libraries/{slug}/v06/graph/query"]
    migration = ScriptDirectory.from_config(Config("alembic.ini")).get_revision("0023")
    assert migration is not None
    assert migration.down_revision == "0022"
    assert not list(Path("alembic/versions").glob("*v06*.py"))


def test_v06_m1_preserves_v05_healthy_publication_helpers():
    from app.services import graph_publication_read

    assert callable(graph_publication_read.list_healthy_published_entities)
    assert callable(graph_publication_read.list_healthy_published_relations)
    assert graph_publication_read.CURRENT_STATUSES == ("active", "degraded")
