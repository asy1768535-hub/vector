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
from app.schemas.chat import ChatGraphEvidence
from app.schemas.dify import DifyRecord
from app.services.chat_graph_augmentation import (
    ChatGraphAugmentation,
    is_relationship_query,
    prepare_chat_graph_augmentation,
)


def _evidence() -> ChatGraphEvidence:
    ids = [uuid.uuid4() for _ in range(8)]
    return ChatGraphEvidence(
        publication_id=ids[0], relation_id=ids[1], source_entity_id=ids[2],
        source_entity_name="项目甲", relation_type_key="responsible_for", relation_label="负责",
        target_entity_id=ids[3], target_entity_name="系统乙", evidence_id=ids[4],
        document_id=ids[5], document_revision_id=ids[6], chunk_id=ids[7],
        title="职责说明", content="项目甲负责系统乙。",
    )


def test_relationship_gate_is_conservative_and_local():
    assert is_relationship_query("项目甲由谁负责？")
    assert is_relationship_query("系统 A 和系统 B 有什么关系？")
    assert not is_relationship_query("请总结这份文档")
    assert not is_relationship_query("项目甲是什么？")


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "answer_enabled", "expected_records"),
    [("shadow", True, 0), ("enabled", False, 0), ("enabled", True, 1)],
)
async def test_shadow_and_global_gate_control_prompt_fusion(mode, answer_enabled, expected_records):
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
    with patch(
        "app.services.chat_graph_augmentation.chat_graph_context.query_chat_graph_for_chunks",
        new=AsyncMock(return_value=result),
    ), patch(
        "app.services.chat_graph_augmentation._hydrate_relation_evidence",
        new=AsyncMock(return_value=[evidence]),
    ):
        augmentation = await prepare_chat_graph_augmentation(
            AsyncMock(), library, "谁负责这个系统？", records, config=config,
        )
    assert isinstance(augmentation, ChatGraphAugmentation)
    assert augmentation.candidate_count == 1
    assert len(augmentation.records) == expected_records
    assert augmentation.context_chars > 0 if expected_records else augmentation.context_chars == 0


def test_0050_is_single_head_and_matches_orm_contract():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0052"]
    assert script.get_revision("0050").down_revision == "0049"
    assert ChatMessage.__table__.columns.graph_augmented.nullable is False
    assert ChatMessage.__table__.columns.graph_evidence.nullable is False
    assert Library.__table__.columns.graph_assisted_chat_mode.nullable is False
    assert "ck_lib_graph_assisted_chat_mode" in {
        row.name for row in Library.__table__.constraints if isinstance(row, CheckConstraint)
    }
