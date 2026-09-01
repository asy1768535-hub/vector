from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from app.api.chat import _split_used_records
from app.models.chat_history import ChatMessage
from app.models.library import Library
from app.schemas.chat import ChatGraphEvidence, ChatMessageRequest
from app.schemas.dify import DifyRecord
from app.services.chat_graph_augmentation import (
    ChatGraphAugmentation,
    MAX_CHAT_GRAPH_CHUNKS,
    _chunk_ids,
    _shortest_path_relation_ids,
    prepare_chat_graph_augmentation,
)
from app.services.chat_graph_context import MAX_CONTEXT_HOPS


def _evidence() -> ChatGraphEvidence:
    ids = [uuid.uuid4() for _ in range(8)]
    return ChatGraphEvidence(
        publication_id=ids[0], relation_id=ids[1], source_entity_id=ids[2],
        source_entity_name="项目甲", relation_type_key="responsible_for", relation_label="负责",
        target_entity_id=ids[3], target_entity_name="系统乙", evidence_id=ids[4],
        document_id=ids[5], document_revision_id=ids[6], chunk_id=ids[7],
        title="职责说明", content="项目甲负责系统乙。", depth=3,
    )


def test_graph_scope_follows_adjustable_retrieval_results_with_a_hard_cap():
    chunk_ids = [uuid.uuid4() for _ in range(MAX_CHAT_GRAPH_CHUNKS + 3)]
    records = [
        DifyRecord(title=str(index), content="正文", score=1, metadata={"chunk_id": str(chunk_id)})
        for index, chunk_id in enumerate(chunk_ids)
    ]

    assert _chunk_ids(records[:3]) == tuple(chunk_ids[:3])
    assert _chunk_ids(records) == tuple(chunk_ids[:MAX_CHAT_GRAPH_CHUNKS])
    assert MAX_CONTEXT_HOPS == 3
    assert _evidence().depth == 3


def test_split_used_records_preserves_real_citation_indexes():
    source = DifyRecord(
        title="普通来源", content="正文", score=0.9,
        metadata={"document_id": "d1", "chunk_id": "c1"},
    )
    evidence = _evidence()
    graph = DifyRecord(
        title="图谱关系", content=evidence.content, score=1,
        metadata={"chat_graph_evidence": evidence.model_dump(mode="json")},
    )
    sources, graph_evidence = _split_used_records([source, graph])
    assert len(sources) == 1
    assert graph_evidence[0].citation_index == 2


def test_graph_evidence_prioritizes_the_shortest_path_between_query_seeds():
    nodes = [uuid.uuid4() for _ in range(5)]
    relations = [
        SimpleNamespace(id=uuid.uuid4(), source_entity_id=nodes[0], target_entity_id=nodes[1]),
        SimpleNamespace(id=uuid.uuid4(), source_entity_id=nodes[1], target_entity_id=nodes[2]),
        SimpleNamespace(id=uuid.uuid4(), source_entity_id=nodes[2], target_entity_id=nodes[3]),
        SimpleNamespace(id=uuid.uuid4(), source_entity_id=nodes[0], target_entity_id=nodes[4]),
    ]
    graph = SimpleNamespace(
        seed_matches=[
            SimpleNamespace(entity_id=nodes[0]),
            SimpleNamespace(entity_id=nodes[3]),
        ],
        relations=relations,
    )

    assert _shortest_path_relation_ids(graph) == {row.id for row in relations[:3]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "answer_enabled", "request_enabled", "expected_records"),
    [
        ("shadow", True, True, 0),
        ("enabled", False, True, 1),
        ("enabled", True, True, 1),
        ("enabled", True, False, 0),
    ],
)
async def test_library_and_request_modes_control_prompt_fusion(
    mode, answer_enabled, request_enabled, expected_records,
):
    chunk_id = uuid.uuid4()
    library = SimpleNamespace(
        id=uuid.uuid4(), graph_extraction_enabled=True, graph_assisted_chat_mode=mode,
    )
    config = SimpleNamespace(
        graph_retrieval_enabled=True,
        graph_retrieval_timeout_seconds=1.0,
        chat_graph_answer_enabled=answer_enabled,
    )
    records = [DifyRecord(title="RAG", content="正文", score=1, metadata={"chunk_id": str(chunk_id)})]
    graph = SimpleNamespace(relations=[object()])
    result = SimpleNamespace(graph=graph)
    evidence = _evidence()
    graph_query = AsyncMock(return_value=result)
    with patch(
        "app.services.chat_graph_augmentation.chat_graph_context.query_chat_graph_for_chunks",
        new=graph_query,
    ), patch(
        "app.services.chat_graph_augmentation._hydrate_relation_evidence",
        new=AsyncMock(return_value=[evidence]),
    ):
        augmentation = await prepare_chat_graph_augmentation(
            AsyncMock(), library, "请概括项目甲的情况", records,
            request_enabled=request_enabled, config=config,
        )
    assert isinstance(augmentation, ChatGraphAugmentation)
    assert augmentation.candidate_count == (1 if request_enabled else 0)
    assert len(augmentation.records) == expected_records
    assert augmentation.context_chars > 0 if expected_records else augmentation.context_chars == 0
    assert graph_query.await_count == (1 if request_enabled else 0)


def test_chat_request_enables_graph_assistance_by_default_and_accepts_opt_out():
    default_request = ChatMessageRequest(library_slug="demo", query="问题")
    disabled_request = ChatMessageRequest(library_slug="demo", query="问题", use_graph=False)

    assert default_request.use_graph is True
    assert disabled_request.use_graph is False


@pytest.mark.asyncio
async def test_query_entities_seed_graph_when_vector_retrieval_is_empty():
    library = SimpleNamespace(
        id=uuid.uuid4(), graph_extraction_enabled=True, graph_assisted_chat_mode="enabled",
    )
    config = SimpleNamespace(
        graph_retrieval_enabled=True,
        graph_retrieval_timeout_seconds=1.0,
    )
    graph = SimpleNamespace(relations=[object()])
    evidence = _evidence()
    with patch(
        "app.services.chat_graph_augmentation.chat_graph_context.query_chat_graph_for_query_entities",
        new=AsyncMock(return_value=SimpleNamespace(graph=graph)),
    ) as query_entities, patch(
        "app.services.chat_graph_augmentation._hydrate_relation_evidence",
        new=AsyncMock(return_value=[evidence]),
    ):
        augmentation = await prepare_chat_graph_augmentation(
            AsyncMock(), library, "从项目甲出发查找系统乙", [], config=config,
        )

    query_entities.assert_awaited_once()
    assert augmentation.reason_code == "augmented"
    assert len(augmentation.records) == 1


def test_0050_is_single_head_and_matches_orm_contract():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision("0050").down_revision == "0049"
    assert ChatMessage.__table__.columns.graph_augmented.nullable is False
    assert ChatMessage.__table__.columns.graph_evidence.nullable is False
    assert Library.__table__.columns.graph_assisted_chat_mode.nullable is False
    assert "ck_lib_graph_assisted_chat_mode" in {
        row.name for row in Library.__table__.constraints if isinstance(row, CheckConstraint)
    }
