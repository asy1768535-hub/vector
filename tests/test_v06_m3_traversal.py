from __future__ import annotations

import asyncio
import dataclasses
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.config import Settings
from app.services import graph_retrieval
from tests.test_v06_m2_snapshot_resolver import (
    ENTITY_ID,
    ENTITY_TYPE_ID,
    RELATION_TYPE_ID,
    SECOND_ENTITY_ID,
    FakeDB,
    _current_row,
    _entity_row,
    _library,
    _load_results,
    _relation_type_row,
    _request,
    _Result,
    _snapshot,
)


THIRD_ENTITY_ID = uuid.UUID("61000000-0000-0000-0000-000000000009")
FOURTH_ENTITY_ID = uuid.UUID("61000000-0000-0000-0000-000000000010")
RELATION_IDS = tuple(
    uuid.UUID(f"62000000-0000-0000-0000-{index:012d}") for index in range(1, 9)
)


def _resolved(
    entity_id: uuid.UUID,
    *,
    input_index: int = 0,
    name: str | None = None,
) -> graph_retrieval.ResolvedPublishedEntity:
    canonical_name = name or f"Entity {str(entity_id)[-4:]}"
    return graph_retrieval.ResolvedPublishedEntity(
        input_index=input_index,
        entity_id=entity_id,
        item_hash="b" * 64,
        entity_type_id=ENTITY_TYPE_ID,
        entity_type_key="node",
        entity_type_label="Node",
        canonical_name=canonical_name,
        normalized_name=canonical_name.casefold(),
        source_type="manual",
        confidence=None,
    )


def _relation_type(
    *,
    key: str = "link",
    direction: str = "directed",
) -> graph_retrieval.ResolvedRelationType:
    return graph_retrieval.ResolvedRelationType(
        relation_type_id=RELATION_TYPE_ID,
        key=key,
        label=key.title(),
        direction=direction,
    )


def _hop_row(
    relation_id: uuid.UUID,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    *,
    key: str = "link",
    direction: str = "directed",
):
    values = {
        "relation_id": relation_id,
        "relation_item_hash": "c" * 64,
        "relation_type_id": RELATION_TYPE_ID,
        "relation_type_key": key,
        "relation_type_label": key.title(),
        "relation_type_direction": direction,
        "source_entity_id": source_id,
        "target_entity_id": target_id,
        "relation_source_type": "manual",
        "relation_confidence": None,
    }
    for prefix, entity_id in (("source", source_id), ("target", target_id)):
        name = f"Entity {str(entity_id)[-4:]}"
        values.update(
            {
                f"{prefix}_item_hash": "b" * 64,
                f"{prefix}_entity_type_id": ENTITY_TYPE_ID,
                f"{prefix}_entity_type_key": "node",
                f"{prefix}_entity_type_label": "Node",
                f"{prefix}_canonical_name": name,
                f"{prefix}_normalized_name": name.casefold(),
                f"{prefix}_source_type": "manual",
                f"{prefix}_confidence": None,
            }
        )
    return SimpleNamespace(**values)


def _traverse(
    results,
    *,
    seeds=None,
    relation_types=(),
    direction="both",
    max_hops=1,
    max_nodes=100,
    max_relations=200,
):
    db = FakeDB([_Result(rows) for rows in results])
    traversal = asyncio.run(
        graph_retrieval.traverse_published_graph(
            db,
            _library(),
            _snapshot(),
            seeds or (_resolved(ENTITY_ID),),
            relation_types,
            direction=direction,
            max_hops=max_hops,
            max_nodes=max_nodes,
            max_relations=max_relations,
        )
    )
    return traversal, db


def test_m3_zero_hop_deduplicates_and_sorts_seeds_without_sql():
    seeds = (
        _resolved(SECOND_ENTITY_ID, input_index=0),
        _resolved(ENTITY_ID, input_index=1),
        _resolved(SECOND_ENTITY_ID, input_index=2),
    )

    traversal, db = _traverse([], seeds=seeds, max_hops=0, max_nodes=3)

    assert [node.entity_id for node in traversal.nodes] == [ENTITY_ID, SECOND_ENTITY_ID]
    assert [node.depth for node in traversal.nodes] == [0, 0]
    assert traversal.relations == ()
    assert traversal.truncated.nodes is False
    assert traversal.truncated.relations is False
    assert db.statements == []
    with pytest.raises(dataclasses.FrozenInstanceError):
        traversal.truncated.nodes = True


@pytest.mark.parametrize("direction", ["outbound", "inbound", "both"])
def test_m3_hop_sql_is_scoped_bounded_and_private(direction):
    traversal, db = _traverse(
        [[]],
        relation_types=(_relation_type(),),
        direction=direction,
    )

    assert traversal.relations == ()
    assert len(db.statements) == 1
    sql = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    for required in (
        "graph_publication_items",
        "knowledge_relations",
        "relation_types",
        "entity_types",
        "order by",
        "limit 201",
        "not_required",
        "approved",
    ):
        assert required in sql
    for forbidden in (
        ".properties",
        "support_evidence_ids",
        "fact_snapshot",
        "authority_level",
        "valid_from",
        "quote_text",
        "evidence_text_snapshot",
    ):
        assert forbidden not in sql


def test_m3_two_hop_bfs_is_canonical_min_depth_and_deduplicated():
    traversal, db = _traverse(
        [
            [
                _hop_row(RELATION_IDS[1], ENTITY_ID, THIRD_ENTITY_ID, key="z_link"),
                _hop_row(RELATION_IDS[0], ENTITY_ID, SECOND_ENTITY_ID, key="a_link"),
            ],
            [
                _hop_row(RELATION_IDS[4], SECOND_ENTITY_ID, ENTITY_ID, key="cycle"),
                _hop_row(RELATION_IDS[3], THIRD_ENTITY_ID, FOURTH_ENTITY_ID, key="path"),
                _hop_row(RELATION_IDS[2], SECOND_ENTITY_ID, FOURTH_ENTITY_ID, key="path"),
            ],
        ],
        max_hops=2,
    )

    assert len(db.statements) == 2
    assert [node.entity_id for node in traversal.nodes] == [
        ENTITY_ID,
        SECOND_ENTITY_ID,
        THIRD_ENTITY_ID,
        FOURTH_ENTITY_ID,
    ]
    assert [node.depth for node in traversal.nodes] == [0, 1, 1, 2]
    assert [row.relation_id for row in traversal.relations] == [
        RELATION_IDS[0],
        RELATION_IDS[1],
        RELATION_IDS[4],
        RELATION_IDS[2],
        RELATION_IDS[3],
    ]
    assert [row.depth for row in traversal.relations] == [1, 1, 2, 2, 2]
    assert traversal.truncated == graph_retrieval.GraphTraversalTruncation(
        nodes=False,
        relations=False,
    )


def test_m3_exact_limits_without_extra_candidate_are_not_truncated():
    traversal, _ = _traverse(
        [[_hop_row(RELATION_IDS[0], ENTITY_ID, SECOND_ENTITY_ID)]],
        max_nodes=2,
        max_relations=1,
    )

    assert len(traversal.nodes) == 2
    assert len(traversal.relations) == 1
    assert traversal.truncated == graph_retrieval.GraphTraversalTruncation(False, False)


def test_m3_node_limit_keeps_complete_stable_prefix():
    traversal, _ = _traverse(
        [[
            _hop_row(RELATION_IDS[0], ENTITY_ID, SECOND_ENTITY_ID),
            _hop_row(RELATION_IDS[1], ENTITY_ID, THIRD_ENTITY_ID),
        ]],
        max_nodes=2,
        max_relations=3,
    )

    assert [node.entity_id for node in traversal.nodes] == [ENTITY_ID, SECOND_ENTITY_ID]
    assert [row.relation_id for row in traversal.relations] == [RELATION_IDS[0]]
    assert traversal.truncated == graph_retrieval.GraphTraversalTruncation(True, False)


def test_m3_relation_limit_and_combined_limit_flags_are_exact():
    rows = [
        _hop_row(RELATION_IDS[0], ENTITY_ID, SECOND_ENTITY_ID),
        _hop_row(RELATION_IDS[1], ENTITY_ID, THIRD_ENTITY_ID),
    ]
    relation_only, _ = _traverse([rows], max_nodes=3, max_relations=1)
    combined, _ = _traverse([rows], max_nodes=2, max_relations=1)

    assert relation_only.truncated == graph_retrieval.GraphTraversalTruncation(False, True)
    assert combined.truncated == graph_retrieval.GraphTraversalTruncation(True, True)
    assert len(relation_only.relations) == len(combined.relations) == 1


def test_m3_relation_between_known_nodes_is_allowed_at_node_cap():
    traversal, _ = _traverse(
        [[_hop_row(RELATION_IDS[0], ENTITY_ID, SECOND_ENTITY_ID)]],
        seeds=(_resolved(ENTITY_ID), _resolved(SECOND_ENTITY_ID, input_index=1)),
        max_nodes=2,
        max_relations=1,
    )

    assert len(traversal.nodes) == 2
    assert len(traversal.relations) == 1
    assert traversal.truncated == graph_retrieval.GraphTraversalTruncation(False, False)


def test_m3_full_mixed_two_hop_orchestrator_uses_ten_statements():
    db = FakeDB(
        [
            *_load_results(),
            _Result([_entity_row(0)]),
            _Result([_entity_row(1)]),
            _Result([_relation_type_row()]),
            _Result([_hop_row(RELATION_IDS[0], ENTITY_ID, SECOND_ENTITY_ID, key="member_of")]),
            _Result([]),
            _Result([_current_row()]),
        ]
    )
    request = _request(
        seeds=[{"entity_id": ENTITY_ID}, {"canonical_name": "Alice", "entity_type_key": "person"}],
        relation_type_keys=["member_of"],
        max_hops=2,
    )

    result = asyncio.run(
        graph_retrieval.resolve_and_traverse_graph_retrieval_query(
            db,
            _library(),
            request,
            config=Settings(_env_file=None),
        )
    )

    assert len(db.statements) == 10
    assert len(result.resolution.seeds) == 2
    assert [node.entity_id for node in result.traversal.nodes] == [ENTITY_ID, SECOND_ENTITY_ID]
    assert len(result.traversal.relations) == 1
